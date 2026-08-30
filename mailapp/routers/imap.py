"""API: личные IMAP-конфиги пользователей (каждый сам подключает свой ящик).

GET    /api/imap/config   — конфиг текущего пользователя (пароль маской)
PUT    /api/imap/config   — сохранить/обновить {host, port, ssl, user, app_password?}
DELETE /api/imap/config   — отключить (удалить)
GET    /api/imap/status   — {enabled, last_sync, last_error}

Цепочка доставки: IMAP fetch → kind:1301 (NIP-59, подпись ключом ВЛАДЕЛЬЦА,
p-тег = его pubkey) → релеи → мост владельца → его inbox.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..auth import _authed, auth_error
from ..routers.mail import _session_of
from .. import imap_store as store

router = APIRouter(prefix="/api/imap", tags=["imap"])


class ImapConfigIn(BaseModel):
    host: str
    port: int | None = 993
    ssl: bool | None = True
    user: str
    app_password: str | None = None
    enabled: bool | None = True


def _owner(req: Request) -> str | None:
    return _authed(_session_of(req))


@router.get("/config")
def get_config(req: Request):
    owner = _owner(req)
    if not owner:
        return auth_error()
    c = store.get_config(owner)
    if not c:
        return {"ok": True, "configured": False}
    return {
        "ok": True, "configured": True,
        "host": c["host"], "port": c["port"], "ssl": c["ssl"], "user": c["user"],
        "has_password": bool(c["app_password"]),
        "enabled": c["enabled"], "last_sync": c["last_sync"],
        "last_error": c["last_error"],
    }


@router.put("/config")
def put_config(body: ImapConfigIn, req: Request):
    owner = _owner(req)
    if not owner:
        return auth_error()
    if not body.host.strip() or not body.user.strip():
        return JSONResponse({"ok": False, "error": "host и user обязательны"}, status_code=400)
    try:
        store.save_config(owner, body.host.strip(), body.port or 993,
                          bool(body.ssl if body.ssl is not None else True),
                          body.user.strip(), body.app_password,
                          int(body.enabled if body.enabled is not None else True))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "message": "IMAP-конфиг сохранён. Демон начнёт доставлять письма в течение минуты."}


@router.delete("/config")
def delete_config(req: Request):
    owner = _owner(req)
    if not owner:
        return auth_error()
    store.delete_config(owner)
    return {"ok": True, "message": "IMAP-ящик отключён"}


@router.get("/status")
def status(req: Request):
    owner = _owner(req)
    if not owner:
        return auth_error()
    c = store.get_config(owner)
    if not c:
        return {"ok": True, "configured": False}
    return {
        "ok": True, "configured": True, "enabled": c["enabled"],
        "last_sync": c["last_sync"], "last_error": c["last_error"],
        "user": c["user"], "host": c["host"],
    }
