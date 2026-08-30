"""Авторизация v3 — учётные записи (multi-account).

- Таблица `accounts` в общей БД: address (npub@домен), pubkey_hex, password_hash,
  label, role ('admin' | 'user').
- Сессии: token → owner (pubkey_hex). Админ видит все ящики, обычный — свой.
- Пароли: PBKDF2-HMAC-SHA256, 100k итераций, соль.
- Семантика ответов: 200 + {"ok": false, "error": "..."} вместо 401 —
  внешний прокси *.v2.site конвертирует 401 в 502 (проверено 2026-08-26).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from fastapi import Response
from fastapi.responses import JSONResponse

from . import config as cfg
from .config import DOMAIN
from .db import connect

_ITER = 100_000

# ── безопасное хранение nsec (AES-256-GCM) ─────────────
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MASTER_KEY_PATH = os.path.join(cfg.BASE, "keys", "master.key")


def _master_key() -> bytes:
    """Мастер-ключ шифрования nsec. Генерируется один раз, права 600."""
    os.makedirs(os.path.dirname(_MASTER_KEY_PATH), exist_ok=True)
    if os.path.exists(_MASTER_KEY_PATH):
        with open(_MASTER_KEY_PATH, "rb") as f:
            return f.read()
    key = AESGCM.generate_key(bit_length=256)
    with open(_MASTER_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(_MASTER_KEY_PATH, 0o600)
    return key


def _encrypt_nsec(nsec_hex: str) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master_key()).encrypt(nonce, nsec_hex.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_nsec(token: str) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_master_key()).decrypt(nonce, ct, None).decode()


def save_mail_key(pubkey_hex: str, nsec_hex: str) -> None:
    """Сохранить nsec владельца (зашифрованным) в таблицу mail_keys."""
    with connect(cfg.DB) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO mail_keys (pubkey_hex, nsec_enc, updated_at)
               VALUES (?,?,?)""",
            (pubkey_hex, _encrypt_nsec(nsec_hex), int(__import__("time").time())),
        )
        conn.commit()


def get_mail_key(pubkey_hex: str) -> str | None:
    """Вернуть расшифрованный nsec владельца (или None)."""
    with connect(cfg.DB) as conn:
        row = conn.execute("SELECT nsec_enc FROM mail_keys WHERE pubkey_hex=?", (pubkey_hex,)).fetchone()
    if not row:
        return None
    try:
        return _decrypt_nsec(row[0])
    except Exception:
        return None


# ── хэширование пароля ──────────────────────────────────
def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER)
    return f"{salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, hx = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER)
        return hmac.compare_digest(h.hex(), hx)
    except Exception:
        return False


