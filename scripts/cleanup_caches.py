#!/usr/bin/env python3
"""Авто-чистка кэшей пода (Фаза 5). Cron: ежедневно 04:30.

- media_cache: старше 7 дней (скриншоты, временные медиа)
- uploads: старше 30 дней
- tmp/*: старше 7 дней
Безопасно: свежие файлы не трогает, работает по mtime.
"""
import os
import time

BASE = os.environ.get("MAIL_BASE", os.path.expanduser("~/data"))  # env-переопределяемый
TARGETS = [
    (os.path.join(BASE, "media_cache"), 7 * 86400),
    (os.path.join(BASE, "uploads"), 30 * 86400),
    (os.path.join(BASE, "tmp"), 7 * 86400),
]

now = time.time()
freed = 0
removed = 0
for path, ttl in TARGETS:
    if not os.path.isdir(path):
        continue
    for root, dirs, files in os.walk(path):
        for fname in files:
            fp = os.path.join(root, fname)
            try:
                if now - os.path.getmtime(fp) > ttl:
                    size = os.path.getsize(fp)
                    os.remove(fp)
                    freed += size
                    removed += 1
            except Exception:
                pass
print(f"cleanup: удалено {removed} файлов, освобождено {freed / 1024 / 1024:.1f} МБ")
