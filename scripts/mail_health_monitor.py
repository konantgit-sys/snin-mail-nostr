#!/usr/bin/env python3
"""Монитор здоровья SNIN Mail (Фаза 5): алерт в Octopus при деградации.

Каждые 5 минут (cron) проверяет /api/health и ресурсы пода:
- ok != true                      → сервис не отвечает
- mail_queue.pending >= 50        → очередь накапливается (воркер не успевает)
- mail_queue.workers_alive == 0   → воркер расшифровки мёртв
- ram_pct >= 93                   → RAM на пределе (cgroup memory.current/max)
- диск data >= 85%                → df -h (по правилам контейнера)

Алерт: только Octopus-бот (octopus_bot_token.txt), группа -1003797670859.
Дедуп: алерт при ПЕРЕХОДЕ в bad, повторный раз в 60 мин (sticky), сообщение
«восстановлено» при возврате в ok. Лог: ~/data/backups/monitor/monitor.log
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("MAIL_BASE", os.path.expanduser("~/data"))  # env-переопределяемый
HEALTH_URL = "http://localhost:8123/api/health"
TOKEN_FILE = os.path.join(BASE, "octopus_bot_token.txt")
CHAT_ID = "-1003797670859"  # группа Крайтера Octopus
STATE_FILE = os.path.join(BASE, "backups", "monitor", "state.json")
LOG_FILE = os.path.join(BASE, "backups", "monitor", "monitor.log")
REALERT_SEC = 3600  # повторный алерт, если плохо держится > 1 часа

PENDING_LIMIT = 50
RAM_PCT_LIMIT = 93.0
DISK_PCT_LIMIT = 85.0


def log(msg: str):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def get_health() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def disk_pct() -> float:
    try:
        du = shutil.disk_usage(BASE)
        return 100.0 * du.used / du.total
    except Exception:
        return 0.0


def check(h: dict | None) -> list:
    """Возвращает список проблем (пусто = всё ок)."""
    problems = []
    if h is None or not h.get("ok"):
        problems.append("сервис не отвечает / ok=false")
        return problems
    mq = h.get("mail_queue") or {}
    if mq.get("pending", 0) >= PENDING_LIMIT:
        problems.append(f"очередь растёт: pending={mq.get('pending')} (лимит {PENDING_LIMIT})")
    if mq.get("workers_alive", 1) == 0:
        problems.append("воркер расшифровки мёртв (workers_alive=0)")
    if h.get("ram_pct") is not None and h["ram_pct"] >= RAM_PCT_LIMIT:
        problems.append(f"RAM {h['ram_pct']:.1f}% >= {RAM_PCT_LIMIT:.0f}% (cgroup)")
    if disk_pct() >= DISK_PCT_LIMIT:
        problems.append(f"диск data {disk_pct():.1f}% >= {DISK_PCT_LIMIT:.0f}%")
    return problems


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"bad": False, "last_alert": 0, "problems": []}


def save_state(st: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def send_telegram(text: str) -> bool:
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if not token:
            log("НЕТ токена Octopus-бота — алерт не отправлен")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": CHAT_ID, "text": text, "disable_notification": False}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.load(r).get("ok", False)
        return ok
    except Exception as e:
        log(f"send_telegram: {e}")
        return False


def main(dry: bool = False):
    h = get_health()
    problems = check(h)
    st = load_state()
    now = time.time()
    bad = bool(problems)

    if dry:
        log("DRY-RUN: " + (", ".join(problems) if problems else "всё ок"))
        if h:
            log("  health: ok=%s mq=%s ram_pct=%s" % (h.get("ok"), h.get("mail_queue"), h.get("ram_pct")))
        return 0 if not bad else 1

    if bad and not st.get("bad"):
        # переход в bad → алерт
        text = "🚨 SNIN Mail: " + "; ".join(problems)
        ok = send_telegram(text)
        log(f"ALERT: {text} (sent={ok})")
        st = {"bad": True, "last_alert": now, "problems": problems}
    elif bad and st.get("bad") and now - st.get("last_alert", 0) > REALERT_SEC:
        text = "🔁 SNIN Mail всё ещё: " + "; ".join(problems)
        ok = send_telegram(text)
        log(f"RE-ALERT: {text} (sent={ok})")
        st["last_alert"] = now
        st["problems"] = problems
    elif bad:
        log(f"bad (уже алертили {int(now - st.get('last_alert', 0))}с назад): {problems}")
    else:
        if st.get("bad"):
            send_telegram("✅ SNIN Mail: восстановлено")
            log("RECOVERED: всё ок, алерт восстановления отправлен")
        else:
            log("ok")
        st = {"bad": False, "last_alert": 0, "problems": []}
    save_state(st)
    return 0 if not bad else 1


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(main(dry=dry))
