#!/usr/bin/env python3
"""SNIN Mail CLI — управление почтой из терминала.

Примеры:
  python3 scripts/mail_cli.py health
  python3 scripts/mail_cli.py list                      # входящие
  python3 scripts/mail_cli.py list --folder archive     # архив
  python3 scripts/mail_cli.py list --folder outbox      # исходящие
  python3 scripts/mail_cli.py read 42
  python3 scripts/mail_cli.py send --to npub1…@dom --subject "Привет" --body "Текст" [--attach f]
  python3 scripts/mail_cli.py draft --subject "Черновик" --body "…"
  python3 scripts/mail_cli.py draft --id 3 --delete
  python3 scripts/mail_cli.py archive 42 [--unarchive]

Аутентификация (по приоритету): --token / env MAIL_TOKEN →
кэш ~/.cache/mail_cli_token (TTL 6 дней) → логин по --password / env MAIL_PASSWORD /
auth_password из config.json (админ).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import mimetypes
import os
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

TOKEN_CACHE = os.path.expanduser("~/.cache/mail_cli_token")
TOKEN_TTL = 6 * 86400


# ── transport (инжектируемый — тесты подменяют) ──────────────────────────
def http_request(method: str, url: str, json_body=None, token: str = "", timeout: int = 30):
    """Обёртка над requests; возвращает (status, data)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.request(method, url, json=json_body, headers=headers, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
    return r.status_code, data


# ── токен ────────────────────────────────────────────────────────────────
def _read_token_cache() -> str:
    try:
        tok, exp = open(TOKEN_CACHE).read().split()
        if float(exp) > time.time():
            return tok
    except Exception:
        pass
    return ""


def _write_token_cache(token: str) -> None:
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        with open(TOKEN_CACHE, "w") as f:
            f.write(f"{token} {time.time() + TOKEN_TTL}\n")
    except Exception:
        pass


def _config_auth_password() -> str:
    """auth_password из config.json проекта (если доступен)."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        return json.load(open(os.path.join(base, "config.json"))).get("auth_password", "")
    except Exception:
        return ""


def get_token(url: str, password: str, http=None) -> tuple[int, str]:
    """Вернуть токен: кэш → env → логин. (code, token|error)"""
    http = http or http_request
    tok = os.environ.get("MAIL_TOKEN", "").strip()
    if not tok:
        tok = _read_token_cache()
    if tok:
        return 0, tok
    pw = password or os.environ.get("MAIL_PASSWORD", "") or _config_auth_password()
    if not pw:
        return 1, "нет пароля: --password / MAIL_PASSWORD / auth_password в config.json"
    code, data = http("POST", url.rstrip("/") + "/api/login", json_body={"password": pw})
    if code != 200 or not data.get("ok"):
        return 2, data.get("error", f"login failed (HTTP {code})")
    _write_token_cache(data["token"])
    return 0, data["token"]


# ── вывод ────────────────────────────────────────────────────────────────
def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _print_mail(m: dict) -> None:
    flag = "•" if m.get("is_read") else "○"
    arch = " [A]" if m.get("archived") else ""
    print(f"{m['id']:>5} {flag} {_fmt_ts(m.get('received_at') or 0)}  {m.get('from',''):<34} {m.get('subject','')[:40]}{arch}")


# ── команды ──────────────────────────────────────────────────────────────
def cmd_health(url: str, http=None) -> int:
    http = http or http_request
    code, d = http("GET", url.rstrip("/") + "/api/health")
    if code != 200:
        print(f"health: HTTP {code}")
        return 1
    print(f"ok: {d.get('ok')}  uptime: {int(d.get('uptime_s', 0)//3600)}ч "
          f"RAM: {d.get('ram_pct')}%  db: {d.get('db_size', 0)//1024}КБ "
          f"inbox: {d.get('counters', {}).get('inbox')} "
          f"outbox: {d.get('counters', {}).get('outbox')} "
          f"queue: {d.get('mail_queue', {}).get('done_1m')}/мин")
    return 0


def cmd_status(url: str, token: str, http=None) -> int:
    http = http or http_request
    code, d = http("GET", url.rstrip("/") + "/api/status", token=token)
    if code != 200 or not d.get("ok"):
        print(f"status: {d.get('error', d)}")
        return 1
    print(json.dumps(d, ensure_ascii=False, indent=2)[:800])
    return 0


def cmd_list(url: str, token: str, folder: str, http=None) -> int:
    http = http or http_request
    if folder == "outbox":
        code, d = http("GET", url.rstrip("/") + "/api/outbox", token=token)
        mails = d.get("mails", [])
        for m in mails:
            print(f"{m['id']:>5}   {_fmt_ts(m.get('sent_at') or 0)}  -> {m.get('recipient','')[:34]} {m.get('subject','')[:40]}")
        print(f"\nисходящих: {len(mails)}")
        return 0
    code, d = http("GET", url.rstrip("/") + "/api/mails?folder=" + folder, token=token)
    if code != 200 or not d.get("ok"):
        print(f"list: {d.get('error', d)}")
        return 1
    for m in d.get("mails", []):
        _print_mail(m)
    total = d.get("total", len(d.get("mails", [])))
    print(f"\n{folder}: {len(d.get('mails', []))} показано / {total} всего")
    return 0


def cmd_read(url: str, token: str, mid: int, http=None) -> int:
    http = http or http_request
    code, d = http("GET", f"{url.rstrip('/')}/api/mails/{mid}", token=token)
    if code != 200 or not d.get("ok"):
        print(f"read: {d.get('error', d)}")
        return 1
    m = d["mail"]
    print(f"#{m['id']} {m.get('subject','(без темы)')}\n"
          f"от:    {m.get('from')}\n"
          f"дата:  {_fmt_ts(m.get('received_at') or 0)}\n"
          f"вложений: {len(m.get('attachments') or [])}\n{'-'*50}\n{m.get('body','')[:4000]}")
    return 0


def _upload_attach(url: str, token: str, path: str, http) -> tuple[str | None, str | None]:
    """Загрузить файл в Blossom → (sha256, url)."""
    try:
        data = open(path, "rb").read()
    except OSError as e:
        return None, f"не удалось прочитать {path}: {e}"
    b64 = base64.b64encode(data).decode()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    code, d = http("POST", url.rstrip("/") + "/api/blossom/upload",
                   json_body={"filename": os.path.basename(path), "mime": mime, "data_base64": b64}, token=token)
    if code != 200 or not d.get("sha256"):
        return None, f"upload {path}: {d.get('error', d)}"
    return d["sha256"], d.get("url", "")


def cmd_send(url: str, token: str, to_npub: str, subject: str, body: str, attach: str, http=None) -> int:
    http = http or http_request
    attachments = []
    if attach:
        sha, furl = _upload_attach(url, token, attach, http)
        if not sha:
            print(f"send: {furl}")
            return 1
        attachments.append({"filename": os.path.basename(attach), "mime": mimetypes.guess_type(attach)[0] or "application/octet-stream", "url": furl, "sha256": sha})
    payload = {"to_npub": to_npub, "subject": subject, "body": body,
               "in_reply_to": "", "owner": "", "attachments": attachments}
    code, d = http("POST", url.rstrip("/") + "/api/send", json_body=payload, token=token)
    if code != 200 or not d.get("ok"):
        print(f"send: {d.get('error', d)}")
        return 1
    print(f"отправлено (outbox id {d.get('outbox_id', '?')})")
    return 0


def cmd_draft(url: str, token: str, did: int, subject: str, body: str, delete: bool, http=None) -> int:
    http = http or http_request
    if delete:
        code, d = http("DELETE", f"{url.rstrip('/')}/api/drafts/{did}", token=token)
        if code != 200 or not d.get("ok"):
            print(f"draft delete: {d.get('error', d)}")
            return 1
        print(f"черновик #{did} удалён")
        return 0
    payload = {"id": did, "to_addr": "", "subject": subject or "", "body": body or "", "attachments": []}
    code, d = http("POST", url.rstrip("/") + "/api/drafts", json_body=payload, token=token)
    if code != 200 or not d.get("ok"):
        print(f"draft: {d.get('error', d)}")
        return 1
    print(f"черновик сохранён (id {d.get('id', did)})")
    return 0


def cmd_archive(url: str, token: str, mid: int, unarchive: bool, http=None) -> int:
    http = http or http_request
    code, d = http("POST", f"{url.rstrip('/')}/api/mails/{mid}/archive",
                   json_body={"archived": not unarchive}, token=token)
    if code != 200 or not d.get("ok"):
        print(f"archive: {d.get('error', d)}")
        return 1
    print(f"письмо #{mid}: {'в архиве' if d.get('archived') else 'извлечено из архива'}")
    return 0


# ── main ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mail_cli", description="SNIN Mail CLI")
    p.add_argument("--url", default=os.environ.get("MAIL_URL", "http://localhost:8123"))
    p.add_argument("--password", default="")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="метрики сервиса")
    sub.add_parser("status", help="статус аккаунта")

    pl = sub.add_parser("list", help="список писем")
    pl.add_argument("--folder", choices=["inbox", "archive", "outbox"], default="inbox")

    pr = sub.add_parser("read", help="деталь письма")
    pr.add_argument("id", type=int)

    ps = sub.add_parser("send", help="отправить письмо")
    ps.add_argument("--to", dest="to_npub", required=True)
    ps.add_argument("--subject", required=True)
    ps.add_argument("--body", default="")
    ps.add_argument("--attach")

    pd = sub.add_parser("draft", help="черновик")
    pd.add_argument("--id", type=int, default=0)
    pd.add_argument("--subject", default="")
    pd.add_argument("--body", default="")
    pd.add_argument("--delete", action="store_true")

    pa = sub.add_parser("archive", help="в архив / из архива")
    pa.add_argument("id", type=int)
    pa.add_argument("--unarchive", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    url = args.url.rstrip("/")
    if args.cmd in ("health",):
        return cmd_health(url)
    code, token = get_token(url, args.password)
    if code:
        print(f"auth: {token}")
        return code
    if args.cmd == "status":
        return cmd_status(url, token)
    if args.cmd == "list":
        return cmd_list(url, token, args.folder)
    if args.cmd == "read":
        return cmd_read(url, token, args.id)
    if args.cmd == "send":
        return cmd_send(url, token, args.to_npub, args.subject, args.body, args.attach)
    if args.cmd == "draft":
        return cmd_draft(url, token, args.id, args.subject, args.body, args.delete)
    if args.cmd == "archive":
        return cmd_archive(url, token, args.id, args.unarchive)
    return 1


if __name__ == "__main__":
    sys.exit(main())
