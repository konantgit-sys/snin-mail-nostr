"""
Nostr Mail — Blossom (NIP-96) тесты.

Покрытие:
- Внутренний upload (session) → скачивание по /media/<sha256> → содержимое совпадает.
- Внешний NIP-96 upload: /upload с Authorization: Nostr (kind 27235) → 200 + url.
- Невалидный NIP-98 auth → 401.
- Лимит размера → 413.
- DELETE /media/<sha> без владельца → 403.

Запуск: cd sites/cryter-mail && NO_BRIDGE=1 python3 -m pytest tests/test_blossom.py -v
"""

import os
import sys
import base64
import json
import time

import pytest

os.environ["NO_BRIDGE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# mailbridge (src) — как в start.sh (PYTHONPATH при запуске uvicorn)
_mb_src = os.environ.get("NOSTR_MAIL_BRIDGE_SRC", "")
if not _mb_src:
    _f = os.path.expanduser("~/data/projects/nostr-mail-bridge/src")
    _mb_src = _f if os.path.exists(_f) else ""
if _mb_src:
    sys.path.insert(0, _mb_src)
    sys.path.insert(0, os.path.join(os.path.dirname(_mb_src), "deps"))
import app as appmod  # noqa: E402
import mailapp.config as cfg  # noqa: E402
import mailapp.auth as auth  # noqa: E402
import mailapp.routers.blossom as blossom  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

PASSWORD = cfg.AUTH_PASSWORD
TEST_CONTENT = b"NIP-96 Blossom test payload \x00\x01\x02\xff" * 100


@pytest.fixture()
def db(tmp_path):
    import sqlite3
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT, sender_pubkey TEXT, from_addr TEXT, to_addr TEXT,
            subject TEXT, body TEXT, received_at INTEGER, is_read INTEGER DEFAULT 0,
            raw_event TEXT, owner TEXT DEFAULT ''
        );
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT, recipient_pubkey TEXT, subject TEXT, body TEXT,
            sent_at INTEGER, raw_event TEXT, owner TEXT DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DB", db)
    monkeypatch.setattr(cfg, "DEFAULT_OWNER", "OWNER_A")
    monkeypatch.setattr(cfg, "OWNERS", [{"nsec_hex": cfg.NSEC, "pubkey_hex": "OWNER_A", "address": "a@x", "label": "Крайтер", "npub": cfg.NPUB}])
    monkeypatch.setattr(cfg, "OWNER_INDEX", {"OWNER_A": {"nsec_hex": cfg.NSEC, "pubkey_hex": "OWNER_A", "address": "a@x", "label": "Крайтер", "npub": cfg.NPUB}})
    monkeypatch.setattr(cfg, "ACCOUNTS_FILE", str(tmp_path / "mail_accounts.json"))
    monkeypatch.setattr(cfg, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth, "SESSIONS", {})
    monkeypatch.setattr(blossom, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(blossom, "PUBLIC_BASE", "https://test.local")
    os.makedirs(str(tmp_path / "uploads"), exist_ok=True)
    with TestClient(appmod.app) as c:
        yield c


def _login(client):
    """Возвращает Authorization-заголовок (Bearer-токен) — cookie не читается."""
    return {"Authorization": "Bearer " + client.post("/api/login", json={"password": PASSWORD}).json()["token"]}


def _nip98_auth(privkey_hex: str, url: str, method: str = "POST") -> str:
    from mailbridge.nip44 import pubkey_from_privkey
    from mailbridge.nip59 import sign_event
    pub = pubkey_from_privkey(privkey_hex)
    created = int(time.time())
    eid, sig = sign_event(pub, created, 27235, [["u", url], ["method", method]], "", privkey_hex)
    ev = {"id": eid, "pubkey": pub, "created_at": created, "kind": 27235,
          "tags": [["u", url], ["method", method]], "content": "", "sig": sig}
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


# ── внутренний upload (session) ─────────────────────────

def test_internal_upload_download_roundtrip(client):
    h = _login(client)
    b64 = base64.b64encode(TEST_CONTENT).decode()
    r = client.post("/api/blossom/upload", json={"filename": "t.bin", "mime": "application/octet-stream", "data_base64": b64}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("https://test.local/media/")
    sha = data["url"].rsplit("/", 1)[1]
    assert len(sha) == 64

    r2 = client.get(f"/media/{sha}")
    assert r2.status_code == 200
    assert r2.content == TEST_CONTENT


def test_internal_upload_requires_auth(client):
    r = client.post("/api/blossom/upload", json={"data_base64": base64.b64encode(b"x").decode()})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_internal_upload_too_large(client, monkeypatch):
    h = _login(client)
    monkeypatch.setattr(blossom, "MAX_UPLOAD", 10)  # 10 байт
    r = client.post("/api/blossom/upload", json={"data_base64": base64.b64encode(b"x" * 100).decode()}, headers=h)
    assert r.status_code == 413


# ── внешний NIP-96 upload ───────────────────────────────

def test_nip96_upload_with_auth(client):
    r = client.post("/upload", content=TEST_CONTENT,
                    headers={"Content-Type": "application/octet-stream",
                             "Authorization": _nip98_auth(cfg.NSEC, "http://testserver/upload", "POST")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("https://test.local/media/")
    sha = data["url"].rsplit("/", 1)[1]
    r2 = client.get(f"/media/{sha}")
    assert r2.content == TEST_CONTENT


def test_nip96_upload_bad_auth(client):
    # без заголовка
    r = client.post("/upload", content=TEST_CONTENT)
    assert r.status_code == 401
    # мусор в заголовке
    r = client.post("/upload", content=TEST_CONTENT,
                    headers={"Authorization": "Nostr aGVsbG8="})
    assert r.status_code == 401


def test_nip96_upload_wrong_url_tag(client):
    # auth-событие подписано для другого URL
    r = client.post("/upload", content=TEST_CONTENT,
                    headers={"Authorization": _nip98_auth(cfg.NSEC, "https://evil.local/upload", "POST")})
    assert r.status_code == 401


# ── DELETE ───────────────────────────────────────────────

def test_delete_requires_owner(client):
    r = client.post("/upload", content=b"delete me",
                    headers={"Authorization": _nip98_auth(cfg.NSEC, "http://testserver/upload", "POST")})
    sha = r.json()["url"].rsplit("/", 1)[1]
    # чужой ключ
    import secp256k1
    other = secp256k1.PrivateKey()
    r = client.request("DELETE", f"/media/{sha}",
                       headers={"Authorization": _nip98_auth(other.serialize(), f"http://testserver/media/{sha}", "DELETE")})
    assert r.status_code == 403
    # владелец
    r = client.request("DELETE", f"/media/{sha}",
                       headers={"Authorization": _nip98_auth(cfg.NSEC, f"http://testserver/media/{sha}", "DELETE")})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── чанкованная загрузка (обход лимита прокси ~1 МБ) ────

def _chunks_of(data: bytes, size: int = 300 * 1024):
    """Режет данные на части и возвращает список base64."""
    return [base64.b64encode(data[i:i + size]).decode() for i in range(0, len(data), size)]


def test_chunk_upload_small_file_single_part(client):
    """total=1 — одна часть, сразу сборка."""
    h = _login(client)
    content = b"hello chunk upload"
    r = client.post("/api/blossom/upload-chunk", json={
        "filename": "a.txt", "mime": "text/plain", "total": 1, "index": 0,
        "data_base64": base64.b64encode(content).decode(),
    }, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["sha256"]
    # скачиваем и сверяем
    dl = client.get("/media/" + d["sha256"])
    assert dl.status_code == 200
    assert dl.content == content


def test_chunk_upload_multipart_roundtrip(client):
    """Файл 1.2 МБ в 5 частей — сборка, sha256 совпадает с прямым расчётом."""
    h = _login(client)
    import hashlib
    content = os.urandom(1_200_000)
    parts = _chunks_of(content)
    up_id = None
    for i, b64 in enumerate(parts):
        body = {"filename": "big.bin", "mime": "application/octet-stream",
                "total": len(parts), "index": i, "data_base64": b64}
        if up_id:
            body["upload_id"] = up_id
        r = client.post("/api/blossom/upload-chunk", json=body, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        if i < len(parts) - 1:
            assert d["ok"] is True and d["done"] is False
            up_id = up_id or d.get("upload_id")
        else:
            assert d["done"] is True or d.get("sha256")
            assert d["sha256"] == hashlib.sha256(content).hexdigest()
            dl = client.get("/media/" + d["sha256"])
            assert dl.content == content


def test_chunk_upload_chunk_too_large(client):
    h = _login(client)
    big = base64.b64encode(os.urandom(blossom.CHUNK_MAX + 1)).decode()
    r = client.post("/api/blossom/upload-chunk", json={
        "filename": "x", "mime": "application/octet-stream", "total": 1, "index": 0,
        "data_base64": big,
    }, headers=h)
    assert r.status_code == 413


def test_chunk_upload_requires_auth(client):
    r = client.post("/api/blossom/upload-chunk", json={
        "filename": "x", "mime": "application/octet-stream", "total": 1, "index": 0,
        "data_base64": base64.b64encode(b"x").decode(),
    })
    assert r.json()["ok"] is False


def test_chunk_upload_missing_part(client):
    """Если клиент пропустил часть — сборка падает с 'missing part'."""
    h = _login(client)
    c0 = base64.b64encode(b"part0").decode()
    c1 = base64.b64encode(b"part1").decode()
    r0 = client.post("/api/blossom/upload-chunk", json={
        "filename": "x", "mime": "application/octet-stream", "total": 3, "index": 0, "data_base64": c0}, headers=h)
    up_id = r0.json()["upload_id"]
    # шлём сразу последнюю (index=2), часть 1 пропущена
    r2 = client.post("/api/blossom/upload-chunk", json={
        "filename": "x", "mime": "application/octet-stream", "total": 3, "index": 2,
        "data_base64": c1, "upload_id": up_id}, headers=h)
    assert r2.status_code == 400
    assert "missing part" in r2.json()["error"]


# ── reverse-proxy: X-Forwarded-Proto ─────────────────────

def test_nip96_upload_with_xfp(client):
    """За прокси (nginx) бэкенд видит scheme=http, но клиент подписал https.
    Сервер должен строить canonical URL из X-Forwarded-Proto/Host."""
    url = "https://snin-mail.v2.site/upload"
    r = client.post("/upload", content=TEST_CONTENT,
                    headers={"Content-Type": "application/octet-stream",
                             "Authorization": _nip98_auth(cfg.NSEC, url, "POST"),
                             "X-Forwarded-Proto": "https",
                             "X-Forwarded-Host": "snin-mail.v2.site"})
    assert r.status_code == 200, r.text


def test_nip96_upload_xfp_wrong_scheme(client):
    """X-Forwarded-Proto: http при подписанном https — должно быть 401."""
    url = "https://snin-mail.v2.site/upload"
    r = client.post("/upload", content=TEST_CONTENT,
                    headers={"Content-Type": "application/octet-stream",
                             "Authorization": _nip98_auth(cfg.NSEC, url, "POST"),
                             "X-Forwarded-Proto": "http",
                             "X-Forwarded-Host": "snin-mail.v2.site"})
    assert r.status_code == 401, r.text
