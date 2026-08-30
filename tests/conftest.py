"""Conftest: пути + общие фикстуры (db, client) для ВСЕХ тестов.

db/client продублированы из test_api.py (файловые фикстуры имеют приоритет
над conftest-овскими — test_api.py продолжит использовать свои).

Самодостаточность: если в корне проекта нет config.json (свежий клон),
conftest генерирует его с ТЕСТОВЫМИ ключами — тесты работают без ручной
настройки. Боевой config.json НЕ перезаписывается.
"""
import os
import json
import secrets
import sqlite3
import sys

import pytest

_MB = os.environ.get("NOSTR_MAIL_BRIDGE_SRC", "")
_DEPS = os.path.join(os.path.dirname(_MB), "deps") if _MB else ""
_FALLBACK_MB = os.path.expanduser("~/data/projects/nostr-mail-bridge/src")
if not _MB and os.path.exists(_FALLBACK_MB):  # локальный контейнер (наш деплой)
    _MB, _DEPS = _FALLBACK_MB, os.path.expanduser("~/data/projects/nostr-mail-bridge/deps")
for _p in (_MB, _DEPS):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "config.json")

# ── самодостаточность: тестовый config.json (только если его нет) ──
def _bech32_encode(hrp: str, data: bytes) -> str:
    """BIP-173 bech32 encode (для npub из pubkey hex)."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    def _polymod(values):
        GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
        chk = 1
        for v in values:
            b = chk >> 25
            chk = (chk & 0x1ffffff) << 5 ^ v
            for i in range(5):
                chk ^= GEN[i] if ((b >> i) & 1) else 0
        return chk
    def _convertbits(data, frombits, tobits, pad=True):
        acc = 0
        bits = 0
        ret = []
        maxv = (1 << tobits) - 1
        for value in data:
            acc = (acc << frombits) | value
            bits += frombits
            while bits >= tobits:
                bits -= tobits
                ret.append((acc >> bits) & maxv)
        if pad and bits:
            ret.append((acc << (tobits - bits)) & maxv)
        return ret
    def _hrp_expand(hrp):
        return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    five = _convertbits(list(data), 8, 5)
    checksum = _polymod(_hrp_expand(hrp) + five + [0] * 6) ^ 1
    return hrp + "1" + "".join(CHARSET[d] for d in five + [(checksum >> 5 * (5 - i)) & 31 for i in range(6)])


def _ensure_test_config() -> str | None:
    """Создать config.json с тестовыми ключами, если его нет. Вернуть путь или None."""
    if os.path.exists(CONFIG_PATH):
        return None  # боевой/пользовательский конфиг — не трогаем
    nsec_hex = secrets.token_hex(32)
    try:
        from mailbridge.nip44 import pubkey_from_privkey
        pubkey_hex = pubkey_from_privkey(nsec_hex)  # парная пара — NIP-44/59 требуют соответствия
    except Exception:
        pubkey_hex = secrets.token_hex(32)
    npub = _bech32_encode("npub", bytes.fromhex(pubkey_hex))
    runtime = os.path.join(BASE, "test_runtime")
    os.makedirs(runtime, exist_ok=True)
    cfg = {
        "nsec_hex": nsec_hex,
        "pubkey_hex": pubkey_hex,
        "npub": npub,
        "mail_domain": "snin-mail.test",
        "mail_address": npub + "@snin-mail.test",
        "relays": [],
        "db": os.path.join(runtime, "inbox.db"),
        "telegram_token": "",
        "telegram_chat_id": "",
        "lightning": "",
        "auth_password": "test-password",
        "owners": [],
        "limits": {
            "max_mails_per_user": 500,
            "max_send_per_day": 100,
            "max_attachment_size_mb": 5,
            "max_attachments_per_mail": 5,
            "max_mail_body": 20000
        }
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return CONFIG_PATH


_created = _ensure_test_config()


@pytest.fixture()
def db(tmp_path):
    """Временная БД со схемой inbox/outbox + 2 письма."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT, sender_pubkey TEXT, from_addr TEXT, to_addr TEXT,
            subject TEXT, body TEXT, received_at INTEGER, is_read INTEGER DEFAULT 0,
            raw_event TEXT, attachments TEXT DEFAULT '[]', owner TEXT DEFAULT ''
        );
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT, recipient_pubkey TEXT, subject TEXT, body TEXT,
            sent_at INTEGER, raw_event TEXT, owner TEXT DEFAULT ''
        );
        INSERT INTO inbox (message_id, sender_pubkey, from_addr, to_addr, subject, body, received_at, is_read, owner)
        VALUES ('<m1@test>', 'aaa', 'npub1a…@snin-mail.v2.site', 'npub1b…@snin-mail.v2.site', 'Привет', 'Тело 1', 1000, 0, 'OWNER_A'),
               ('<m2@test>', 'bbb', 'npub1c…@snin-mail.v2.site', 'npub1b…@snin-mail.v2.site', 'Срочно', 'Тело 2', 2000, 1, 'OWNER_A');
        INSERT INTO outbox (message_id, recipient_pubkey, subject, body, sent_at, owner)
        VALUES ('<o1@test>', 'ccc', 'Отправленное', 'Тело', 1500, 'OWNER_A');
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    """Чистый клиент: временная БД, чистые сессии."""
    import app as appmod
    import mailapp.config as cfg
    import mailapp.auth as auth
    from fastapi.testclient import TestClient

    monkeypatch.setattr(cfg, "DB", db)
    monkeypatch.setattr(cfg, "DEFAULT_OWNER", "OWNER_A")
    owner = {"nsec_hex": cfg.NSEC, "pubkey_hex": "OWNER_A",
             "address": "a@x", "label": "Крайтер", "npub": cfg.NPUB}
    monkeypatch.setattr(cfg, "OWNERS", [owner])
    monkeypatch.setattr(cfg, "OWNER_INDEX", {"OWNER_A": owner})
    monkeypatch.setattr(cfg, "ACCOUNTS_FILE", str(tmp_path / "mail_accounts.json"))
    monkeypatch.setattr(cfg, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth, "SESSIONS", {})
    with TestClient(appmod.app) as c:
        yield c
