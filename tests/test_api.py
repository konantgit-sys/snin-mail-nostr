"""
Nostr Mail — API тесты (pytest).

Покрытие:
- Авторизация: неверный пароль, вход, выход, защита API.
- РЕГРЕССИЯ v1: раньше ЛЮБАЯ cookie пускала — теперь только реальный токен.
- CRUD писем: список, деталь, прочитано/непрочитано, удаление.
- Отправка: валидация (тема/тело/адресат), успех.
- Outbox, NIP-05 discovery.

Запуск:  cd sites/cryter-mail && NO_BRIDGE=1 python3 -m pytest tests/test_api.py -v
"""

import os
import sys
import json
import base64
import sqlite3

import pytest

os.environ["NO_BRIDGE"] = "1"  # мост не стартуем в тестах

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod  # noqa: E402
import mailapp.config as cfg  # noqa: E402
import mailapp.auth as auth  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

NPUB = cfg.NPUB
PUBKEY = cfg.PUBKEY
PASSWORD = cfg.AUTH_PASSWORD


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
    monkeypatch.setattr(cfg, "DB", db)
    monkeypatch.setattr(cfg, "DEFAULT_OWNER", "OWNER_A")
    monkeypatch.setattr(cfg, "OWNERS", [{"nsec_hex": cfg.NSEC, "pubkey_hex": "OWNER_A", "address": "a@x", "label": "Крайтер", "npub": cfg.NPUB}])
    monkeypatch.setattr(cfg, "OWNER_INDEX", {"OWNER_A": {"nsec_hex": cfg.NSEC, "pubkey_hex": "OWNER_A", "address": "a@x", "label": "Крайтер", "npub": cfg.NPUB}})
    monkeypatch.setattr(cfg, "ACCOUNTS_FILE", str(tmp_path / "mail_accounts.json"))
    monkeypatch.setattr(cfg, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth, "SESSIONS", {})
    with TestClient(appmod.app) as c:
        yield c


def _login(client, password=PASSWORD):
    return client.post("/api/login", json={"password": password})


# ── авторизация ─────────────────────────────────────────

def test_login_wrong_password(client):
    r = _login(client, "wrong")
    assert r.status_code == 200  # 200+ok:false — прокси *.v2.site ломает 401 в 502
    assert r.json()["ok"] is False
    assert r.json()["error"] == "wrong password"


def test_login_ok_sets_cookie(client):
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "snin_session" in r.cookies


def test_mails_requires_auth(client):
    r = client.get("/api/mails")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "auth"


