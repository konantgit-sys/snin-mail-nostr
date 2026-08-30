"""Тесты SNIN Mail CLI: парсинг, токен, команды (transport замокан).

Запуск: python3 -m pytest tests/test_cli.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import mail_cli as cli  # noqa: E402


@pytest.fixture(autouse=True)
def _no_token_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TOKEN_CACHE", str(tmp_path / "tok"))
    monkeypatch.delenv("MAIL_TOKEN", raising=False)
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)


def _fake_http(routes):
    """routes: {method:path: (status, json)}; возвращает transport-функцию."""
    calls = []

    def http(method, url, json_body=None, token="", timeout=30):
        calls.append((method, url, json_body, token))
        for key, resp in routes.items():
            m, path = key.split(" ", 1)
            if m.lower() == method.lower() and url.endswith(path):
                return resp[0], resp[1]
        return 404, {"ok": False, "error": f"unmocked {method} {url}"}

    http.calls = calls
    return http


# ── парсинг ─────────────────────────────────────────────
def test_parser_health():
    args = cli.build_parser().parse_args(["health"])
    assert args.cmd == "health"


def test_parser_send_requires_to_and_subject():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["send", "--body", "x"])


def test_parser_list_folder_choices():
    args = cli.build_parser().parse_args(["list", "--folder", "archive"])
    assert args.folder == "archive"


def test_parser_archive_unarchive():
    args = cli.build_parser().parse_args(["archive", "7", "--unarchive"])
    assert args.id == 7 and args.unarchive is True


# ── токен ────────────────────────────────────────────────
def test_get_token_from_env():
    os.environ["MAIL_TOKEN"] = "tok123"
    code, tok = cli.get_token("http://x", "")
    assert (code, tok) == (0, "tok123")


def test_get_token_logs_in_and_caches():
    http = _fake_http({"POST /api/login": (200, {"ok": True, "token": "newtok"})})
    code, tok = cli.get_token("http://x", "pw", http=http)
    assert (code, tok) == (0, "newtok")
    assert os.path.exists(cli.TOKEN_CACHE)
    # повторный вызов берёт из кэша, login больше не зовётся
    code2, tok2 = cli.get_token("http://x", "pw", http=http)
    assert tok2 == "newtok"
    assert len([c for c in http.calls if c[1].endswith("/api/login")]) == 1


def test_get_token_no_password(monkeypatch):
    monkeypatch.setattr(cli, "_config_auth_password", lambda: "")
    code, err = cli.get_token("http://x", "")
    assert code != 0 and "пароля" in err


def test_get_token_wrong_password():
    http = _fake_http({"POST /api/login": (200, {"ok": False, "error": "wrong password"})})
    code, err = cli.get_token("http://x", "bad", http=http)
    assert code == 2 and err == "wrong password"


# ── команды ─────────────────────────────────────────────
def test_health_ok(capsys):
    http = _fake_http({"GET /api/health": (200, {"ok": True, "uptime_s": 3600, "ram_pct": 40.0,
                                                  "db_size": 1024, "counters": {"inbox": 3, "outbox": 59},
                                                  "mail_queue": {"done_1m": 0}})})
    assert cli.cmd_health("http://x", http=http) == 0
    out = capsys.readouterr().out
    assert "ok: True" in out and "inbox: 3" in out


def test_health_down():
    http = _fake_http({"GET /api/health": (500, {})})
    assert cli.cmd_health("http://x", http=http) == 1


def test_list_inbox(capsys):
    http = _fake_http({"GET /api/mails?folder=inbox": (200, {"ok": True, "total": 2, "mails": [
        {"id": 2, "subject": "Срочно", "from": "a@x", "received_at": 2000, "is_read": True, "archived": False},
        {"id": 1, "subject": "Привет", "from": "b@x", "received_at": 1000, "is_read": False, "archived": False}]})})
    assert cli.cmd_list("http://x", "t", "inbox", http=http) == 0
    out = capsys.readouterr().out
    assert "Срочно" in out and "Привет" in out and "2 показано / 2" in out


def test_list_archive_shows_flag(capsys):
    http = _fake_http({"GET /api/mails?folder=archive": (200, {"ok": True, "total": 1, "mails": [
        {"id": 5, "subject": "Архив", "from": "c@x", "received_at": 3000, "is_read": True, "archived": True}]})})
    assert cli.cmd_list("http://x", "t", "archive", http=http) == 0
    assert "[A]" in capsys.readouterr().out


def test_list_outbox(capsys):
    http = _fake_http({"GET /api/outbox": (200, {"ok": True, "mails": [
        {"id": 9, "subject": "Отправлено", "recipient": "npub1…@d", "sent_at": 4000}]})})
    assert cli.cmd_list("http://x", "t", "outbox", http=http) == 0
    assert "Отправлено" in capsys.readouterr().out


def test_read_ok(capsys):
    http = _fake_http({"GET /api/mails/42": (200, {"ok": True, "mail": {
        "id": 42, "subject": "Тема", "from": "a@x", "received_at": 5000, "attachments": [], "body": "Тело письма"}})})
    assert cli.cmd_read("http://x", "t", 42, http=http) == 0
    out = capsys.readouterr().out
    assert "#42" in out and "Тело письма" in out


def test_read_404():
    http = _fake_http({"GET /api/mails/999": (404, {"ok": False, "error": "not found"})})
    assert cli.cmd_read("http://x", "t", 999, http=http) == 1


def test_send_without_attach(capsys):
    http = _fake_http({"POST /api/send": (200, {"ok": True, "outbox_id": 77})})
    assert cli.cmd_send("http://x", "t", "npub1…@d", "Привет", "Тело", None, http=http) == 0
    out = capsys.readouterr().out
    assert "77" in out
    post = [c for c in http.calls if c[1].endswith("/api/send")][0]
    body = post[2]
    assert body["to_npub"] == "npub1…@d" and body["subject"] == "Привет" and body["attachments"] == []


def test_send_with_attach_uploads_first(capsys, tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    routes = {
        "POST /api/blossom/upload": (200, {"ok": True, "sha256": "ab" * 32, "url": "https://x/media/" + "ab" * 32}),
        "POST /api/send": (200, {"ok": True, "outbox_id": 78}),
    }
    http = _fake_http(routes)
    assert cli.cmd_send("http://x", "t", "npub1…@d", "S", "B", str(f), http=http) == 0
    post = [c for c in http.calls if c[1].endswith("/api/send")][0]
    assert post[2]["attachments"][0]["sha256"] == "ab" * 32
    assert post[2]["attachments"][0]["filename"] == "pic.png"


def test_draft_save(capsys):
    http = _fake_http({"POST /api/drafts": (200, {"ok": True, "id": 3})})
    assert cli.cmd_draft("http://x", "t", 0, "Черновик", "Тело", False, http=http) == 0
    body = [c for c in http.calls if c[1].endswith("/api/drafts")][0][2]
    assert body["subject"] == "Черновик"
    assert "3" in capsys.readouterr().out


def test_draft_delete(capsys):
    http = _fake_http({"DELETE /api/drafts/3": (200, {"ok": True})})
    assert cli.cmd_draft("http://x", "t", 3, "", "", True, http=http) == 0
    assert "удалён" in capsys.readouterr().out


def test_archive(capsys):
    http = _fake_http({"POST /api/mails/7/archive": (200, {"ok": True, "archived": True})})
    assert cli.cmd_archive("http://x", "t", 7, False, http=http) == 0
    req = [c for c in http.calls if "archive" in c[1]][0]
    assert req[2] == {"archived": True}
    assert "в архиве" in capsys.readouterr().out


def test_unarchive(capsys):
    http = _fake_http({"POST /api/mails/7/archive": (200, {"ok": True, "archived": False})})
    assert cli.cmd_archive("http://x", "t", 7, True, http=http) == 0
    assert "извлечено" in capsys.readouterr().out


# ── main (сквозной) ─────────────────────────────────────
def test_main_health_flow(monkeypatch, capsys):
    http = _fake_http({"GET /api/health": (200, {"ok": True, "uptime_s": 1, "ram_pct": 1.0, "db_size": 1,
                                                  "counters": {"inbox": 1}, "mail_queue": {"done_1m": 0}})})
    monkeypatch.setattr(cli, "http_request", http)
    assert cli.main(["health"]) == 0
    assert "ok: True" in capsys.readouterr().out
