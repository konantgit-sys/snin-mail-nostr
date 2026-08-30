"""
Nostr Mail Bridge — демон Фазы 1.

Слушает релеи (kind:1059 gift wrap + kind:1301 напрямую, адресованные нашему
pubkey), расшифровывает (NIP-59 unwrap → NIP-44), парсит RFC 2822, кладёт
в SQLite inbox, шлёт уведомления в Octopus. Умеет отправлять письма
(gift wrap на получателя) и публиковать на релеи.

Запуск:
    python3 -m mailbridge.mail_bridge --config config.json
или:
    python3 -m mailbridge.mail_bridge --nsec <hex> --relays wss://... [--db inbox.db]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

import requests
import websocket

from .nip44 import pubkey_from_privkey
from .nip59 import unwrap, wrap_mail, verify_signature, create_rumor, wrap
from .mail_message import build_mail, parse_mail, MAIL_KIND

log = logging.getLogger("mailbridge")

DEFAULT_RELAYS = [
    "wss://nos.lol",
    "wss://offchain.pub",
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    sender_pubkey TEXT,
    from_addr TEXT,
    to_addr TEXT,
    subject TEXT,
    body TEXT,
    received_at INTEGER,
    is_read INTEGER DEFAULT 0,
    raw_event TEXT,
    attachments TEXT DEFAULT '[]',
    owner TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    recipient_pubkey TEXT,
    subject TEXT,
    body TEXT,
    sent_at INTEGER,
    raw_event TEXT,
    owner TEXT DEFAULT ''
);
"""


def _ensure_inbox_attachments(db_path: str):
    """Добавляет колонку attachments в inbox (если её нет) — миграция."""
    import sqlite3 as _s
    try:
        with _s.connect(db_path, timeout=15) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(inbox)").fetchall()]
            if "attachments" not in cols:
                c.execute("ALTER TABLE inbox ADD COLUMN attachments TEXT DEFAULT '[]'")
                c.commit()
    except Exception:
        pass


