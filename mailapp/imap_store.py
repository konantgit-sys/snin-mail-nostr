"""IMAP-конфиги пользователей: таблица imap_configs + шифрование паролей.

Каждый пользователь/агент может сам подключить свой внешний IMAP-ящик
(mail.ru и др.) через клиент: письма с него автоматически доставляются
в ЕГО ящик на snin-mail.v2.site (полный Nostr-контур: IMAP → kind:1301
(NIP-59) → релеи → его мост → его inbox).

Пароль приложения шифруется AES-256-GCM: ключ = sha256(master_nsec + ":imap").
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config as _cfg


def _db() -> str:
    return _cfg.DB


def _key() -> bytes:
    return hashlib.sha256((_cfg.NSEC + ":imap:v1").encode()).digest()

TABLE = "imap_configs"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    owner             TEXT PRIMARY KEY,   -- pubkey_hex владельца ящика SNIN
    host              TEXT NOT NULL,
    port              INTEGER DEFAULT 993,
    ssl               INTEGER DEFAULT 1,
    user              TEXT NOT NULL,
    app_password_enc  TEXT NOT NULL,      -- base64(iv + tag + ct), AES-256-GCM
    enabled           INTEGER DEFAULT 1,
    last_sync         INTEGER DEFAULT 0,  -- unix ts последней успешной доставки
    last_error        TEXT DEFAULT '',
    updated_at        INTEGER DEFAULT 0
);
"""


def ensure_table() -> None:
    from .db import connect
    conn = connect(_db())
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def encrypt_password(password: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, password.encode(), b"imap")
    return base64.b64encode(nonce + ct).decode()


def decrypt_password(enc: str) -> str:
    raw = base64.b64decode(enc)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_key()).decrypt(nonce, ct, b"imap").decode()


def save_config(owner: str, host: str, port: int, ssl: bool, user: str,
                app_password: str | None, enabled: int = 1) -> None:
    """Upsert конфига. app_password=None → пароль не меняется (сохранить старый)."""
    from .db import connect
    ensure_table()
    conn = connect(_db())
    try:
        if app_password is None:
            row = conn.execute(
                "SELECT app_password_enc FROM imap_configs WHERE owner=?", (owner,)
            ).fetchone()
            if row:
                enc = row[0]
            else:
                raise ValueError("app_password обязателен при первом сохранении")
        else:
            enc = encrypt_password(app_password)
        conn.execute(
            f"""INSERT INTO imap_configs
                (owner, host, port, ssl, user, app_password_enc, enabled, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(owner) DO UPDATE SET
                  host=excluded.host, port=excluded.port, ssl=excluded.ssl,
                  user=excluded.user, app_password_enc=excluded.app_password_enc,
                  enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (owner, host, int(port or 993), 1 if ssl else 0, user, enc,
             int(enabled), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_config(owner: str) -> dict | None:
    from .db import connect, query
    ensure_table()
    rows = query(_db(), "SELECT * FROM imap_configs WHERE owner=?", (owner,))
    if not rows:
        return None
    r = rows[0]
    try:
        pw = decrypt_password(r["app_password_enc"])
    except Exception:
        pw = ""
    return {
        "owner": r["owner"], "host": r["host"], "port": r["port"],
        "ssl": bool(r["ssl"]), "user": r["user"], "app_password": pw,
        "enabled": bool(r["enabled"]), "last_sync": r["last_sync"],
        "last_error": r["last_error"], "updated_at": r["updated_at"],
    }


def list_configs(enabled_only: bool = True) -> list[dict]:
    from .db import connect, query
    ensure_table()
    sql = "SELECT * FROM imap_configs"
    if enabled_only:
        sql += " WHERE enabled=1"
    out = []
    for r in query(_db(), sql):
        try:
            pw = decrypt_password(r["app_password_enc"])
        except Exception:
            pw = ""
        out.append({
            "owner": r["owner"], "host": r["host"], "port": r["port"],
            "ssl": bool(r["ssl"]), "user": r["user"], "app_password": pw,
            "enabled": bool(r["enabled"]), "last_sync": r["last_sync"],
            "last_error": r["last_error"],
        })
    return out


def delete_config(owner: str) -> bool:
    from .db import connect
    ensure_table()
    conn = connect(_db())
    try:
        cur = conn.execute("DELETE FROM imap_configs WHERE owner=?", (owner,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def touch_sync(owner: str, ok: bool, error: str = "") -> None:
    from .db import connect
    conn = connect(_db())
    try:
        if ok:
            conn.execute(
                "UPDATE imap_configs SET last_sync=?, last_error='' WHERE owner=?",
                (int(time.time()), owner))
        else:
            conn.execute(
                "UPDATE imap_configs SET last_error=? WHERE owner=?",
                (error[:500], owner))
        conn.commit()
    finally:
        conn.close()
