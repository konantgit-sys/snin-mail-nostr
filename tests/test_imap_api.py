"""API личных IMAP-конфигов: сохранение/чтение/удаление/статус + шифрование.

Цепочка: пользователь сам подключает свой внешний IMAP-ящик через вкладку
«Входящие (IMAP)» → конфиг шифруется (AES-256-GCM) → демон доставляет
письма в ЕГО SNIN-ящик через полный Nostr-контур.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_api import client, _login  # noqa: F401,E402  (фикстуры из test_api.py)


# ── шифрование ──────────────────────────────────────────
def test_encrypt_decrypt_roundtrip(monkeypatch, tmp_path):
    import mailapp.imap_store as store
    monkeypatch.setattr(store._cfg, "NSEC", "ab" * 32)
    enc = store.encrypt_password("secret-app-pass")
    assert enc != "secret-app-pass"
    assert store.decrypt_password(enc) == "secret-app-pass"
    # разные nonce → разные шифротексты одного пароля
    assert store.encrypt_password("x") != store.encrypt_password("x")


def test_store_save_get_delete(client):
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    # не настроено
    r = client.get("/api/imap/config", headers=h)
    assert r.json()["ok"] is True and r.json()["configured"] is False

    # сохранить
    r = client.put("/api/imap/config", json={
        "host": "imap.mail.ru", "port": 993, "ssl": True,
        "user": "user@mail.ru", "app_password": "app-pass-123"}, headers=h)
    assert r.json()["ok"] is True

    # прочитать (пароль маской, has_password=true)
    r = client.get("/api/imap/config", headers=h)
    d = r.json()
    assert d["configured"] is True
    assert d["host"] == "imap.mail.ru" and d["user"] == "user@mail.ru"
    assert d["has_password"] is True
    assert "app_password" not in d

    # обновить без пароля → пароль сохраняется
    r = client.put("/api/imap/config", json={
        "host": "imap.mail.ru", "port": 143, "ssl": False, "user": "user@mail.ru"}, headers=h)
    assert r.json()["ok"] is True
    d = client.get("/api/imap/config", headers=h).json()
    assert d["port"] == 143 and d["ssl"] is False and d["has_password"] is True

    # статус
    st = client.get("/api/imap/status", headers=h).json()
    assert st["configured"] is True and st["host"] == "imap.mail.ru"

    # удалить
    r = client.delete("/api/imap/config", headers=h)
    assert r.json()["ok"] is True
    assert client.get("/api/imap/config", headers=h).json()["configured"] is False


def test_imap_requires_auth(client):
    assert client.get("/api/imap/config").json()["error"] == "auth"
    assert client.put("/api/imap/config", json={
        "host": "imap.mail.ru", "user": "u@mail.ru", "app_password": "x"
    }).json()["error"] == "auth"


def test_imap_bad_body(client):
    h = {"Authorization": "Bearer " + _login(client).json()["token"]}
    r = client.put("/api/imap/config", json={"host": "", "user": ""}, headers=h)
    assert r.status_code == 400


def test_list_configs_enabled_only(client, monkeypatch, tmp_path):
    """Демон берёт только включённые конфиги, пароль расшифрован."""
    import mailapp.imap_store as store
    monkeypatch.setattr(store._cfg, "NSEC", "cd" * 32)
    store.ensure_table()
    store.save_config("OWNER_A", "imap.x", 993, True, "a@x", "pw1", enabled=1)
    store.save_config("OWNER_B", "imap.y", 993, True, "b@y", "pw2", enabled=0)
    cfgs = store.list_configs(enabled_only=True)
    assert len(cfgs) == 1
    assert cfgs[0]["app_password"] == "pw1"
    assert cfgs[0]["owner"] == "OWNER_A"
