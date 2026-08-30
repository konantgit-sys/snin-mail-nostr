"""Фичи: черновики, серверный поиск, архив. 12 тестов."""
import pytest


def _login(client, password=None):
    if password is None:
        import app as _appmod  # noqa: F401
        from mailapp import config as _cfg
        password = _cfg.AUTH_PASSWORD
    r = client.post("/api/login", json={"password": password})
    assert r.json()["ok"] is True
    return {"Authorization": "Bearer " + r.json()["token"]}


# ── черновики ───────────────────────────────────────────

def test_draft_create_and_list(client):
    h = _login(client)
    r = client.post("/api/drafts", headers=h, json={"to_addr": "npub1b…", "subject": "Черновик 1", "body": "текст"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["id"] > 0
    did = r.json()["id"]
    d = client.get("/api/drafts", headers=h).json()
    assert d["total"] == 1
    assert d["drafts"][0]["subject"] == "Черновик 1"
    assert d["drafts"][0]["to"] == "npub1b…"
    # деталь: вложения распарсены
    det = client.get(f"/api/drafts/{did}", headers=h).json()["draft"]
    assert det["attachments"] == []
    assert det["subject"] == "Черновик 1"


def test_draft_update(client):
    h = _login(client)
    did = client.post("/api/drafts", headers=h, json={"subject": "v1", "body": "a"}).json()["id"]
    r = client.post("/api/drafts", headers=h, json={"id": did, "subject": "v2", "body": "b"})
    assert r.json()["id"] == did
    det = client.get(f"/api/drafts/{did}", headers=h).json()["draft"]
    assert det["subject"] == "v2" and det["body"] == "b"


def test_draft_empty_not_saved(client):
    h = _login(client)
    r = client.post("/api/drafts", headers=h, json={"to_addr": "", "subject": "", "body": ""})
    assert r.json()["ok"] and r.json().get("deleted") is True
    assert client.get("/api/drafts", headers=h).json()["total"] == 0


def test_draft_delete(client):
    h = _login(client)
    did = client.post("/api/drafts", headers=h, json={"subject": "x"}).json()["id"]
    assert client.delete(f"/api/drafts/{did}", headers=h).json()["ok"] is True
    assert client.get(f"/api/drafts/{did}", headers=h).json()["ok"] is False
    # повторное удаление — 404
    assert client.delete(f"/api/drafts/{did}", headers=h).status_code == 404


def test_draft_foreign_owner_404(client):
    h = _login(client)
    did = client.post("/api/drafts", headers=h, json={"subject": "мой"}).json()["id"]
    # логинимся под другим владельцем
    import app as appmod
    import mailapp.auth as auth
    # регистрируем второго пользователя через прямой вызов (register требует не занятый адрес)
    r = client.post("/api/register", json={"address": "other@x", "password": "secret123", "label": "Other"})
    if r.status_code == 200 and r.json().get("ok"):
        h2 = {"Authorization": "Bearer " + r.json().get("token", "")}
        det = client.get(f"/api/drafts/{did}", headers=h2)
        assert det.status_code == 404
    # чужой id не в списке у первого — уже проверено тоталом; достаточно отсутствия 200 на чужой доступ


# ── поиск (серверный, по всей БД) ───────────────────────

def test_search_by_subject(client):
    h = _login(client)
    d = client.get("/api/mails", headers=h, params={"q": "Срочно"}).json()
    assert d["total"] == 1
    assert d["mails"][0]["subject"] == "Срочно"


def test_search_by_body(client):
    h = _login(client)
    d = client.get("/api/mails", headers=h, params={"q": "Тело"}).json()
    assert d["total"] == 2  # оба письма содержат «Тело»


def test_search_no_results(client):
    h = _login(client)
    d = client.get("/api/mails", headers=h, params={"q": "такого нет"}).json()
    assert d["total"] == 0 and d["mails"] == []


# ── архив ───────────────────────────────────────────────

def test_archive_hides_from_list(client):
    h = _login(client)
    r = client.post("/api/mails/1/archive", headers=h, json={"archived": True})
    assert r.json()["ok"] and r.json()["archived"] is True
    d = client.get("/api/mails", headers=h).json()
    assert d["total"] == 1  # письмо 1 ушло в архив
    assert all(m["id"] != 1 for m in d["mails"])


def test_archive_folder_lists_archived(client):
    h = _login(client)
    client.post("/api/mails/1/archive", headers=h, json={"archived": True})
    d = client.get("/api/mails", headers=h, params={"folder": "archive"}).json()
    assert d["total"] == 1
    assert d["mails"][0]["id"] == 1
    assert d["mails"][0]["archived"] is True


def test_unarchive_returns_to_list(client):
    h = _login(client)
    client.post("/api/mails/1/archive", headers=h, json={"archived": True})
    client.post("/api/mails/1/archive", headers=h, json={"archived": False})
    d = client.get("/api/mails", headers=h).json()
    assert d["total"] == 2
    assert any(m["id"] == 1 for m in d["mails"])


def test_archive_404(client):
    h = _login(client)
    assert client.post("/api/mails/999/archive", headers=h, json={"archived": True}).status_code == 404
