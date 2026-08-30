"""Blossom (NIP-96) — серверная часть на snin-mail.v2.site.

Endpoints:
- POST /upload            — внешний NIP-96: raw body + Authorization: Nostr
                           (kind 27235, NIP-98). Проверяет подпись и теги.
- POST /api/blossom/upload — внутренний (наш веб-клиент): session-токен +
                           JSON {filename, mime, data_base64}.
- GET  /media/{sha256}    — раздача файла.
- DELETE /media/{sha256}  — удаление с NIP-98 auth (владелец файла).
- GET  /api/blossom/info  — server info (имя, лимиты, supported_nips).

Файлы хранятся в UPLOAD_DIR, имя = sha256 (без расширения), как в NIP-96.
URL файла: {PUBLIC_BASE}/media/<sha256>.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .. import config as cfg
from ..auth import _authed, auth_error
from .mail import _session_of
from mailbridge.blossom import sha256_of, AUTH_KIND

router = APIRouter()

UPLOAD_DIR = getattr(cfg, "UPLOAD_DIR", None) or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads"
)
PUBLIC_BASE = getattr(cfg, "PUBLIC_BASE", None) or "https://snin-mail.v2.site"
MAX_UPLOAD = int(getattr(cfg, "LIMITS", {}).get("blossom_max_mb", 20)) * 1024 * 1024

TMP_DIR = os.path.join(os.path.dirname(UPLOAD_DIR), "tmp")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# Внешний прокси *.v2.site режет тело запроса на ~1 МБ (413 nginx, проверено 2026-08-28).
# Куски ≤ 450 КБ raw (base64 ~600 КБ) гарантированно проходят.
CHUNK_MAX = 450 * 1024
CHUNK_MAX_B64 = CHUNK_MAX * 4 // 3
CHUNK_MAX_PARTS = 64  # 64 * 450 КБ ≈ 28 МБ — с запасом над лимитом 20 МБ


def _cleanup_stale_tmp(max_age: int = 3600):
    """Удаляет осиротевшие части (клиент прервал загрузку) старше max_age сек."""
    now = time.time()
    try:
        for name in os.listdir(TMP_DIR):
            if not name.endswith(".part"):
                continue
            p = os.path.join(TMP_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _canonical_url(request: Request) -> str:
    """Внешний URL запроса с учётом X-Forwarded-Proto/Host (за reverse-proxy).

    nginx принимает HTTPS снаружи, но к бэкенду ходит по HTTP — без этого
    бэкенд видел бы request.url со схемой http, и NIP-98 (тег u) не совпал бы
    с URL, который подписал клиент (https://...).
    """
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip()
    url = f"{scheme}://{host}{request.url.path}"
    return url.split("?")[0]


def _verify_nip98(auth_header: str, url: str, method: str) -> str | None:
    """Проверяет NIP-98 auth: Authorization: Nostr <base64(event_json)>.

    Возвращает pubkey владельца (hex) или None при невалидной подписи.
    """
    if not auth_header or not auth_header.startswith("Nostr "):
        return None
    try:
        ev = json.loads(base64.b64decode(auth_header[6:].strip()).decode())
    except Exception:
        return None
    if ev.get("kind") != AUTH_KIND:
        return None
    tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) > 1}
    if tags.get("u") != url:
        return None
    if tags.get("method") != method:
        return None
    if abs(int(time.time()) - int(ev.get("created_at", 0))) > 300:
        return None  # окно 5 минут
    from mailbridge.nip59 import verify_signature
    try:
        ok = verify_signature(ev["pubkey"], ev["id"], ev["sig"])
    except Exception:
        return None
    return ev["pubkey"] if ok else None


def _save(data: bytes, owner: str | None) -> dict:
    """Сохраняет файл, возвращает {url, sha256, size}."""
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"file too large: {len(data)} > {MAX_UPLOAD}")
    if not data:
        raise ValueError("empty upload")
    sha = sha256_of(data)
    path = os.path.join(UPLOAD_DIR, sha)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    meta = {"sha256": sha, "size": len(data), "owner": owner or "", "ts": int(time.time())}
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return {"url": f"{PUBLIC_BASE}/media/{sha}", "sha256": sha, "size": len(data)}


@router.post("/upload")
async def upload_nip96(request: Request):
    """Внешний NIP-96 upload: raw body + NIP-98 auth."""
    url = _canonical_url(request)
    auth_header = request.headers.get("authorization", "")
    owner = _verify_nip98(auth_header, url, "POST")
    if not owner:
        return JSONResponse({"ok": False, "error": "invalid NIP-98 auth"}, status_code=401)
    data = await request.body()
    if not data:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
    try:
        res = _save(data, owner)
    except ValueError as e:
        code = 413 if "too large" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    return res


@router.post("/api/blossom/upload")
async def upload_internal(req: Request):
    """Внутренний upload для нашего веб-клиента (session-токен)."""
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    try:
        body = json.loads(await req.body())
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    data_b64 = (body.get("data_base64") or "").strip()
    try:
        data = base64.b64decode(data_b64)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid base64"}, status_code=400)
    try:
        res = _save(data, me)
    except ValueError as e:
        code = 413 if "too large" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    res["filename"] = (body.get("filename") or "file")[:120]
    res["mime"] = body.get("mime") or "application/octet-stream"
    return res


@router.post("/api/blossom/upload-chunk")
async def upload_chunk(req: Request):
    """Чанкованная загрузка больших файлов (обход лимита тела прокси ~1 МБ).

    POST JSON: {filename, mime, total, index, data_base64, upload_id?}
    - Части шлются последовательно (index 0..total-1), каждая ≤ CHUNK_MAX.
    - Первая часть без upload_id → сервер создаёт id и возвращает его.
    - Последняя часть (index == total-1) → сборка, сохранение в Blossom,
      возврат {url, sha256, size, filename, mime}.
    """
    me = _authed(_session_of(req))
    if not me:
        return auth_error()
    _cleanup_stale_tmp()
    try:
        body = json.loads(await req.body())
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    try:
        total = int(body.get("total") or 0)
        index = int(body.get("index") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad total/index"}, status_code=400)
    if total < 1 or total > CHUNK_MAX_PARTS:
        return JSONResponse({"ok": False, "error": "bad total"}, status_code=400)
    if index < 0 or index >= total:
        return JSONResponse({"ok": False, "error": "bad index"}, status_code=400)
    fname = (body.get("filename") or "file")[:120]
    mime = body.get("mime") or "application/octet-stream"
    data_b64 = (body.get("data_base64") or "").strip()
    if len(data_b64) > CHUNK_MAX_B64:
        return JSONResponse({"ok": False, "error": "chunk too large"}, status_code=413)
    try:
        data = base64.b64decode(data_b64)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid base64"}, status_code=400)
    if not data:
        return JSONResponse({"ok": False, "error": "empty chunk"}, status_code=400)
    up_id = (body.get("upload_id") or "").strip()
    if not up_id:
        up_id = uuid.uuid4().hex[:16]
    if len(up_id) > 32 or not up_id.isalnum():
        return JSONResponse({"ok": False, "error": "bad upload_id"}, status_code=400)
    part_path = os.path.join(TMP_DIR, f"{up_id}_{index:03d}.part")
    with open(part_path, "wb") as f:
        f.write(data)
    if index != total - 1:
        return {"ok": True, "upload_id": up_id, "index": index, "total": total, "done": False}
    # последняя часть — собираем файл
    chunks = []
    for i in range(total):
        p = os.path.join(TMP_DIR, f"{up_id}_{i:03d}.part")
        if not os.path.exists(p):
            return JSONResponse({"ok": False, "error": f"missing part {i}"}, status_code=400)
        with open(p, "rb") as f:
            chunks.append(f.read())
    try:
        for i in range(total):
            os.remove(os.path.join(TMP_DIR, f"{up_id}_{i:03d}.part"))
    except OSError:
        pass
    data_full = b"".join(chunks)
    try:
        res = _save(data_full, me)
    except ValueError as e:
        code = 413 if "too large" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    res["ok"] = True
    res["done"] = True
    res["filename"] = fname
    res["mime"] = mime
    return res


@router.get("/media/{sha}")
def media(sha: str, response: Response):
    """Раздача файла. Имя = sha256, mime угадываем (не критично)."""
    sha = sha.lower()
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        return JSONResponse({"ok": False, "error": "bad sha256"}, status_code=400)
    path = os.path.join(UPLOAD_DIR, sha)
    if not os.path.exists(path):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    import mimetypes
    mime, _ = mimetypes.guess_type(sha)
    return FileResponse(path, media_type=mime or "application/octet-stream")


@router.delete("/media/{sha}")
def media_delete(sha: str, request: Request):
    """Удаление файла (NIP-96): владелец по NIP-98 auth."""
    sha = sha.lower()
    path = os.path.join(UPLOAD_DIR, sha)
    if not os.path.exists(path):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    url = _canonical_url(request)
    auth_header = request.headers.get("authorization", "")
    owner = _verify_nip98(auth_header, url, "DELETE")
    meta_path = path + ".json"
    meta = {}
    try:
        meta = json.load(open(meta_path))
    except Exception:
        pass
    if not owner or (meta.get("owner") and meta["owner"] != owner):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    os.remove(path)
    if os.path.exists(meta_path):
        os.remove(meta_path)
    return {"ok": True, "deleted": sha}


@router.get("/api/blossom/info")
def info():
    return {
        "name": "SNIN Mail Blossom",
        "domain": cfg.DOMAIN,
        "supported_nips": [96],
        "max_upload_mb": MAX_UPLOAD // (1024 * 1024),
        "urls": f"{PUBLIC_BASE}/media/<sha256>",
    }
