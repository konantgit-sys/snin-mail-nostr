"""API-роутеры: status, mails (CRUD), send, outbox.

Каждый эндпоинт независим — можно вынести на отдельный сервер
(горизонтальное развитие), заменив только конфиг DB.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Cookie, Request, Response


def _session_of(req: Request) -> str | None:
    """Сессия: ТОЛЬКО Authorization: Bearer <token>.

    Прокси v2.site кеширует Set-Cookie приложения и подмешивает её в чужие
    запросы (проверено 2026-08-28: GET /api/mails без cookie отдавал чужой
    ящик). Поэтому cookie-авторизация на v2.site небезопасна — читаем только
    заголовок, который прокси не трогает."""
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        t = auth[7:].strip()
        if t:
            return t
    return None
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config as cfg
from ..config import MAIL_ADDR, NPUB, PUBKEY, RELAYS, LIGHTNING, DOMAIN, OWNERS, OWNER_INDEX, DEFAULT_OWNER
from ..db import connect, execute, query, query_one
from ..auth import _authed, auth_error, display_address, login as do_login, logout as do_logout, register as do_register
from .. import bridge as bridge_mod

# ── Фаза 0: кэш списков писем (TTL 5с, per-owner, сброс при записи) ──
_LIST_CACHE: dict = {}
_LIST_CACHE_TTL = 5.0


def _cache_key(kind: str, own: str, offset: int, limit: int, q: str = "", folder: str = ""):
    return (kind, own, offset, limit, q, folder)


def _cache_get(key):
    item = _LIST_CACHE.get(key)
    if not item:
        return None
    ts, rows, total, unread = item
    if time.time() - ts > _LIST_CACHE_TTL:
        _LIST_CACHE.pop(key, None)
        return None
    return rows, total, unread


def _cache_put(key, rows, total, unread):
    _LIST_CACHE[key] = (time.time(), rows, total, unread)


def _cache_invalidate(owner: str):
    dead = [k for k in list(_LIST_CACHE) if k[1] == owner]
    for k in dead:
        _LIST_CACHE.pop(k, None)


# ── Фаза 0: rate-limit входа — 20 неудачных попыток/час на адрес ──
_LOGIN_FAIL: dict = {}
_LOGIN_FAIL_LIMIT = 20


def _login_fail_check(addr: str) -> bool:
    now = time.time()
    ts_list = [t for t in _LOGIN_FAIL.get(addr, []) if now - t < 3600]
    _LOGIN_FAIL[addr] = ts_list
    return len(ts_list) >= _LOGIN_FAIL_LIMIT


def _login_fail_note(addr: str):
    now = time.time()
    lst = _LOGIN_FAIL.setdefault(addr, [])
    lst.append(now)
    _LOGIN_FAIL[addr] = [t for t in lst if now - t < 3600]


def _login_fail_reset(addr: str):
    _LOGIN_FAIL.pop(addr, None)


def upload_internal_caller(fname: str, mime: str, data_b64: str, owner: str) -> dict:
    """Загружает вложение на Blossom напрямую (без HTTP-петли).

    Вызывается из send_mail_api: файл (base64 от веб-клиента) → _save →
    {url, sha256, mime, filename}. Кидает RuntimeError при ошибке.
    """
    import base64 as _b64
    from .blossom import _save
    try:
        data = _b64.b64decode((data_b64 or "").strip())
    except Exception:
        raise RuntimeError("invalid base64")
    try:
        res = _save(data, owner)
    except ValueError as e:
        raise RuntimeError(str(e))
    res["filename"] = fname
    res["mime"] = mime
    return res

router = APIRouter()


# ── статус / NIP-05 ─────────────────────────────────────
@router.get("/api/status")
def status(req: Request):
    own = _authed(_session_of(req))
    from ..auth import _account_by_pubkey
    acc = _account_by_pubkey(own) if own else None
    is_admin = bool(acc and acc["role"] == "admin")
    accounts = [
        {"pubkey": o["pubkey_hex"], "npub": o["npub"], "address": display_address(o), "label": o["label"]}
        for o in OWNERS
    ] if is_admin else ([{
        "pubkey": acc["pubkey_hex"], "npub": cfg.NPUB if acc and acc["pubkey_hex"] == cfg.PUBKEY else "",
        "address": display_address(acc), "label": acc["label"],
    }] if acc else [])
    return {
        "ok": bool(own),
        "address": MAIL_ADDR,
        "npub": NPUB,
        "pubkey": PUBKEY,
        "domain": DOMAIN,
        "relays": RELAYS,
        "lightning": LIGHTNING,
        "accounts": accounts,
        "me": {
            "owner": acc["pubkey_hex"] if acc else "",
            "address": display_address(acc) if acc else "",
            "label": acc["label"] if acc else "",
            "role": acc["role"] if acc else "",
        },
        "default_owner": DEFAULT_OWNER,
        "auth_required": True,
        "debug": {"host": __import__("socket").gethostname(), "pid": __import__("os").getpid()},
    }


@router.get("/api/stats")
def stats():
    """Публичная статистика для дашбордов (без персональных данных писем)."""
    import sqlite3, os
    from ..config import DB
    res = {"ok": True, "domain": DOMAIN, "npub": NPUB, "address": MAIL_ADDR}
    try:
        con = sqlite3.connect(DB, timeout=5)
        cur = con.cursor()
        res["mails"] = cur.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        res["outbox"] = cur.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        res["accounts"] = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        res["unread"] = cur.execute("SELECT COUNT(*) FROM inbox WHERE is_read=0").fetchone()[0]
        res["db_size_kb"] = os.path.getsize(DB) // 1024
        con.close()
    except Exception as e:
        res["db_error"] = str(e)
    from ..auth import query_accounts
    try:
        rows = query_accounts("SELECT * FROM accounts ORDER BY rowid")
        res["mailboxes"] = [
            {"label": r.get("label", ""), "address": r.get("address", "")}
            for r in rows
        ]
    except Exception:
        pass
    return res


@router.get("/.well-known/nostr.json")
def nip05(name: str = ""):
    """NIP-05 discovery для ВСЕХ ящиков домена: _smtp (мост) + каждый npub.

    Раньше отдавался только первый владелец — адреса новых пользователей
    (друзей) не резолвились внешними клиентами (nostrmail.org и др.).
    """
    names = {"_smtp": PUBKEY, NPUB: PUBKEY}
    try:
        from ..auth import _all_accounts
        for acc in _all_accounts():
            names.setdefault(acc["npub"], acc["pubkey_hex"])
    except Exception:
        pass
    return JSONResponse({"names": names})


# ── auth ────────────────────────────────────────────────
@router.post("/api/login")
def login(body: dict, response: Response):
    nsec = (body.get("nsec") or "").strip()
    if nsec:  # вход по ключу (как NostrMail) — без пароля
        from ..auth import login_by_nsec
        return login_by_nsec(nsec, response)
    addr = (body.get("address") or "").strip()
    pw = body.get("password") or ""
    if not addr:  # обратная совместимость: старый {password} → админ
        from ..auth import legacy_login
        return legacy_login(pw, response)
    if _login_fail_check(addr):
        return JSONResponse({"ok": False, "error": "Слишком много неудачных попыток входа. Подождите час."}, status_code=429)
    res = do_login(addr, pw, response)
    if isinstance(res, dict) and res.get("ok"):
        _login_fail_reset(addr)
    else:
        _login_fail_note(addr)
    return res


def _login_token(r):
    """Вытащить токен из ответа login (для Authorization)."""
    if isinstance(r, dict) and r.get("ok"):
        return r.get("token", "")
    return ""


@router.post("/api/register")
def register(body: dict, response: Response):
    """Регистрация ящика: nsec + password (+ label). Создаёт аккаунт и мост."""
    nsec = (body.get("nsec") or "").strip()
    pw = body.get("password") or ""
    label = (body.get("label") or "").strip()
    from ..auth import register as do_register
    res = do_register(nsec, pw, label, response)
    if isinstance(res, dict) and res.get("ok"):
        o = {
            "pubkey_hex": res["pubkey"],
            "npub": res["npub"],
            "address": res["address"],
            "label": label or "Пользователь",
        }
        bridge_mod.add_owner(o)  # nsec владелец моста берёт из зашифрованного mail_keys
    return res


@router.post("/api/reset-password")
def reset_password(body: dict):
    """Сброс пароля: {address, nsec, new_password} — владение ключом."""
    from ..auth import reset_password as do_reset
    return do_reset(body.get("address", ""), body.get("nsec", ""), body.get("new_password", ""))


@router.post("/api/logout")
def logout(response: Response, req: Request):
    return do_logout(response, _session_of(req))


# ── входящие ────────────────────────────────────────────
@router.get("/api/mails")
def mails(req: Request, owner: str = "", offset: int = 0, limit: int = 100, q: str = "", folder: str = ""):
    """Список входящих. Параметры:
    - q: полнотекстовый поиск по теме/телу/отправителю (вся БД, не только загруженное)
    - folder=archive: архив (по умолчанию архивные скрыты)
    """
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    from ..auth import _account_by_pubkey
    acc = _account_by_pubkey(me)
    if acc and acc["role"] != "admin":
        own = me  # обычный пользователь видит только свой ящик
    else:
        own = owner or me
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    q = q.strip()[:120]
    folder = folder.strip()[:20]
    key = _cache_key("in", own, offset, limit, q, folder)
    cached = _cache_get(key)
    if cached is None:
        if folder == "archive":
            arch_cond = "archived=1"
        else:
            arch_cond = "(archived IS NULL OR archived=0)"
        where = f"owner=? AND {arch_cond}"
        params: list = [own]
        if q:
            like = f"%{q}%"
            where += " AND (subject LIKE ? OR body LIKE ? OR from_addr LIKE ? OR to_addr LIKE ?)"
            params += [like, like, like, like]
        total = query_one(cfg.DB, f"SELECT COUNT(*) c FROM inbox WHERE {where}", tuple(params))["c"]
        unread = query_one(cfg.DB, f"SELECT COUNT(*) c FROM inbox WHERE {where} AND is_read=0", tuple(params))["c"]
        rows = query(
            cfg.DB,
            f"SELECT id, message_id, from_addr, subject, body, received_at, is_read, archived "
            f"FROM inbox WHERE {where} ORDER BY received_at DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        )
        _cache_put(key, rows, total, unread)
    else:
        rows, total, unread = cached
    out = []
    for r in rows:
        r = dict(r)
        r["is_read"] = bool(r["is_read"])
        r["archived"] = bool(r.get("archived"))
        r["from"] = r.pop("from_addr")
        out.append(r)
    return {"ok": True, "mails": out, "total": total, "unread": unread, "has_more": offset + len(out) < total}


@router.get("/api/mails/{mid}")
def mail_detail(mid: int, req: Request):
    if not _authed(_session_of(req)):
        return auth_error()
    with connect(cfg.DB) as conn:
        row0 = conn.execute("SELECT owner FROM inbox WHERE id=?", (mid,)).fetchone()
        if not row0:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        _cache_invalidate(row0["owner"])
        cur = conn.execute("UPDATE inbox SET is_read=1 WHERE id=?", (mid,))
        row = conn.execute(
            "SELECT id, message_id, sender_pubkey, from_addr, to_addr, subject, body, received_at, is_read, attachments "
            "FROM inbox WHERE id=?", (mid,)
        ).fetchone()
    m = dict(row)
    m["is_read"] = bool(m["is_read"])
    m["from"] = m.pop("from_addr")
    m["to"] = m.pop("to_addr")
    try:
        raw_atts = m.pop("attachments") or "[]"
        m["attachments"] = json.loads(raw_atts)
    except Exception:
        m["attachments"] = []
    return {"ok": True, "mail": m}


@router.post("/api/mails/{mid}/read")
def mail_set_read(mid: int, body: dict, req: Request):
    """Отметить письмо прочитанным/непрочитанным: {"read": true|false}"""
    if not _authed(_session_of(req)):
        return auth_error()
    read = bool(body.get("read", True))
    owner_row = query_one(cfg.DB, "SELECT owner FROM inbox WHERE id=?", (mid,))
    if not owner_row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    n = execute(cfg.DB, "UPDATE inbox SET is_read=? WHERE id=?", (1 if read else 0, mid))
    if n == 0:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    _cache_invalidate(owner_row["owner"])
    return {"ok": True, "id": mid, "is_read": read}


@router.delete("/api/mails")
def mails_clean(req: Request, filter: str = ""):
    """Массовое удаление: DELETE /api/mails?filter=read — все прочитанные своего ящика."""
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    if filter == "read":
        n = execute(cfg.DB, "DELETE FROM inbox WHERE owner=? AND is_read=1", (me,))
        _cache_invalidate(me)
        return {"ok": True, "deleted": n, "filter": "read"}
    return JSONResponse({"ok": False, "error": "unknown filter"}, status_code=400)


@router.delete("/api/mails/{mid}")
def mail_delete(mid: int, req: Request):
    if not _authed(_session_of(req)):
        return auth_error()
    owner_row = query_one(cfg.DB, "SELECT owner FROM inbox WHERE id=?", (mid,))
    if not owner_row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    n = execute(cfg.DB, "DELETE FROM inbox WHERE id=?", (mid,))
    if n == 0:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    _cache_invalidate(owner_row["owner"])
    return {"ok": True, "deleted": mid}


@router.post("/api/mails/{mid}/archive")
def mail_archive(mid: int, body: dict, req: Request):
    """В архив / из архива: POST /api/mails/{mid}/archive {"archived": true|false}."""
    if not _authed(_session_of(req)):
        return auth_error()
    row = query_one(cfg.DB, "SELECT owner, archived FROM inbox WHERE id=?", (mid,))
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    target = 1 if body.get("archived", True) else 0
    execute(cfg.DB, "UPDATE inbox SET archived=? WHERE id=?", (target, mid))
    _cache_invalidate(row["owner"])
    return {"ok": True, "archived": bool(target)}


# ── черновики (сохранение композера) ─────────────────────
class DraftBody(BaseModel):
    id: int = 0
    to_addr: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[dict] = []


@router.post("/api/drafts")
def draft_save(body: DraftBody, req: Request):
    """Сохранить черновик: POST /api/drafts. Без id — создать, с id — обновить."""
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    to_addr = (body.to_addr or "").strip()[:320]
    subject = (body.subject or "").strip()[:200]
    text = (body.body or "").strip()
    if not to_addr and not subject and not text:
        # пустой композер — не храним
        if body.id:
            execute(cfg.DB, "DELETE FROM drafts WHERE id=? AND owner=?", (body.id, me))
        return {"ok": True, "id": 0, "deleted": True}
    atts = json.dumps(body.attachments or [], ensure_ascii=False)
    now = int(time.time())
    if body.id:
        cur = query_one(cfg.DB, "SELECT id FROM drafts WHERE id=? AND owner=?", (body.id, me))
        if not cur:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        execute(
            cfg.DB,
            "UPDATE drafts SET to_addr=?, subject=?, body=?, attachments=?, updated_at=? WHERE id=?",
            (to_addr, subject, text, atts, now, body.id),
        )
        return {"ok": True, "id": body.id}
    cur = execute(
        cfg.DB,
        "INSERT INTO drafts (owner, to_addr, subject, body, attachments, updated_at) VALUES (?,?,?,?,?,?)",
        (me, to_addr, subject, text, atts, now),
    )
    return {"ok": True, "id": cur}


@router.get("/api/drafts")
def drafts_list(req: Request, owner: str = "", offset: int = 0, limit: int = 100):
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    from ..auth import _account_by_pubkey
    acc = _account_by_pubkey(me)
    own = me if not (acc and acc["role"] != "admin") else (owner or me)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    total = query_one(cfg.DB, "SELECT COUNT(*) c FROM drafts WHERE owner=?", (own,))["c"]
    rows = query(
        cfg.DB,
        "SELECT id, to_addr, subject, body, updated_at FROM drafts "
        "WHERE owner=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (own, limit, offset),
    )
    for r in rows:
        r["to"] = r.pop("to_addr")
    return {"ok": True, "drafts": rows, "total": total, "has_more": offset + len(rows) < total}


@router.get("/api/drafts/{did}")
def draft_detail(did: int, req: Request):
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    row = query_one(
        cfg.DB, "SELECT id, to_addr, subject, body, attachments, updated_at FROM drafts WHERE id=? AND owner=?",
        (did, me),
    )
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        row["attachments"] = json.loads(row.get("attachments") or "[]")
    except Exception:
        row["attachments"] = []
    row["to"] = row.pop("to_addr")
    return {"ok": True, "draft": row}


@router.delete("/api/drafts/{did}")
def draft_delete(did: int, req: Request):
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    n = execute(cfg.DB, "DELETE FROM drafts WHERE id=? AND owner=?", (did, me))
    if n == 0:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "deleted": did}


# ── отправка / исходящие ────────────────────────────────
class SendBody(BaseModel):
    to_npub: str
    subject: str
    body: str
    in_reply_to: str = ""
    owner: str = ""
    attachments: list[dict] = []


def _parse_recipient(to_npub: str) -> str | None:
    """Принимает npub или полный адрес npub@домен → hex pubkey."""
    to = to_npub.strip()
    if not to:
        return None
    if "@" in to:
        to = to.split("@")[0].strip()
    from mailbridge.mail_bridge import _npub_to_hex  # local import
    return _npub_to_hex(to)


@router.post("/api/send")
def send_mail_api(body: SendBody, req: Request):
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    try:
        subject = body.subject.strip()
        mail_body = body.body.strip()
        if not subject:
            return JSONResponse({"ok": False, "error": "пустая тема"}, status_code=400)
        if len(subject) > 200:
            return JSONResponse({"ok": False, "error": "тема слишком длинная (макс 200)"}, status_code=400)
        if not mail_body:
            return JSONResponse({"ok": False, "error": "пустое письмо"}, status_code=400)
        if len(mail_body) > 20000:
            return JSONResponse({"ok": False, "error": "письмо слишком длинное (макс 20000)"}, status_code=400)
        to_pub = _parse_recipient(body.to_npub)
        if not to_pub:
            return JSONResponse({"ok": False, "error": "не удалось распознать npub адресата"}, status_code=400)
        to_npub = body.to_npub.strip()
        if "@" in to_npub:
            to_npub = to_npub.split("@")[0].strip()

        from mailbridge.nip44 import pubkey_from_privkey  # noqa: F401
        from mailbridge.nip59 import wrap_mail  # noqa: F401
        from mailbridge.mail_message import build_mail, parse_mail, MAIL_KIND  # noqa: F401

        from ..auth import _account_by_pubkey as _acc, get_mail_key
        _a = _acc(me)
        own = (body.owner or me) if (_a and _a["role"] == "admin") else me
        owner_info = cfg.OWNER_INDEX.get(own)
        if owner_info and not owner_info.get("nsec_hex"):
            # владелец есть в индексе, но nsec не в config (новый юзер) — из mail_keys
            owner_info = {**owner_info, "nsec_hex": get_mail_key(own)}
        if not owner_info:
            # после рестарта юзера нет в config owners — из accounts + mail_keys
            _acc_row = _acc(own)
            if _acc_row:
                owner_info = {
                    "pubkey_hex": own,
                    "address": _acc_row["address"],
                    "label": _acc_row["label"],
                    "npub": _acc_row["address"].split("@")[0],
                    "nsec_hex": get_mail_key(own),
                }
        if not owner_info or not owner_info.get("nsec_hex"):
            return JSONResponse({"ok": False, "error": "unknown owner"}, status_code=400)

        # вложения: базовые проверки (количество, размер), затем Blossom-upload
        atts = []
        total_b64 = 0
        for a in body.attachments or []:
            fname = (a.get("filename") or "")[:120]
            data = (a.get("data_base64") or "")
            if not fname and not data:
                continue
            if fname and data:
                total_b64 += len(data)
                max_b64 = cfg.LIMITS["max_attachment_size_mb"] * 1024 * 1024 * 4 // 3  # base64-размер
                if len(data) > max_b64:
                    return JSONResponse(
                        {"ok": False, "error": f"вложение {fname} больше лимита {cfg.LIMITS['max_attachment_size_mb']} МБ"},
                        status_code=413,
                    )
                # NIP-96: файл → Blossom (наш /api/blossom/upload), в письме — ссылка
                try:
                    up = upload_internal_caller(fname, a.get("mime") or "application/octet-stream", data, own)
                except RuntimeError as e:
                    return JSONResponse({"ok": False, "error": f"Blossom upload: {e}"}, status_code=502)
                atts.append({"filename": fname, "mime": up.get("mime"),
                             "url": up.get("url"), "sha256": up.get("sha256")})
            elif fname and a.get("url"):
                # уже загружено (клиент загрузил сам) — пропускаем проверку размера
                atts.append({"filename": fname, "mime": a.get("mime") or "application/octet-stream",
                             "url": a.get("url"), "sha256": a.get("sha256", "")})
        if len(atts) > cfg.LIMITS["max_attachments_per_mail"]:
            return JSONResponse({"ok": False, "error": f"максимум {cfg.LIMITS['max_attachments_per_mail']} вложений"}, status_code=400)

        # дневной лимит отправок
        day_start = int(time.time()) - int(time.time()) % 86400
        with connect(cfg.DB) as conn:
            sent_today = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE owner=? AND sent_at>=?", (own, day_start)
            ).fetchone()[0]
        if sent_today >= cfg.LIMITS["max_send_per_day"]:
            return JSONResponse(
                {"ok": False, "error": f"дневной лимит отправки исчерпан ({cfg.LIMITS['max_send_per_day']})"},
                status_code=429,
            )

        from_addr = f"{owner_info['address'].split('@')[0]}@{cfg.DOMAIN}"
        mail_text = build_mail(from_addr, to_npub, subject, mail_body,
                               in_reply_to=body.in_reply_to or None, attachments=atts)
        gw = wrap_mail(owner_info["nsec_hex"], to_pub, MAIL_KIND, mail_text, [["p", to_pub]])
        br = bridge_mod.get_bridge(own)
        if br:
            accepted = br.publish(gw)
        else:
            # мост в отдельном процессе — воркер публикует сам (published=0 починено)
            accepted = bridge_mod.publish_direct(gw)
        with connect(cfg.DB) as conn:
            conn.execute(
                "INSERT INTO outbox (message_id, recipient_pubkey, subject, body, sent_at, raw_event, owner) "
                "VALUES (?,?,?,?,?,?,?)",
                (parse_mail(mail_text)["message_id"], to_pub, subject, mail_body,
                 int(time.time()), json.dumps(gw, ensure_ascii=False), own),
            )
            _cache_invalidate(own)
        return {"ok": True, "published": len(accepted), "event_id": gw["id"][:16]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/outbox")
def outbox(req: Request, owner: str = "", offset: int = 0, limit: int = 100):
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    from ..auth import _account_by_pubkey
    acc = _account_by_pubkey(me)
    if acc and acc["role"] != "admin":
        own = me
    else:
        own = owner or me
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    key = _cache_key("out", own, offset, limit)
    cached = _cache_get(key)
    if cached is None:
        total = query_one(cfg.DB, "SELECT COUNT(*) c FROM outbox WHERE owner=?", (own,))["c"]
        rows = query(
            cfg.DB,
            "SELECT id, message_id, recipient_pubkey, subject, body, sent_at FROM outbox "
            "WHERE owner=? ORDER BY sent_at DESC LIMIT ? OFFSET ?",
            (own, limit, offset),
        )
        _cache_put(key, rows, total, 0)
    else:
        rows, total = cached
    out = []
    for r in rows:
        r = dict(r)
        r["to"] = r.pop("recipient_pubkey")
        out.append(r)
    return {"ok": True, "outbox": out, "total": total, "has_more": offset + len(out) < total}