class MailBridge:
    def __init__(
        self,
        privkey_hex: str,
        relays: list[str] | None = None,
        db_path: str = "inbox.db",
        telegram_token: str = "",
        telegram_chat_id: str = "",
        logger: logging.Logger | None = None,
        owner: str | None = None,
        label: str = "",
        max_inbox: int = 500,
    ):
        self.privkey = privkey_hex
        self.pubkey = pubkey_from_privkey(privkey_hex)
        self.owner = owner or self.pubkey  # владелец ящика (hex) — для мульти-ящика
        self.label = label or "Крайтер"   # метка в уведомлениях
        self.max_inbox = max_inbox        # квота ящика (писем)
        self.relays = relays or list(DEFAULT_RELAYS)
        self.db_path = db_path
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self._log = logger or log
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._init_db()

    # ── база ──────────────────────────────────────────────

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            # миграция старых БД (до мульти-ящика): добавляем owner
            for tbl in ("inbox", "outbox"):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                if "owner" not in cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN owner TEXT DEFAULT ''")
            conn.commit()
        _ensure_inbox_attachments(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ── входящие ──────────────────────────────────────────

    def handle_event(self, event: dict) -> bool:
        """Обрабатывает одно nostr-событие. True — письмо принято."""
        if not isinstance(event, dict) or "kind" not in event:
            return False
        kind = event.get("kind")

        if kind == 1059:
            return self._handle_gift_wrap(event)
        if kind == 1301:
            return self._handle_plain_1301(event)
        return False

    def _handle_gift_wrap(self, event: dict) -> bool:
        try:
            rumor, sender = unwrap(event, self.privkey)
        except Exception as e:
            self._log.debug("gift wrap не наш/битый: %s", e)
            return False
        if rumor.get("kind") != MAIL_KIND:
            self._log.debug("внутри gift wrap kind=%s, не письмо", rumor.get("kind"))
            return False
        return self._ingest_mail(rumor.get("content", ""), sender, event)

    def _handle_plain_1301(self, event: dict) -> bool:
        """Открытый kind:1301 (без gift wrap). Проверяем подпись, парсим."""
        if not verify_signature(event.get("pubkey", ""), event.get("id", ""), event.get("sig", "")):
            self._log.debug("kind:1301 с невалидной подписью — игнор")
            return False
        p_tags = [t[1] for t in event.get("tags", []) if isinstance(t, list) and t and t[0] == "p"]
        if self.pubkey not in p_tags:
            return False
        return self._ingest_mail(event.get("content", ""), event.get("pubkey", ""), event)

    def _ingest_mail(self, content: str, sender_pubkey: str, raw_event: dict) -> bool:
        parsed = parse_mail(content)
        # если это не письмо (нет subject/from) — возможно контент зашифрован NIP-44
        if not parsed["from"] and not parsed["subject"] and content:
            try:
                from .nip44 import decrypt, get_conversation_key
                ck = get_conversation_key(self.privkey, sender_pubkey)
                content = decrypt(content, ck)
                parsed = parse_mail(content)
            except Exception:
                self._log.debug("контент kind:1301 не распознан как письмо")
                return False

        if not parsed["from"] or not parsed["subject"]:
            self._log.debug("письмо без From/Subject — игнор")
            return False

        message_id = parsed["message_id"] or f"<{uuid.uuid4().hex}@snin-mail.v2.site>"

        attachments_json = json.dumps(parsed.get("attachments", []), ensure_ascii=False)

        # маршрутизация To: письмо пришло на мост (p-тег = мы), но To: = другой ящик
        # того же домена (внешние клиенты шлют через _smtp) → кладём в ящик адресата
        owner = self.owner
        to_addr = (parsed.get("to") or "").strip()
        target = self._owner_for_to(to_addr) if to_addr else None
        if target:
            owner = target
            self._log.info("маршрутизация To: %s → ящик %s", to_addr, owner[:12])

        # квота ящика: не копим сверх лимита
        with self._connect() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE owner=?", (owner,)
            ).fetchone()[0]
        if cnt >= self.max_inbox:
            self._log.warning("⚠️ ящик %s полон (%s/%s) — письмо отклонено", self.label, cnt, self.max_inbox)
            self.notify_telegram(f"⚠️ Ящик [{self.label}] полон ({cnt}/{self.max_inbox}) — письмо «{parsed['subject'][:60]}» не сохранено")
            return False

        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO inbox
                   (message_id, sender_pubkey, from_addr, to_addr, subject, body,
                    received_at, is_read, raw_event, attachments, owner)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?)""",
                (
                    message_id,
                    sender_pubkey,
                    parsed["from"],
                    parsed["to"],
                    parsed["subject"],
                    parsed["body"],
                    int(time.time()),
                    json.dumps(raw_event, ensure_ascii=False),
                    attachments_json,
                    owner,
                ),
            )
            inserted = conn.total_changes

        if inserted:
            self._log.info("📮 письмо принято: %s — %s", parsed["from"], parsed["subject"])
            self.notify_telegram(
                f"📮 Новое письмо [{self.label}]\nОт: {parsed['from']}\n"
                f"Тема: {parsed['subject']}\n\n{parsed['body'][:300]}"
            )
            return True
        return False

    # ── маршрутизация по To: ────────────────────────────────

    def _owner_for_to(self, addr: str) -> str | None:
        """Адрес вида npub@домен → pubkey владельца ящика (таблица accounts).

        Нужно для писем от внешних клиентов: они шлют через _smtp (наш ключ),
        но To: указывает на другой ящик того же домена — письмо должно
        попасть адресату, а не владельцу моста.
        """
        if "@" not in addr:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT pubkey_hex FROM accounts WHERE address=?", (addr.strip(),)
                ).fetchone()
                if row and row[0] != self.owner:
                    return row[0]
        except Exception as e:
            self._log.debug("маршрутизация To: нет accounts/ошибка: %s", e)
        return None

    # ── исходящие ─────────────────────────────────────────

    def send_mail(
        self,
        to_pubkey_hex: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
        publish: bool = True,
    ) -> dict | None:
        """Собирает письмо, gift wrap на получателя, публикует. Возвращает gift wrap."""
        mail_text = build_mail(
            from_addr, to_addr, subject, body,
            in_reply_to=in_reply_to, references=references,
        )
        rumor = create_rumor(self.pubkey, MAIL_KIND, mail_text, [["p", to_pubkey_hex]])
        gw = wrap(rumor, self.privkey, to_pubkey_hex)
        if publish:
            self.publish(gw)
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO outbox (message_id, recipient_pubkey, subject, body, sent_at, raw_event, owner) VALUES (?,?,?,?,?,?,?)",
                    (parse_mail(mail_text)["message_id"], to_pubkey_hex, subject, body, int(time.time()), json.dumps(gw, ensure_ascii=False), self.owner),
                )
        return gw

    def publish(self, event: dict) -> list[str]:
        """Публикует событие на все релеи (короткие соединения). Возвращает список принявших."""
        accepted = []
        payload = json.dumps(["EVENT", event], separators=(",", ":"))
        for url in self.relays:
            try:
                ws = websocket.create_connection(url, timeout=10)
                ws.send(payload)
                # ждём OK
                deadline = time.time() + 5
                ok = False
                while time.time() < deadline:
                    try:
                        msg = ws.recv()
                        arr = json.loads(msg)
                        if isinstance(arr, list) and arr and arr[0] == "OK":
                            ok = bool(arr[2]) if len(arr) > 2 else True
                            break
                    except Exception:
                        break
                ws.close()
                if ok:
                    accepted.append(url)
            except Exception as e:
                self._log.debug("publish %s: %s", url, e)
        self._log.info("опубликовано на %d/%d релеев", len(accepted), len(self.relays))
        return accepted

    # ── telegram ──────────────────────────────────────────

    def notify_telegram(self, text: str):
        if not self.telegram_token or not self.telegram_chat_id:
            self._log.warning("telegram notify: токен/чат не настроены, пропуск")
            return
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if r.status_code == 200 and r.json().get("ok"):
                self._log.info("telegram notify ok (%s)", r.json()["result"]["message_id"])
            else:
                self._log.warning("telegram notify: api ответил %s", r.text[:120])
        except Exception as e:
            self._log.warning("telegram notify failed: %s", e)

    # ── сеть ──────────────────────────────────────────────

    def _run_relay(self, url: str):
        subid = f"mb-{uuid.uuid4().hex[:8]}"
        filter_ = {"kinds": [1059, 1301], "#p": [self.pubkey], "limit": 100}

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: ws.send(json.dumps(["REQ", subid, filter_])),
                    on_message=lambda ws, msg: self._on_message(msg),
                    on_error=lambda ws, err: self._log.debug("%s error: %s", url, err),
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self._log.debug("%s crashed: %s", url, e)
            if not self._stop.is_set():
                self._log.info("реконнект %s через 5с", url)
                self._stop.wait(5)

    def _on_message(self, message: str):
        try:
            arr = json.loads(message)
        except Exception:
            return
        if not isinstance(arr, list) or not arr:
            return
        if arr[0] == "EVENT":
            # ["EVENT", <event>] — публикация; ["EVENT", <subid>, <event>] — ответ на REQ
            ev = arr[1] if len(arr) == 2 else arr[2]
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    return
            if isinstance(ev, dict):
                try:
                    self.handle_event(ev)
                except Exception as e:
                    self._log.debug("handle_event: %s", e)

    def start(self):
        self._log.info("мост запущен: pubkey %s, релеев %d", self.pubkey, len(self.relays))
        for url in self.relays:
            t = threading.Thread(target=self._run_relay, args=(url,), daemon=True)
            t.start()
            self._threads.append(t)
        while not self._stop.is_set():
            self._stop.wait(1)

    def stop(self):
        self._stop.set()


# ── утилиты ──────────────────────────────────────────────

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _convertbits(data, frombits, tobits, pad=True):
    """BIP-173 convertbits — корректная 5→8 бит конвертация."""
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_to_bytes(s: str) -> bytes:
    s = s.lower()
    pos = s.rfind("1")
    data = s[pos + 1:]
    vals = [_BECH32_CHARSET.find(c) for c in data]
    return bytes(_convertbits(vals, 5, 8) or b"")


def _npub_to_hex(npub: str) -> str | None:
    """npub (bech32) → pubkey hex (32 байта). None при ошибке."""
    try:
        if not npub.lower().startswith("npub1"):
            return None
        raw = _bech32_to_bytes(npub)
        # NIP-19: bech32-данные = ровно 32 байта ключа (без байта версии, в отличие от BIP-173)
        key = raw[:32] if len(raw) >= 32 else None
        return key.hex() if key and len(key) == 32 else None
    except Exception:
        return None


# ── CLI ──────────────────────────────────────────────────

def _load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Nostr Mail Bridge")
    parser.add_argument("--config", help="JSON конфиг (nsec, relays, db, telegram...)")
    parser.add_argument("--nsec-hex", help="приватный ключ (hex)")
    parser.add_argument("--relays", help="релеи через запятую")
    parser.add_argument("--db", default="inbox.db")
    args = parser.parse_args()

    cfg = _load_config(args.config) if args.config else {}
    nsec = args.nsec_hex or cfg.get("nsec_hex")
    if not nsec:
        raise SystemExit("нужен nsec_hex (--nsec-hex или config)")

    relays = (args.relays or cfg.get("relays") or DEFAULT_RELAYS)
    if isinstance(relays, str):
        relays = [r.strip() for r in relays.split(",") if r.strip()]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bridge = MailBridge(
        privkey_hex=nsec,
        relays=relays,
        db_path=args.db or cfg.get("db", "inbox.db"),
        telegram_token=cfg.get("telegram_token", ""),
        telegram_chat_id=cfg.get("telegram_chat_id", ""),
    )
    bridge.start()


if __name__ == "__main__":
    main()
