"""Конфигурация Nostr Mail: загрузка config.json, константы.

Модуль не имеет зависимостей от FastAPI/моста — импортируется где угодно.
"""
from __future__ import annotations

import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "config.json")

with open(CONFIG_PATH) as f:
    CFG = json.load(f)

NSEC = CFG["nsec_hex"]
PUBKEY = CFG["pubkey_hex"]
NPUB = CFG["npub"]
MAIL_ADDR = CFG["mail_address"]
DOMAIN = CFG["mail_domain"]
DB = CFG["db"]
RELAYS = CFG["relays"]
LIGHTNING = CFG.get("lightning", "")
AUTH_PASSWORD = CFG.get("auth_password", "cryter-mail")
SESSIONS_FILE = os.path.join(BASE, ".sessions.json")
SESSIONS_TTL = 86400 * 7  # 7 дней

STATIC_DIR = os.path.join(BASE, "static")

# ── мульти-ящик: владельцы (Крайтер, V2Bot, …) ──
OWNERS: list[dict] = CFG.get("owners", [])
OWNER_INDEX: dict[str, dict] = {o["pubkey_hex"]: o for o in OWNERS}
DEFAULT_OWNER: str = OWNERS[0]["pubkey_hex"] if OWNERS else PUBKEY

ACCOUNTS_FILE: str = CFG.get("accounts_file", os.path.expanduser("~/data/.secure/mail_accounts.json"))  # пароли сид-аккаунтов (0o600)

# ── квоты (на пользователя) ─────────────────────────────
_LIMITS = CFG.get("limits", {})
LIMITS = {
    "max_mails_per_user": int(_LIMITS.get("max_mails_per_user", 500)),        # писем в ящике
    "max_send_per_day": int(_LIMITS.get("max_send_per_day", 100)),            # отправок в сутки
    "max_attachment_size_mb": int(_LIMITS.get("max_attachment_size_mb", 5)),  # файл, МБ
    "max_attachments_per_mail": int(_LIMITS.get("max_attachments_per_mail", 5)),
    "max_mail_body": int(_LIMITS.get("max_mail_body", 20000)),                # символов в письме
    "register_limit_per_hour": int(_LIMITS.get("register_limit_per_hour", 50)),  # антиспам
}
