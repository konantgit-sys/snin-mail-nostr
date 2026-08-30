"""
Тесты моста (mail_bridge.py): полный контур приёма письма без сети.

Проверяем: gift wrap → handle_event → unwrap → parse → SQLite inbox → telegram.
Плюс: дедупликация, plain kind:1301, send_mail (wrap наружу).
"""

import json
import os
import tempfile
from unittest import mock


from mailbridge import mail_message as mm
from mailbridge import nip44, nip59
from mailbridge.mail_bridge import MailBridge


def _make_bridge(privkey_hex, tmpdir, token="", chat=""):
    return MailBridge(
        privkey_hex=privkey_hex,
        relays=["wss://test.local"],
        db_path=os.path.join(tmpdir, "inbox.db"),
        telegram_token=token,
        telegram_chat_id=chat,
    )


def _wrap_mail_to(recipient_priv, sender_priv, subject="Hello", body="Body", to_addr="bob@nostr"):
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    mail = mm.build_mail(
        from_addr="alice@nostr", to_addr=to_addr, subject=subject, body=body
    )
    rumor = nip59.create_rumor(sender_pub, 1301, mail, [["p", recipient_pub]])
    return nip59.wrap(rumor, sender_priv, recipient_pub)


def test_inbox_full_cycle():
    """gift wrap → handle_event → письмо в SQLite inbox с правильными полями."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(recipient_priv, tmp)
        gw = _wrap_mail_to(recipient_priv, sender_priv, subject="Тема письма", body="Строка 1\nСтрока 2")

        assert bridge.handle_event(gw) is True

        with bridge._connect() as conn:
            rows = conn.execute("SELECT subject, body, from_addr, to_addr, sender_pubkey, is_read FROM inbox").fetchall()
        assert len(rows) == 1
        subject, body, from_addr, to_addr, sender_pubkey, is_read = rows[0]
        assert subject == "Тема письма"
        assert body == "Строка 1\nСтрока 2"
        assert from_addr == "alice@nostr"
        assert to_addr == "bob@nostr"
        assert sender_pubkey == nip44.pubkey_from_privkey(sender_priv)
        assert is_read == 0


def test_duplicate_delivery_ignored():
    """То же письмо второй раз — не дублируется (UPSERT по message_id)."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(recipient_priv, tmp)
        gw = _wrap_mail_to(recipient_priv, sender_priv)

        assert bridge.handle_event(gw) is True
        assert bridge.handle_event(gw) is False  # дубликат

        with bridge._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        assert n == 1


def test_telegram_notify_on_new_mail():
    """Новое письмо → уведомление в Octopus с темой."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(recipient_priv, tmp, token="TOKEN", chat="-1001")
        gw = _wrap_mail_to(recipient_priv, sender_priv, subject="Заголовок теста")

        with mock.patch("mailbridge.mail_bridge.requests.post") as post:
            bridge.handle_event(gw)
            post.assert_called_once()
            call_json = post.call_args.kwargs["json"]
            assert call_json["chat_id"] == "-1001"
            assert "Заголовок теста" in call_json["text"]


def test_plain_1301_accepted():
    """Открытый kind:1301 (подписанный, p-тег на нас) — принимается."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(recipient_priv, tmp)
        recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
        sender_pub = nip44.pubkey_from_privkey(sender_priv)

        mail = mm.build_mail("carol@nostr", "bob@nostr", "Открытое", "текст")
        eid, sig = nip59.sign_event(sender_pub, 1700000000, 1301, [["p", recipient_pub]], mail, sender_priv)
        event = {
            "id": eid, "pubkey": sender_pub, "kind": 1301,
            "content": mail, "created_at": 1700000000,
            "tags": [["p", recipient_pub]], "sig": sig,
        }

        assert bridge.handle_event(event) is True
        with bridge._connect() as conn:
            row = conn.execute("SELECT subject FROM inbox").fetchone()
        assert row[0] == "Открытое"


