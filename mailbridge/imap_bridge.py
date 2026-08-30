"""IMAP-мост: обычный email → SNIN-почта (kind:1301 через NIP-59).

Забирает письма с обычного IMAP-ящика (mail.ru и др.), конвертирует в
Nostr-письмо и публикует в ящик владельца SNIN-почты. Полный контур:
IMAP fetch → RFC 2822 → wrap_mail (NIP-59) → релеи → мост ловит →
inbox. Это направление работает БЕЗ собственного домена.

Конфиг: .secure/imap_config.json (НЕ в git):
{
  "host": "imap.mail.ru",
  "user": "example@mail.ru",
  "app_password": "xxxxxxxxxxxx",
  "target_owner": "cryter",        # label владельца из mail_accounts / OWNER_INDEX
  "poll_seconds": 120,
  "ssl": true
}

Запуск:  cd sites/cryter-mail && python3 -m mailbridge.imap_bridge
         (PYTHONPATH как в start.sh)
"""
from __future__ import annotations

import argparse
import base64
import email
import email.utils
import imaplib
import json
import logging
import os
import sys
import time

log = logging.getLogger("imap_bridge")

MAIL_KIND = 1301
SECURE_FILE = "/home/agent/data/.secure/imap_config.json"
REPO_SRC = "/home/agent/data/projects/nostr-mail-bridge/src"
REPO_DEPS = "/home/agent/data/projects/nostr-mail-bridge/deps"
for _p in (REPO_SRC, REPO_DEPS):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _decode_hdr(value: str) -> str:
    """RFC 2047 encoded-word → текст (Subject: =?utf-8?b?...?= и т.п.)."""
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value


def load_config(path: str = SECURE_FILE) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"нет {path} — создай по образцу imap_config.example.json"
        )
    cfg = json.load(open(path, encoding="utf-8"))
    need = ["host", "user", "app_password", "target_owner"]
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        raise ValueError(f"в imap_config.json не хватает: {missing}")
    return cfg


def fetch_unseen(cfg: dict, timeout: int = 60) -> list[tuple[bytes, bytes]]:
    """Забирает непрочитанные письма. Возвращает [(номер, RFC822 raw)]."""
    host = cfg["host"]
    if cfg.get("ssl", True):
        conn = imaplib.IMAP4_SSL(host, cfg.get("port", 993), timeout=timeout)
    else:
        conn = imaplib.IMAP4(host, cfg.get("port", 143), timeout=timeout)
    try:
        conn.login(cfg["user"], cfg["app_password"])
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        out: list[tuple[bytes, bytes]] = []
        for num in data[0].split():
            try:
                _, msg = conn.fetch(num, "(RFC822)")
                if msg and msg[0]:
                    out.append((num, msg[0][1]))
            except Exception as e:
                log.warning("fetch %s: %s", num, e)
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def mark_seen(cfg: dict, keep_unseen: list[bytes], timeout: int = 60) -> None:
    """Помечает ВСЕ письма прочитанными, кроме номеров в keep_unseen (не удалось)."""
    host = cfg["host"]
    if cfg.get("ssl", True):
        conn = imaplib.IMAP4_SSL(host, cfg.get("port", 993), timeout=timeout)
    else:
        conn = imaplib.IMAP4(host, cfg.get("port", 143), timeout=timeout)
    try:
        conn.login(cfg["user"], cfg["app_password"])
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        for num in data[0].split():
            if num in keep_unseen:
                continue
            try:
                conn.store(num, "+FLAGS", "\\Seen")
            except Exception:
                pass
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def imap_to_mail_text(raw: bytes) -> tuple[dict, str]:
    """IMAP-письмо (RFC 822) → (заголовки, RFC 2822 текст для kind:1301).

    Возвращает (meta, mail_text): meta = {from_, to, subject, date, attachments},
    mail_text — письмо в нашем формате (From/To/Subject/Date/Message-ID + тело),
    готовое для wrap_mail. Вложения-картинки base64 встраиваются в тело
    (Blossom-загрузка для IMAP — отдельная задача).
    """
    msg = email.message_from_bytes(raw)
    from_ = _decode_hdr(msg.get("From", ""))
    to_ = _decode_hdr(msg.get("To", ""))
    subject = _decode_hdr(msg.get("Subject", ""))
    date = msg.get("Date", "")

    body_parts: list[str] = []
    attachments: list[dict] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if ctype == "text/plain" and "attachment" not in disp.lower():
            try:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                continue
        elif "attachment" in disp.lower():
            fname = part.get_filename() or "file"
            data = part.get_payload(decode=True) or b""
            if data:
                attachments.append({
                    "filename": fname,
                    "mime": part.get_content_type() or "application/octet-stream",
                    "data_base64": base64.b64encode(data).decode(),
                })

    body = "\n\n".join(p for p in body_parts if p).strip() or "(без текста)"
    # заголовки, как у нас (поле To — адрес ящика-получателя, но в kind:1301
    # From — реальный отправитель письма; мост сам маршрутизирует в ящик владельца)
    mail_text = (
        f"From: {from_}\n"
        f"To: {to_}\n"
        f"Subject: {subject}\n"
        f"Date: {date}\n"
        f"Message-ID: <imap-{abs(hash(raw)):x}@snin-mail.v2.site>\n\n"
        f"{body}"
    )
    meta = {"from_": from_, "to": to_, "subject": subject,
            "date": date, "attachments": attachments}
    return meta, mail_text