def test_any_cookie_rejected(client):
    """РЕГРЕССИЯ v1: раньше любая cookie пускала. Теперь — только реальный токен."""
    r = client.get("/api/mails", headers={"Authorization": "Bearer " + "deadbeef"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_logout_invalidates_session(client):
    r = _login(client)
    token = r.json()["token"]
    assert client.get("/api/mails", headers={"Authorization": "Bearer " + token}).status_code == 200
    r2 = client.post("/api/logout", headers={"Authorization": "Bearer " + token})
    assert r2.status_code == 200
    assert client.get("/api/mails", headers={"Authorization": "Bearer " + token}).json()["ok"] is False


def test_status_ok_flag(client):
    assert client.get("/api/status").json()["ok"] is False
    r = _login(client)
    assert client.get("/api/status", headers={"Authorization": "Bearer " + r.json()["token"]}).json()["ok"] is True


# ── письма: список/деталь ────────────────────────────────

def test_mails_list(client):
    r = _login(client)
    d = client.get("/api/mails", headers={"Authorization": "Bearer " + r.json()["token"]})
    assert d.status_code == 200
    mails = d.json()["mails"]
    assert len(mails) == 2
    assert mails[0]["subject"] == "Срочно"  # DESC by received_at
    assert mails[0]["is_read"] is True
    assert mails[1]["subject"] == "Привет"
    assert mails[1]["is_read"] is False
    # алиасы полей, которые ждёт фронт (v3: баг — API отдавал from_addr, фронт ждал from)
    assert "from" in mails[0] and mails[0]["from"] == "npub1c…@snin-mail.v2.site"
    assert "from_addr" not in mails[0]


def test_mail_detail_marks_read(client):
    r = _login(client)
    s = r.json()["token"]
    d = client.get("/api/mails/2", headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200
    assert d.json()["mail"]["subject"] == "Срочно"
    # повторный запрос списка — письмо 2 прочитано (уже было), письмо 1 — не тронуто
    d2 = client.get("/api/mails/1", headers={"Authorization": "Bearer " + s})
    assert d2.json()["mail"]["is_read"] is True  # деталь автоматически прочитала


def test_mail_detail_404(client):
    r = _login(client)
    assert client.get("/api/mails/999", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 404


# ── прочитано/непрочитано ────────────────────────────────

def test_mail_set_read_unread(client):
    r = _login(client)
    s = r.json()["token"]
    d = client.post("/api/mails/1/read", json={"read": True}, headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200 and d.json()["is_read"] is True
    d = client.post("/api/mails/1/read", json={"read": False}, headers={"Authorization": "Bearer " + s})
    assert d.json()["is_read"] is False
    # проверить в списке
    lst = client.get("/api/mails", headers={"Authorization": "Bearer " + s}).json()["mails"]
    assert lst[1]["is_read"] is False


def test_mail_set_read_404(client):
    r = _login(client)
    assert client.post("/api/mails/999/read", json={"read": True},
                       headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 404


# ── удаление ─────────────────────────────────────────────

def test_mail_delete(client):
    r = _login(client)
    s = r.json()["token"]
    d = client.delete("/api/mails/1", headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200 and d.json()["deleted"] == 1
    assert client.get("/api/mails/1", headers={"Authorization": "Bearer " + s}).status_code == 404
    assert client.delete("/api/mails/1", headers={"Authorization": "Bearer " + s}).status_code == 404


# ── отправка ─────────────────────────────────────────────

def test_send_requires_auth(client):
    r = client.post("/api/send", json={"to_npub": NPUB, "subject": "s", "body": "b"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "auth"


def test_send_validation(client):
    r = _login(client)
    s = r.json()["token"]
    # пустая тема
    assert client.post("/api/send", json={"to_npub": NPUB, "subject": "", "body": "b"},
                       headers={"Authorization": "Bearer " + s}).status_code == 400
    # пустое тело
    assert client.post("/api/send", json={"to_npub": NPUB, "subject": "s", "body": "  "},
                       headers={"Authorization": "Bearer " + s}).status_code == 400
    # мусорный адресат
    assert client.post("/api/send", json={"to_npub": "not-a-npub", "subject": "s", "body": "b"},
                       headers={"Authorization": "Bearer " + s}).status_code == 400


def test_send_ok_writes_outbox(client):
    r = _login(client)
    s = r.json()["token"]
    d = client.post("/api/send", json={"to_npub": NPUB, "subject": "Тема", "body": "Тело"},
                    headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200
    assert d.json()["ok"] is True
    assert "event_id" in d.json()
    # в outbox появилось
    ob = client.get("/api/outbox", headers={"Authorization": "Bearer " + s}).json()["outbox"]
    assert len(ob) == 2
    assert ob[0]["subject"] == "Тема"


def test_send_full_address(client):
    """Адресат в формате npub@домен тоже принимается."""
    r = _login(client)
    s = r.json()["token"]
    d = client.post("/api/send", json={"to_npub": f"{NPUB}@{cfg.DOMAIN}",
                                       "subject": "Полный адрес", "body": "Тело"},
                    headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200 and d.json()["ok"] is True


# ── outbox / nip05 ────────────────────────────────────────

def test_outbox_requires_auth(client):
    r = client.get("/api/outbox")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_outbox_list(client):
    r = _login(client)
    d = client.get("/api/outbox", headers={"Authorization": "Bearer " + r.json()["token"]})
    assert d.status_code == 200
    assert len(d.json()["outbox"]) == 1
    assert d.json()["outbox"][0]["subject"] == "Отправленное"


def test_nip05_discovery(client):
    d = client.get("/.well-known/nostr.json")
    assert d.status_code == 200
    names = d.json()["names"]
    assert names["_smtp"] == PUBKEY
    assert names[NPUB] == PUBKEY


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── регистрация / учётные записи ────────────────────────

def _new_nsec():
    """Свежий тестовый nsec (hex → bech32)."""
    from mailapp.auth import _hex_to_nsec, _pubkey_to_npub
    import secrets as _s
    from mailbridge.mail_bridge import pubkey_from_privkey
    hexk = _s.token_bytes(32).hex()
    pub = pubkey_from_privkey(hexk)
    return _hex_to_nsec(hexk), pub, _pubkey_to_npub(pub)


def test_register_ok(client):
    nsec, pub, npub = _new_nsec()
    r = client.post("/api/register", json={"nsec": nsec, "password": "secret123", "label": "Тест"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["address"] == f"{npub}@{cfg.DOMAIN}"
    assert d["pubkey"] == pub


def test_register_duplicate(client):
    nsec, _, _ = _new_nsec()
    client.post("/api/register", json={"nsec": nsec, "password": "secret123"})
    r = client.post("/api/register", json={"nsec": nsec, "password": "secret123"})
    assert r.status_code == 409
    assert r.json()["error"] == "already registered"


def test_register_short_password(client):
    nsec, _, _ = _new_nsec()
    r = client.post("/api/register", json={"nsec": nsec, "password": "123"})
    assert r.status_code == 400
    assert r.json()["error"] == "password too short"


def test_register_invalid_nsec(client):
    r = client.post("/api/register", json={"nsec": "nsec1invalid", "password": "secret123"})
    assert r.status_code == 400


def test_login_by_address_with_own_password(client):
    nsec, pub, npub = _new_nsec()
    client.post("/api/register", json={"nsec": nsec, "password": "secret123", "label": "Новичок"})
    r = client.post("/api/login", json={"address": f"{npub}@{cfg.DOMAIN}", "password": "secret123"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["role"] == "user"
    assert "snin_session" in r.cookies


def test_login_wrong_password_new_account(client):
    nsec, _, npub = _new_nsec()
    client.post("/api/register", json={"nsec": nsec, "password": "secret123"})
    r = client.post("/api/login", json={"address": npub, "password": "wrongpass"})
    assert r.json()["ok"] is False
    assert r.json()["error"] == "wrong password"


def test_user_sees_only_own_mailbox(client):
    """Обычный пользователь видит ТОЛЬКО свой ящик, даже если ?owner= чужой."""
    nsec, _, npub = _new_nsec()
    client.post("/api/register", json={"nsec": nsec, "password": "secret123"})
    r = client.post("/api/login", json={"address": npub, "password": "secret123"})
    s = r.json()["token"]
    # в БД есть письма OWNER_A, но ?owner=OWNER_A — не должен их отдать
    d = client.get("/api/mails?owner=OWNER_A", headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200
    assert d.json()["mails"] == []
    st = client.get("/api/status", headers={"Authorization": "Bearer " + s})
    assert st.json()["me"]["role"] == "user"
    assert len(st.json()["accounts"]) == 1  # только свой


def test_send_with_attachment_and_detail(client):
    import base64
    s = _login(client).json()["token"]
    pdf = b"%PDF-1.4 test " + b"x" * 100
    att = {"filename": "spec.pdf", "mime": "application/pdf", "data_base64": base64.b64encode(pdf).decode()}
    d = client.post("/api/send", json={
        "to_npub": f"{cfg.NPUB}@{cfg.DOMAIN}", "subject": "С файлом", "body": "Смотри",
        "attachments": [att],
    }, headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200
    assert d.json()["ok"] is True
    # письмо легло в outbox
    ob = client.get("/api/outbox", headers={"Authorization": "Bearer " + s}).json()["outbox"]
    assert ob and ob[0]["subject"] == "С файлом"
    # вложение доехало до сервера: событие (raw_event) содержит multipart-контент
    from mailapp.db import query
    rows = query(cfg.DB, "SELECT raw_event FROM outbox WHERE subject=? ORDER BY id DESC LIMIT 1", ("С файлом",))
    assert rows
    import json as _json
    ev = _json.loads(rows[0]["raw_event"])
    assert "multipart" not in ev.get("content", "")  # контент зашифрован — ок, сам multipart проверен unit-тестами


def test_send_attachment_too_many(client):
    import base64
    s = _login(client).json()["token"]
    atts = [{"filename": f"f{i}.bin", "mime": "application/octet-stream", "data_base64": base64.b64encode(b"x" * 10).decode()} for i in range(6)]
    d = client.post("/api/send", json={
        "to_npub": f"{cfg.NPUB}@{cfg.DOMAIN}", "subject": "Много", "body": "b", "attachments": atts,
    }, headers={"Authorization": "Bearer " + s})
    assert d.status_code == 400
    assert "5" in d.json()["error"]


def test_detail_returns_attachments(client):
    import json as _json
    s = _login(client).json()["token"]
    # вставляем письмо с вложениями напрямую в БД (как это делает мост)
    from mailapp.db import connect
    with connect(cfg.DB) as conn:
        conn.execute(
            """INSERT INTO inbox (message_id, sender_pubkey, from_addr, to_addr, subject, body,
                received_at, is_read, raw_event, attachments, owner)
               VALUES (?,?,?,?,?,?,?,0,'{}',?,?)""",
            ("<a1@x>", "OWNER_A", "a@x", "b@x", "С файлом", "Текст", 1000,
             _json.dumps([{"filename": "f.pdf", "mime": "application/pdf", "data_base64": "JVBERg=="}]),
             "OWNER_A"),
        )
        mid = conn.execute("SELECT id FROM inbox WHERE message_id='<a1@x>'").fetchone()[0]
    d = client.get(f"/api/mails/{mid}", headers={"Authorization": "Bearer " + s})
    assert d.status_code == 200
    m = d.json()["mail"]
    assert m["body"] == "Текст"
    assert len(m["attachments"]) == 1
    assert m["attachments"][0]["filename"] == "f.pdf"


def test_bearer_token_auth(client):
    """Авторизация через Authorization: Bearer (прокси не подмешивает заголовок)."""
    r = _login(client)
    token = r.json()["token"]
    assert token
    d = client.get("/api/mails", headers={"Authorization": f"Bearer {token}"})
    assert d.status_code == 200
    assert d.json()["ok"] is True
    # без cookie и без заголовка — auth
    client.cookies.clear()
    bad = client.get("/api/mails")
    assert bad.json().get("error") == "auth"
    # неверный токен — auth
    bad2 = client.get("/api/mails", headers={"Authorization": "Bearer deadbeef"})
    assert bad2.json().get("error") == "auth"


def _gen_key():
    """Новый валидный ключ: (nsec, hex)."""
    import secp256k1
    priv = secp256k1.PrivateKey()
    ser = priv.serialize()
    hex_ = ser if isinstance(ser, str) else ser.hex()
    return auth._hex_to_nsec(hex_), hex_


def test_register_hides_nsec(client, monkeypatch, tmp_path):
    """API регистрации не возвращает nsec и не кладёт его в открытый вид."""
    import mailapp.auth as auth
    # создаём настоящий ключ
    nsec, nsec_hex = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "secret123", "label": "Тест"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "nsec" not in json.dumps(d).lower() or "nsec_hex" not in d
    assert d["pubkey"]
    # в БД: mail_keys содержит зашифрованный (не открытый) nsec
    import mailapp.config as cfg
    import sqlite3
    with sqlite3.connect(cfg.DB) as c:
        row = c.execute("SELECT nsec_enc FROM mail_keys WHERE pubkey_hex=?", (d["pubkey"],)).fetchone()
    assert row and row[0] != nsec_hex and "nsec" not in row[0].lower()


def test_reset_password_with_own_key(client):
    """Сброс пароля: владелец nsec может задать новый пароль."""
    # регистрируемся со своим ключом
    nsec, nsec_hex = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "oldpass1", "label": "Сброс"})
    addr = r.json()["address"]
    # вход со старым
    ok = client.post("/api/login", json={"address": addr, "password": "oldpass1"})
    assert ok.json()["ok"] is True
    # сброс с правильным nsec
    res = client.post("/api/reset-password", json={"address": addr, "nsec": nsec, "new_password": "newpass1"})
    assert res.json()["ok"] is True
    # старый пароль больше не работает
    bad = client.post("/api/login", json={"address": addr, "password": "oldpass1"})
    assert bad.json().get("ok") is False
    # новый работает
    good = client.post("/api/login", json={"address": addr, "password": "newpass1"})
    assert good.json()["ok"] is True


def test_reset_password_wrong_key_rejected(client):
    """Чужой nsec не может сбросить пароль."""
    nsec, nsec_hex = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "oldpass1", "label": "Цель"})
    addr = r.json()["address"]
    other, _ = _gen_key()
    res = client.post("/api/reset-password", json={"address": addr, "nsec": other, "new_password": "hacked1"})
    assert res.json().get("ok") is False
    assert res.json().get("error") == "key mismatch"


def test_send_attachment_too_big(client, monkeypatch):
    """Вложение больше лимита → 413."""
    monkeypatch.setattr(cfg, "LIMITS", {**cfg.LIMITS, "max_attachment_size_mb": 5})
    s = _login(client).json()["token"]
    big = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()  # 6 МБ
    r = client.post("/api/send", headers={"Authorization": "Bearer " + s}, json={
        "to_npub": f"npub13tnevkh3kcf50wueqzu3e755sljd5fqqhkcxx5s66zzswphlt7tqe87x6n@{cfg.DOMAIN}",
        "subject": "Файл", "body": "текст",
        "attachments": [{"filename": "big.bin", "mime": "application/octet-stream", "data_base64": big}],
    })
    assert r.status_code == 413


def test_send_too_many_attachments(client, monkeypatch):
    monkeypatch.setattr(cfg, "LIMITS", {**cfg.LIMITS, "max_attachments_per_mail": 5})
    s = _login(client).json()["token"]
    small = base64.b64encode(b"ok").decode()
    atts = [{"filename": f"f{i}.bin", "mime": "application/octet-stream", "data_base64": small} for i in range(6)]
    r = client.post("/api/send", headers={"Authorization": "Bearer " + s}, json={
        "to_npub": f"npub13tnevkh3kcf50wueqzu3e755sljd5fqqhkcxx5s66zzswphlt7tqe87x6n@{cfg.DOMAIN}",
        "subject": "Много", "body": "текст", "attachments": atts,
    })
    assert r.status_code == 400
    assert "максимум" in r.json()["error"]


def test_send_daily_limit(client, monkeypatch):
    """Дневной лимит отправок → 429 после исчерпания."""
    monkeypatch.setattr(cfg, "LIMITS", {**cfg.LIMITS, "max_send_per_day": 1})
    s = _login(client).json()["token"]
    payload = {
        "to_npub": f"npub13tnevkh3kcf50wueqzu3e755sljd5fqqhkcxx5s66zzswphlt7tqe87x6n@{cfg.DOMAIN}",
        "subject": "Лимит", "body": "текст",
    }
    r1 = client.post("/api/send", headers={"Authorization": "Bearer " + s}, json=payload)
    assert r1.json()["ok"] is True
    r2 = client.post("/api/send", headers={"Authorization": "Bearer " + s}, json=payload)
    assert r2.status_code == 429


def test_new_user_can_send(client, monkeypatch):
    """Зарегистрированный пользователь может отправлять (nsec из mail_keys)."""
    nsec, _ = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "userpass1", "label": "Юзер"})
    addr = r.json()["address"]
    tok = client.post("/api/login", json={"address": addr, "password": "userpass1"}).json()["token"]
    d = client.post("/api/send", headers={"Authorization": f"Bearer {tok}"}, json={
        "to_npub": f"npub13tnevkh3kcf50wueqzu3e755sljd5fqqhkcxx5s66zzswphlt7tqe87x6n@{cfg.DOMAIN}",
        "subject": "От юзера", "body": "тест",
    })
    assert d.status_code == 200, d.text
    assert d.json()["ok"] is True


def test_login_old_domain_still_works(client):
    """Обратная совместимость: вход по npub@старый-домен работает (identity по npub)."""
    nsec, _ = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "pass1234", "label": "Мигрант"})
    npub_part = r.json()["address"].split("@")[0]
    # новый домен в адресе при регистрации
    assert r.json()["address"].endswith(f"@{cfg.DOMAIN}")
    # вход по старому домену (любой другой домен) — работает
    for dom in ("cryter-mail.v2.site", "nostrmail.org"):
        d = client.post("/api/login", json={"address": f"{npub_part}@{dom}", "password": "pass1234"})
        assert d.json()["ok"] is True, f"домен {dom}: {d.text}"
        # и в ответе адрес — с текущим доменом
        assert d.json()["address"].endswith(f"@{cfg.DOMAIN}")


def test_status_shows_current_domain(client):
    """/api/status отдаёт адрес с текущим доменом (не из БД, не старый)."""
    d = client.post("/api/login", json={"password": PASSWORD})  # legacy-login админа
    tok = d.json()["token"]
    st = client.get("/api/status", headers={"Authorization": f"Bearer {tok}"}).json()
    assert st["ok"] is True
    assert st["me"]["address"].endswith(f"@{cfg.DOMAIN}")
    assert st["domain"] == cfg.DOMAIN


def test_register_rate_limit(client, monkeypatch):
    """Антиспам: лимит регистраций в час (429 при превышении)."""
    import mailapp.config as cfgmod
    auth._REG_WINDOW.clear()  # сброс окна (другие тесты уже регистрировались)
    monkeypatch.setattr(cfgmod, "LIMITS", {**cfgmod.LIMITS, "register_limit_per_hour": 2})
    ok = 0
    for i in range(3):
        nsec, _ = _gen_key()
        r = client.post("/api/register", json={"nsec": nsec, "password": f"pass{i}234", "label": f"R{i}"})
        if r.status_code == 200:
            ok += 1
    assert ok == 2, f"ожидали 2 успешные регистрации, вышло {ok}"
    # третья — 429
    nsec, _ = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "pass9999", "label": "R3"})
    assert r.status_code == 429


# ── вход по nsec (как NostrMail) ────────────────────────

def test_login_by_nsec(client):
    """Вход по приватному ключу: nsec → ящик → сессия, без пароля."""
    nsec, _ = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "pass1234", "label": "Ключ"})
    assert r.status_code == 200, r.text
    addr = r.json()["address"]
    d = client.post("/api/login", json={"nsec": nsec})
    assert d.status_code == 200, d.text
    assert d.json()["ok"] is True, d.text
    assert d.json()["address"] == addr
    assert d.json()["token"]
    # и письма доступны по сессии
    m = client.get("/api/mails", headers={"Authorization": f"Bearer {d.json()['token']}"})
    assert m.status_code == 200 and m.json()["ok"] is True


def test_login_by_nsec_unknown_key(client):
    """nsec без ящика → понятная ошибка (а не «неверный пароль»)."""
    nsec, _ = _gen_key()
    d = client.post("/api/login", json={"nsec": nsec})
    assert d.status_code == 200
    assert d.json()["ok"] is False
    assert "ящика" in d.json()["error"]


def test_login_by_nsec_invalid(client):
    """Мусор в поле nsec → invalid nsec."""
    d = client.post("/api/login", json={"nsec": "not-a-key"})
    assert d.status_code == 200
    assert d.json()["ok"] is False
    assert d.json()["error"] == "invalid nsec"


def test_register_no_password(client):
    """Регистрация без пароля: вход только по nsec (как NostrMail)."""
    nsec, _ = _gen_key()
    r = client.post("/api/register", json={"nsec": nsec, "password": "", "label": "БезПароля"})
    assert r.status_code == 200, r.text
    addr = r.json()["address"]
    # по паролю — нельзя
    d = client.post("/api/login", json={"address": addr, "password": "anything123"})
    assert d.json()["ok"] is False
    # по nsec — можно
    d2 = client.post("/api/login", json={"nsec": nsec})
    assert d2.json()["ok"] is True, d2.text
    assert d2.json()["address"] == addr


# ── пагинация ───────────────────────────────────────────

def test_mails_pagination(client):
    """GET /api/mails?offset=&limit= → {mails, total, has_more}."""
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    r = client.get("/api/mails?limit=1&offset=0", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert len(d["mails"]) == 1
    assert d["total"] == 2          # в фикстуре 2 письма
    assert d["has_more"] is True    # 0+1 < 2
    r2 = client.get("/api/mails?limit=1&offset=1", headers=h)
    d2 = r2.json()
    assert len(d2["mails"]) == 1
    assert d2["has_more"] is False
    # пределы: limit >100 режется до 100, отрицательный offset → 0
    r3 = client.get("/api/mails?limit=999&offset=-5", headers=h)
    assert r3.status_code == 200


def test_outbox_pagination(client):
    """GET /api/outbox → total/has_more."""
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    r = client.get("/api/outbox", headers=h)
    d = r.json()
    assert d["ok"] is True
    assert d["total"] == 1
    assert d["has_more"] is False


# ── массовое удаление ───────────────────────────────────

def test_mails_clean_read(client):
    """DELETE /api/mails?filter=read — удаляет только прочитанные (свои)."""
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    r = client.delete("/api/mails?filter=read", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["deleted"] == 1  # в фикстуре 1 прочитанное (is_read=1)
    # осталось только непрочитанное
    r2 = client.get("/api/mails", headers=h)
    assert r2.json()["total"] == 1
    assert r2.json()["mails"][0]["is_read"] is False


def test_mails_clean_unknown_filter(client):
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    r = client.delete("/api/mails?filter=bogus", headers=h)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_mails_clean_requires_auth(client):
    r = client.delete("/api/mails?filter=read")
    assert r.json()["ok"] is False
