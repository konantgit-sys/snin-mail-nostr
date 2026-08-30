"""Blossom (NIP-96) — клиент для загрузки/скачивания файлов.

Вложения писем больше не живут base64 внутри RFC 2822: файл загружается
на Blossom-сервер (наш: snin-mail.v2.site/media/<sha256>), в письме —
только ссылка. Это снимает лимит 64KB на письмо и совместимо с любым
NIP-96 клиентом (nostrmail.org, blossom-клиенты).

Серверная часть — web/mailapp/routers/blossom.py (наш /upload и /media/*).
Внешние Blossom-серверы (blossom.primal.net и др.) тоже поддерживаются.

NIP-98 auth: заголовок `Authorization: Nostr <base64(event_json)>`,
событие kind 27235 с тегами [["u", url], ["method", "POST"]].
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.request
import urllib.error

from .nip59 import sign_event

AUTH_KIND = 27235  # NIP-98 HTTP auth


def _sign_auth(privkey_hex: str, url: str, method: str = "POST") -> dict:
    """Строит NIP-98 auth-событие (kind 27235) для Blossom-запроса."""
    from .nip44 import pubkey_from_privkey
    pub = pubkey_from_privkey(privkey_hex)
    created = int(time.time())
    eid, sig = sign_event(
        pub, created, AUTH_KIND,
        [["u", url], ["method", method]],
        "", privkey_hex,
    )
    return {"id": eid, "pubkey": pub, "created_at": created, "kind": AUTH_KIND,
            "tags": [["u", url], ["method", method]], "content": "", "sig": sig}


def upload(server: str, file_bytes: bytes, mime: str, privkey_hex: str,
           timeout: float = 60.0) -> dict:
    """Загружает файл на Blossom-сервер.

    server: базовый URL (напр. "https://snin-mail.v2.site" или
            "https://blossom.primal.net"). Файл уходит на {server}/upload.
    Возвращает {"url": ..., "sha256": ...}. Кидает RuntimeError при ошибке.
    """
    server = server.rstrip("/")
    url = f"{server}/upload"
    auth_ev = _sign_auth(privkey_hex, url, "POST")
    auth_b64 = base64.b64encode(json.dumps(auth_ev).encode()).decode()
    req = urllib.request.Request(
        url, method="POST", data=file_bytes,
        headers={
            "Content-Type": mime or "application/octet-stream",
            "Authorization": f"Nostr {auth_b64}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            if r.status == 202:
                # асинхронная загрузка — ждём processing_url
                pu = data.get("processing_url")
                if pu:
                    return _poll(server, pu, timeout)
            return {"url": data.get("url"), "sha256": data.get("sha256"),
                    "size": data.get("size")}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"blossom upload {e.code}: {e.read().decode()[:200]}")


def _poll(server: str, processing_url: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(processing_url)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
                st = data.get("status")
                if st == "processed":
                    return {"url": data.get("url"), "sha256": data.get("sha256")}
                if st == "error":
                    raise RuntimeError(f"blossom processing error: {data}")
        except urllib.error.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("blossom upload timeout (processing)")


def download(url: str, timeout: float = 60.0) -> bytes:
    """Скачивает файл с Blossom-сервера по прямой ссылке."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"blossom download {e.code}: {e.read().decode()[:200]}")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