def test_plain_1301_bad_signature_rejected():
    """kind:1301 с кривой подписью — игнор."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(recipient_priv, tmp)
        recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
        sender_pub = nip44.pubkey_from_privkey(sender_priv)

        mail = mm.build_mail("carol@nostr", "bob@nostr", "Спам", "текст")
        eid, sig = nip59.sign_event(sender_pub, 1700000000, 1301, [["p", recipient_pub]], mail, sender_priv)
        event = {
            "id": eid, "pubkey": sender_pub, "kind": 1301,
            "content": mail, "created_at": 1700000000,
            "tags": [["p", recipient_pub]], "sig": "00" * 64,  # битая подпись
        }

        assert bridge.handle_event(event) is False
        with bridge._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        assert n == 0


def test_wrong_recipient_gift_wrap_ignored():
    """gift wrap на чужой ключ — не ловится."""
    with tempfile.TemporaryDirectory() as tmp:
        recipient_priv = nip59.new_private_key()
        stranger_priv = nip59.new_private_key()
        sender_priv = nip59.new_private_key()
        bridge = _make_bridge(stranger_priv, tmp)
        gw = _wrap_mail_to(recipient_priv, sender_priv)  # адресовано не bridge

        assert bridge.handle_event(gw) is False
        with bridge._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        assert n == 0


def test_send_mail_creates_valid_gift_wrap():
    """send_mail: письмо завернуто и получатель может его прочитать."""
    with tempfile.TemporaryDirectory() as tmp:
        sender_priv = nip59.new_private_key()
        recipient_priv = nip59.new_private_key()
        recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
        bridge = _make_bridge(sender_priv, tmp)

        with mock.patch.object(bridge, "publish") as pub:
            gw = bridge.send_mail(
                to_pubkey_hex=recipient_pub,
                from_addr="cryter@cryter-mail.v2.site",
                to_addr="alice@nostr",
                subject="Ответ Крайтера",
                body="Привет!",
                publish=True,
            )
            pub.assert_called_once()

        # получатель разворачивает
        rumor, sender = nip59.unwrap(gw, recipient_priv)
        assert rumor["kind"] == 1301
        parsed = mm.parse_mail(rumor["content"])
        assert parsed["subject"] == "Ответ Крайтера"
        assert parsed["body"] == "Привет!"
        assert sender == nip44.pubkey_from_privkey(sender_priv)

        # outbox записан
        with bridge._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        assert n == 1




def test_url_attachment_sha256_roundtrip():
    """sha256 url-вложения переживает build_mail → parse_mail (целостность файла)."""
    import base64
    from mailbridge.mail_message import build_mail, parse_mail

    sha = "9c7ff9d1857aaa4255a3e97b1996c327966a746fba28ad36ab3d33400d5e1b64"
    att = {"filename": "big.bin", "mime": "application/octet-stream",
           "url": "https://x/media/" + sha, "sha256": sha}
    mail = build_mail("a@x", "b@x", "С вложением", "Текст", attachments=[att])
    parsed = parse_mail(mail)
    assert len(parsed["attachments"]) == 1
    a = parsed["attachments"][0]
    assert a["url"] == "https://x/media/" + sha
    assert a["sha256"] == sha, f"sha256 потерялся: {a}"

    # без sha256 — пустая строка, не ключ-ошибка
    att2 = {"filename": "old.bin", "mime": "application/octet-stream", "url": "https://x/media/abc"}
    mail2 = build_mail("a@x", "b@x", "Старое", "Т", attachments=[att2])
    a2 = parse_mail(mail2)["attachments"][0]
    assert a2["sha256"] == ""


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception:
            print(f"❌ {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} тестов прошло")
    sys.exit(0 if passed == len(tests) else 1)


def test_ingest_saves_attachments(tmp_path):
    """Мост при приёме multipart-письма сохраняет вложения в колонку attachments."""
    import base64
    import json as _json
    from mailbridge.mail_message import build_mail, parse_mail
    from mailbridge.mail_bridge import MailBridge, _ensure_inbox_attachments

    db = str(tmp_path / "inbox.db")
    b = MailBridge(privkey_hex="11" * 32, relays=[], db_path=db, owner="0a" * 32, label="T")
    _ensure_inbox_attachments(db)

    att = {"filename": "doc.pdf", "mime": "application/pdf", "data_base64": base64.b64encode(b"%PDF-x").decode()}
    mail = build_mail("a@x", "b@x", "С вложением", "Текст", attachments=[att])
    ok = b._ingest_mail(mail, "0b" * 32, {"id": "1"})
    assert ok is True

    import sqlite3
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT body, attachments FROM inbox LIMIT 1").fetchone()
    assert row["body"].strip() == "Текст"
    atts = _json.loads(row["attachments"])
    assert len(atts) == 1
    assert atts[0]["filename"] == "doc.pdf"
    assert base64.b64decode(atts[0]["data_base64"]) == b"%PDF-x"


def test_ingest_inbox_quota(tmp_path):
    """Квота ящика: при max_inbox=2 третье письмо отклоняется."""
    from mailbridge.mail_message import build_mail
    from mailbridge.mail_bridge import MailBridge

    db = str(tmp_path / "inbox.db")
    b = MailBridge(privkey_hex="11" * 32, relays=[], db_path=db, owner="0a" * 32, label="Q", max_inbox=2)
    for i in range(3):
        m = build_mail(f"a{i}@x", "b@x", f"Письмо {i}", f"Тело {i}")
        ok = b._ingest_mail(m, "0b" * 32, {"id": str(i)})
        assert ok is (i < 2), f"письмо {i}: ожидали {i < 2}"
    import sqlite3
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    assert n == 2


def test_url_attachment_sha256_roundtrip():
    """sha256 url-вложения переживает build_mail → parse_mail (целостность файла)."""
    import base64
    from mailbridge.mail_message import build_mail, parse_mail

    sha = "9c7ff9d1857aaa4255a3e97b1996c327966a746fba28ad36ab3d33400d5e1b64"
    att = {"filename": "big.bin", "mime": "application/octet-stream",
           "url": "https://x/media/" + sha, "sha256": sha}
    mail = build_mail("a@x", "b@x", "С вложением", "Текст", attachments=[att])
    parsed = parse_mail(mail)
    assert len(parsed["attachments"]) == 1
    a = parsed["attachments"][0]
    assert a["url"] == "https://x/media/" + sha
    assert a["sha256"] == sha, f"sha256 потерялся: {a}"

    # без sha256 — пустая строка, не ключ-ошибка
    att2 = {"filename": "old.bin", "mime": "application/octet-stream", "url": "https://x/media/abc"}
    mail2 = build_mail("a@x", "b@x", "Старое", "Т", attachments=[att2])
    a2 = parse_mail(mail2)["attachments"][0]
    assert a2["sha256"] == ""