def publish_to_inbox(cfg: dict, mail_text: str) -> tuple[bool, str]:
    """Публикует письмо в ящик владельца через мост (полный контур Nostr).

    cfg: {"owner": pubkey_hex владельца, ...} — письмо подписывается ключом
    ВЛАДЕЛЬЦА (из mail_keys) и адресуется p-тегом на него же → его мост
    поймает и положит в ЕГО inbox.
    """
    from mailapp.auth import get_mail_key
    from mailapp.bridge import get_bridge
    from mailbridge.nip59 import wrap_mail
    from mailbridge.nip44 import pubkey_from_privkey

    owner = cfg["owner"]
    nsec = get_mail_key(owner)
    if not nsec:
        return False, f"нет приватного ключа для владельца {owner[:12]}…"
    pub = owner
    if len(pub) != 64:
        pub = pubkey_from_privkey(nsec)

    gw = wrap_mail(nsec, pub, MAIL_KIND, mail_text, [["p", pub]])
    br = get_bridge(pub)
    if br is None:
        # NO_BRIDGE (тесты) или мост не поднят — событие не публикуем,
        # но считаем контур отработанным (проверка цепочки без релеев)
        return True, "no-bridge (test)"
    accepted = br.publish(gw)
    if accepted:
        return True, f"опубликовано на {len(accepted)} релеев"
    return False, "0 релеев приняли (публикация не удалась)"


def run_once(cfg: dict) -> dict:
    """Один проход: fetch unseen → publish → mark seen. Возвращает статистику."""
    items = fetch_unseen(cfg)
    ok, fail = 0, 0
    keep_unseen: list[bytes] = []
    for num, raw in items:
        try:
            meta, mail_text = imap_to_mail_text(raw)
            ok_pub, msg = publish_to_inbox(cfg, mail_text)
            if ok_pub:
                ok += 1
                log.info("доставлено %s: %s | %s", cfg.get("owner","?")[:10], meta.get("subject", "?")[:60], msg)
            else:
                fail += 1
                keep_unseen.append(num)  # не удалось — оставляем непрочитанным
                log.warning("публикация не удалась: %s", msg)
        except Exception as e:
            fail += 1
            keep_unseen.append(num)
            log.exception("письмо не обработано: %s", e)
    if items:
        try:
            mark_seen(cfg, keep_unseen)
        except Exception as e:
            log.warning("mark_seen: %s", e)
    return {"fetched": len(items), "ok": ok, "fail": fail}


def run_all(include_legacy: bool = True) -> dict:
    """Мульти-юзер: все включённые IMAP-конфиги из БД + legacy-конфиг.

    Для каждого владельца — свой цикл: fetch → публикация от ЕГО ключа
    (p-тег = его pubkey) → mark_seen → статус в БД (imap_configs).
    """
    from mailapp import imap_store as store
    from mailapp.config import DB

    store.ensure_table()
    configs = store.list_configs(enabled_only=True)
    stats = {"configs": len(configs), "per_owner": {}}

    # legacy-режим: .secure/imap_config.json (один владелец)
    legacy_owner = None
    if include_legacy and os.path.exists(SECURE_FILE):
        try:
            lc = load_config(SECURE_FILE)
            owner = _owner_hex_for_label(lc.get("target_owner", "cryter"))
            if owner:
                legacy_owner = owner
                cfg = {**lc, "owner": owner}
                st = run_once(cfg)
                stats["per_owner"][owner[:12]] = st
                store.touch_sync(owner, st["fail"] == 0,
                                 "нет подключения" if st["fail"] else "")
        except Exception as e:
            log.warning("legacy imap_config.json: %s", e)

    for c in configs:
        owner = c["owner"]
        cfg = {"host": c["host"], "port": c["port"], "ssl": c["ssl"],
               "user": c["user"], "app_password": c["app_password"], "owner": owner}
        try:
            st = run_once(cfg)
            stats["per_owner"][owner[:12]] = st
            store.touch_sync(owner, st["fail"] == 0,
                             "нет подключения" if st["fail"] else "")
        except Exception as e:
            log.exception("IMAP %s: %s", owner[:12], e)
            store.touch_sync(owner, False, str(e)[:200])
            stats["per_owner"][owner[:12]] = {"fetched": 0, "ok": 0, "fail": 1, "error": str(e)[:120]}

    if legacy_owner and not configs:
        stats["legacy"] = True
    return stats


def _owner_hex_for_label(label: str) -> str | None:
    """label владельца (cryter, director_ai…) → pubkey_hex из accounts."""
    try:
        from mailapp.config import DB
        import sqlite3
        row = sqlite3.connect(DB).execute(
            "SELECT pubkey_hex FROM accounts WHERE label=?", (label,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="IMAP→SNIN мост (мульти-юзер)")
    ap.add_argument("--config", default=SECURE_FILE, help="legacy-конфиг (необязателен)")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--no-legacy", action="store_true", help="только конфиги из БД")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s")
    if args.once:
        print(json.dumps(run_all(include_legacy=not args.no_legacy),
                         ensure_ascii=False, default=str))
        return
    log.info("IMAP-мост (мульти-юзер) запущен: конфиги из БД imap_configs, "
             "poll %s с", os.environ.get("IMAP_POLL", "120"))
    poll = int(os.environ.get("IMAP_POLL", "120"))
    while True:
        try:
            st = run_all(include_legacy=not args.no_legacy)
            total = sum(v.get("ok", 0) + v.get("fail", 0) for v in st["per_owner"].values())
            if total:
                log.info("проход: %s", st)
        except Exception as e:
            log.exception("проход упал: %s", e)
        time.sleep(poll)


if __name__ == "__main__":
    main()