# ── accounts ─────────────────────────────────────────────
def _ensure_accounts_table():
    with connect(cfg.DB) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT UNIQUE NOT NULL,
                pubkey_hex TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                label TEXT DEFAULT '',
                role TEXT DEFAULT 'user',
                created_at INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mail_keys (
                pubkey_hex TEXT PRIMARY KEY,
                nsec_enc TEXT NOT NULL,
                updated_at INTEGER
            )"""
        )
        conn.commit()
    # миграция: nsec из config.json (owners) → зашифрованное хранилище
    for _o in getattr(cfg, "OWNERS", []) or []:
        _k = _o.get("nsec_hex")
        if _k and not get_mail_key(_o.get("pubkey_hex", "")):
            try:
                save_mail_key(_o["pubkey_hex"], _k)
            except Exception:
                pass


def _account_by_pubkey(pubkey: str) -> dict | None:
    rows = query_accounts("SELECT * FROM accounts WHERE pubkey_hex=?", (pubkey,))
    return rows[0] if rows else None


def _account_by_address(address: str) -> dict | None:
    rows = query_accounts("SELECT * FROM accounts WHERE address=?", (address,))
    return rows[0] if rows else None


def _all_accounts() -> list[dict]:
    """Все аккаунты домена (для NIP-05 discovery: каждый npub резолвится)."""
    out = []
    for row in query_accounts("SELECT pubkey_hex, label FROM accounts ORDER BY created_at"):
        npub = _pubkey_to_npub(row["pubkey_hex"])
        if npub:
            out.append({"npub": npub, "pubkey_hex": row["pubkey_hex"], "label": row["label"]})
    return out


def query_accounts(sql: str, params: tuple = ()) -> list[dict]:
    with connect(cfg.DB) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _npub_to_pubkey(npub: str) -> str | None:
    """npub → hex pubkey (безопасно, через локальный декодер)."""
    npub = npub.strip().split("@")[0].strip()
    try:
        from mailbridge.mail_bridge import _npub_to_hex  # лёгкий локальный импорт
        return _npub_to_hex(npub)
    except Exception:
        return None


_CHARSET_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_decode(s: str) -> tuple[str, bytes] | None:
    """Корректный bech32 (BIP-173): проверка hrp + контрольной суммы."""
    try:
        s = s.lower()
        pos = s.rfind("1")
        if pos < 1 or pos + 7 > len(s):
            return None
        hrp = s[:pos]
        data = s[pos + 1:]
        vals = [_CHARSET_B32.find(c) for c in data]
        if any(v < 0 for v in vals):
            return None

        def _polymod(values):
            gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
            chk = 1
            for v in values:
                b = chk >> 25
                chk = ((chk & 0x1FFFFFF) << 5) ^ v
                for i in range(5):
                    if (b >> i) & 1:
                        chk ^= gen[i]
            return chk

        hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
        if _polymod(hrp_exp + vals) != 1:
            return None  # плохая контрольная сумма
        acc, bits, out = 0, 0, bytearray()
        for v in vals[:-6]:
            acc = (acc << 5) | v
            bits += 5
            if bits >= 8:
                bits -= 8
                out.append((acc >> bits) & 0xFF)
        return hrp, bytes(out)
    except Exception:
        return None


def _nsec_to_hex(nsec: str) -> str | None:
    """nsec → hex приватный ключ (32 байта).

    Принимает nsec1… (bech32) ИЛИ 64-hex (удобно для агентов, у которых
    bech32-представление повреждено, а hex-ключ валиден).
    """
    nsec = nsec.strip()
    try:
        if len(nsec) == 64 and all(c in "0123456789abcdefABCDEF" for c in nsec):
            return nsec.lower()
        hrp, payload = _bech32_decode(nsec) or ("", b"")
        if hrp != "nsec" or len(payload) != 32:
            return None
        return payload.hex()
    except Exception:
        return None


def _npub_to_pubkey(npub: str) -> str | None:
    """npub (bech32) → hex pubkey (32 байта)."""
    npub = npub.strip().split("@")[0].strip()
    try:
        hrp, payload = _bech32_decode(npub) or ("", b"")
        if hrp != "npub" or len(payload) != 32:
            return None
        return payload.hex()
    except Exception:
        return None


_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_PREFIX = "npub"


def _hex_to_bech32(hrp: str, payload_hex: str) -> str | None:
    """hex → bech32 (BIP-173) с произвольным hrp (npub/nsec)."""
    try:
        data = bytes.fromhex(payload_hex)
        acc = 0
        bits = 0
        out = []
        for b in data:
            acc = (acc << 8) | b
            bits += 8
            while bits >= 5:
                bits -= 5
                out.append(_CHARSET[(acc >> bits) & 31])
        if bits:
            out.append(_CHARSET[(acc << (5 - bits)) & 31])
        pm = _bech32_polymod(_bech32_hrp_expand(hrp) + out)
        for p in (25, 10, 5, 0):
            out.append(_CHARSET[(pm >> p) & 31])
        return hrp + "1" + "".join(out)
    except Exception:
        return None


def _hex_to_nsec(privkey_hex: str) -> str | None:
    return _hex_to_bech32("nsec", privkey_hex)


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_polymod(values: list[int]) -> int:
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= GEN[i]
    return chk


def _pubkey_to_npub(pubkey_hex: str) -> str | None:
    """hex pubkey → npub (bech32, BIP-173)."""
    try:
        data = bytes.fromhex(pubkey_hex)
        acc = 0
        bits = 0
        out = []
        for b in data:
            acc = (acc << 8) | b
            bits += 8
            while bits >= 5:
                bits -= 5
                out.append(_CHARSET[(acc >> bits) & 31])
        if bits:
            out.append(_CHARSET[(acc << (5 - bits)) & 31])
        # checksum
        def _polymod(values):
            gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
            chk = 1
            for v in values:
                b = chk >> 25
                chk = ((chk & 0x1FFFFFF) << 5) ^ v
                for i in range(5):
                    if (b >> i) & 1:
                        chk ^= gen[i]
            return chk

        hrp = [ord(c) >> 5 for c in _PREFIX] + [0] + [ord(c) & 31 for c in _PREFIX]
        pm = _polymod(hrp + [_CHARSET.index(c) for c in out] + [0] * 6) ^ 1
        for i in range(6):
            out.append(_CHARSET[(pm >> (5 * (5 - i))) & 31])
        return _PREFIX + "1" + "".join(out)
    except Exception:
        return None


def _hex_to_nsec(privkey_hex: str) -> str:
    """hex приватный ключ → nsec (bech32)."""
    def _encode(hrp, data):
        CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        acc, bits, out = 0, 0, []
        for b in data:
            acc = (acc << 8) | b
            bits += 8
            while bits >= 5:
                bits -= 5
                out.append(CHARSET[(acc >> bits) & 31])
        if bits:
            out.append(CHARSET[(acc << (5 - bits)) & 31])

        def _polymod(values):
            gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
            chk = 1
            for v in values:
                b = chk >> 25
                chk = ((chk & 0x1FFFFFF) << 5) ^ v
                for i in range(5):
                    if (b >> i) & 1:
                        chk ^= gen[i]
            return chk

        hrp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
        pm = _polymod(hrp + [CHARSET.index(c) for c in out] + [0] * 6) ^ 1
        for i in range(6):
            out.append(CHARSET[(pm >> (5 * (5 - i))) & 31])
        return "".join(out)

    b = bytes.fromhex(privkey_hex)
    if len(b) != 32:
        raise ValueError("bad key length")
    return "nsec1" + _encode("nsec", b)


# ── сессии ──────────────────────────────────────────────
def _load_sessions() -> dict[str, str]:
    try:
        with open(cfg.SESSIONS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_sessions(sessions: dict[str, str]):
    try:
        import tempfile
        d = os.path.dirname(cfg.SESSIONS_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".sess_")
        with os.fdopen(fd, "w") as f:
            json.dump(sessions, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg.SESSIONS_FILE)  # атомарно — воркеры не видят полузаписи
    except Exception:
        pass


SESSIONS: dict[str, str] = _load_sessions()  # token → owner(pubkey_hex)


def _authed(session: str | None) -> str | None:
    """Возвращает owner (pubkey_hex) сессии или None."""
    global SESSIONS
    if not session:
        return None
    owner = SESSIONS.get(session)
    if owner is not None:
        return owner
    # промах: токен мог выдать другой воркер (uvicorn --workers N) — перечитываем файл
    SESSIONS = _load_sessions()
    return SESSIONS.get(session)


def auth_error() -> JSONResponse:
    # НЕ возвращать 401: прокси v2.site превращает 401 бэкенда в 502
    # "Backend temporarily unavailable" (проверено 2026-08-28 на /upload и /api/*).
    # Фронт ловит error=="auth" и показывает экран логина.
    return JSONResponse({"ok": False, "error": "auth"})


# ── login / register / logout ────────────────────────────
def login(address: str, password: str, response: Response):
    """Вход по адресу (npub или npub@домен) + пароль."""
    pubkey = _npub_to_pubkey(address)
    if not pubkey:
        return JSONResponse({"ok": False, "error": "unknown address"})
    acc = _account_by_pubkey(pubkey)
    if not acc or not _verify_password(password, acc["password_hash"]):
        return JSONResponse({"ok": False, "error": "wrong password"})
    return _start_session(acc, response)


def login_by_nsec(nsec: str, response: Response):
    """Вход по приватному ключу (как в NostrMail): nsec → ящик → сессия. Без пароля.

    Из nsec вычисляется pubkey → ищем аккаунт → сессия. Пароль не нужен:
    владение ключом и есть аутентификация.
    """
    nsec_hex = _nsec_to_hex(nsec)
    if not nsec_hex:
        return JSONResponse({"ok": False, "error": "invalid nsec"})
    try:
        from mailbridge.mail_bridge import pubkey_from_privkey
        pubkey = pubkey_from_privkey(nsec_hex)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid nsec"})
    acc = _account_by_pubkey(pubkey)
    if not acc:
        return JSONResponse({"ok": False, "error": "нет ящика для этого ключа — сначала зарегистрируйся"})
    return _start_session(acc, response)


def _start_session(acc: dict, response: Response):
    """Общий код: токен + cookie + ответ."""
    token = secrets.token_hex(16)
    SESSIONS[token] = acc["pubkey_hex"]
    _save_sessions(SESSIONS)
    response.set_cookie(
        "snin_session", token, httponly=True, samesite="lax",
        max_age=cfg.SESSIONS_TTL, path="/",
    )
    return {"ok": True, "address": display_address(acc), "label": acc["label"], "role": acc["role"], "owner": acc["pubkey_hex"], "token": token}


def display_address(acc: dict) -> str:
    """Показываемый адрес: npub@текущий домен (старые адреса в БД остаются рабочими).

    Домен в БД мог смениться при ребрендинге; вход и приём писем по pubkey,
    поэтому identity сохраняется независимо от домена.
    """
    return f"{acc['address'].split('@')[0]}@{DOMAIN}"


def legacy_login(password: str, response: Response):
    """Обратная совместимость: старый {password} → админ (Крайтер)."""
    if password != cfg.AUTH_PASSWORD:
        return JSONResponse({"ok": False, "error": "wrong password"})
    acc = _account_by_pubkey(_seed_pubkey("cryter"))
    if not acc:
        return JSONResponse({"ok": False, "error": "seed missing"})
    token = secrets.token_hex(16)
    SESSIONS[token] = acc["pubkey_hex"]
    _save_sessions(SESSIONS)
    response.set_cookie(
        "snin_session", token, httponly=True, samesite="lax",
        max_age=cfg.SESSIONS_TTL, path="/",
    )
    return {"ok": True, "address": display_address(acc), "label": acc["label"], "role": acc["role"], "owner": acc["pubkey_hex"], "token": token}


_REG_WINDOW = {}  # час-окно -> счётчик регистраций (антиспам)


def _register_rate_ok() -> bool:
    """Не более N регистраций в час (против спама/авто-рега)."""
    import time as _t
    hour = _t.time() // 3600
    limit = int(getattr(cfg, "LIMITS", {}).get("register_limit_per_hour", 50))
    _REG_WINDOW[hour] = _REG_WINDOW.get(hour, 0) + 1
    # чистим старые окна
    for k in [k for k in _REG_WINDOW if k < hour - 1]:
        del _REG_WINDOW[k]
    return _REG_WINDOW[hour] <= limit


def register(nsec: str, password: str, label: str, response: Response):
    """Регистрация нового ящика: nsec + пароль.

    Приватный ключ нужен мосту, чтобы расшифровывать gift wrap (NIP-59) —
    поэтому регистрация по nsec, а не по npub. address = npub@домен.
    Возвращает также nsec_hex/pubkey_hex/npub — для динамического моста.
    """
    if not _register_rate_ok():
        return JSONResponse({"ok": False, "error": "too many registrations"}, status_code=429)
    nsec_hex = _nsec_to_hex(nsec)
    if not nsec_hex:
        return JSONResponse({"ok": False, "error": "invalid nsec"}, status_code=400)
    try:
        from mailbridge.mail_bridge import pubkey_from_privkey
        pubkey = pubkey_from_privkey(nsec_hex)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid nsec"}, status_code=400)
    npub = _pubkey_to_npub(pubkey)
    if not npub:
        return JSONResponse({"ok": False, "error": "invalid key"}, status_code=400)
    # пароль опционален: пустой → вход только по nsec (как в NostrMail)
    if password and len(password) < 6:
        return JSONResponse({"ok": False, "error": "password too short"}, status_code=400)
    if _account_by_pubkey(pubkey):
        return JSONResponse({"ok": False, "error": "already registered"}, status_code=409)
    address = f"{npub}@{DOMAIN}"
    with connect(cfg.DB) as conn:
        conn.execute(
            "INSERT INTO accounts (address, pubkey_hex, password_hash, label, role, created_at) VALUES (?,?,?,?,?,?)",
            (address, pubkey, _hash_password(password), label or "Пользователь", "user", int(__import__("time").time())),
        )
        conn.commit()
    save_mail_key(pubkey, nsec_hex)  # nsec — только зашифрованным, не в ответе API
    return {"ok": True, "address": address, "pubkey": pubkey, "npub": npub}


def reset_password(address: str, nsec: str, new_password: str):
    """Сброс пароля: пользователь доказывает владение ключом (nsec)."""
    address = (address or "").strip()
    nsec = (nsec or "").strip()
    if not address or not nsec:
        return JSONResponse({"ok": False, "error": "address and nsec required"}, status_code=400)
    acc = _account_by_address(address)
    if not acc:
        return JSONResponse({"ok": False, "error": "unknown address"}, status_code=404)
    if not _register_rate_ok():
        return JSONResponse({"ok": False, "error": "too many registrations"}, status_code=429)
    nsec_hex = _nsec_to_hex(nsec)
    if not nsec_hex:
        return JSONResponse({"ok": False, "error": "invalid nsec"}, status_code=400)
    try:
        from mailbridge.mail_bridge import pubkey_from_privkey
        pubkey = pubkey_from_privkey(nsec_hex)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid nsec"}, status_code=400)
    if pubkey != acc["pubkey_hex"]:
        return JSONResponse({"ok": False, "error": "key mismatch"}, status_code=403)
    if len(new_password) < 6:
        return JSONResponse({"ok": False, "error": "password too short"}, status_code=400)
    with connect(cfg.DB) as conn:
        conn.execute("UPDATE accounts SET password_hash=? WHERE pubkey_hex=?", (_hash_password(new_password), pubkey))
        conn.commit()
    return {"ok": True, "message": "password reset"}


def logout(response: Response, session: str | None):
    if session:
        SESSIONS.pop(session, None)
        _save_sessions(SESSIONS)
    response.delete_cookie("snin_session", path="/")
    return {"ok": True}


# ── seed-аккаунты (Крайтер + V2Bot) ─────────────────────
def _seed_pubkey(which: str) -> str:
    from .config import OWNERS
    for o in OWNERS:
        if o["label"].lower() == which or (which == "cryter" and o["label"] == "Крайтер"):
            return o["pubkey_hex"]
    return ""


def sync_owners_from_accounts():
    """Все зарегистрированные аккаунты → владельцы моста (метаданные без nsec).

    После рестарта новые пользователи (в accounts, но не в config.json owners)
    получают мост и возможность отправки. nsec мост берёт из mail_keys.
    """
    rows = query_accounts("SELECT pubkey_hex, address, label FROM accounts")
    for r in rows:
        if r["pubkey_hex"] not in cfg.OWNER_INDEX:
            o = {"pubkey_hex": r["pubkey_hex"], "address": r["address"], "label": r["label"], "npub": r["address"].split("@")[0]}
            cfg.OWNERS.append(o)
            cfg.OWNER_INDEX[r["pubkey_hex"]] = o


def ensure_seed_accounts(accounts_file: str):
    """Создаёт учётки Крайтера (admin) и V2Bot (user), если их нет.

    Пароли сидов хранятся в accounts_file (0o600):
      {"cryter": "BOCY6IEiFA", "v2bot": "<сгенерированный>"}
    Крайтер наследует текущий AUTH_PASSWORD — ничего не ломается.
    """
    _ensure_accounts_table()
    seeds = {"cryter": cfg.AUTH_PASSWORD, "v2bot": None}
    try:
        with open(accounts_file) as f:
            saved = json.load(f)
        if saved.get("v2bot"):
            seeds["v2bot"] = saved["v2bot"]
    except Exception:
        saved = {}

    from .config import OWNERS
    changed = False
    for o in OWNERS:
        key = "cryter" if o["label"] == "Крайтер" else (o["label"].lower() if o["label"].lower() in ("v2bot",) else "")
        if not key:
            continue
        if _account_by_pubkey(o["pubkey_hex"]):
            continue
        pw = seeds[key]
        if not pw:
            pw = secrets.token_urlsafe(12)
            saved[key] = pw
            changed = True
        with connect(cfg.DB) as conn:
            conn.execute(
                "INSERT INTO accounts (address, pubkey_hex, password_hash, label, role, created_at) VALUES (?,?,?,?,?,?)",
                (o["address"], o["pubkey_hex"], _hash_password(pw), o["label"], "admin" if key == "cryter" else "user",
                 int(__import__("time").time())),
            )
            conn.commit()
    if changed:
        with open(accounts_file, "w") as f:
            json.dump(saved, f)
        os.chmod(accounts_file, 0o600)
