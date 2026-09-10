"""
Тесты формата письма kind:1301 (RFC 2822) — mail_message.py.
"""

import os


from mailbridge import mail_message as mm


def test_roundtrip_basic():
    mail = mm.build_mail(
        from_addr="cryter@cryter-mail.v2.site",
        to_addr="npub1abc@nostr",
        subject="Hello world",
        body="First line.\nSecond line.",
    )
    parsed = mm.parse_mail(mail)
    assert parsed["from"] == "cryter@cryter-mail.v2.site"
    assert parsed["to"] == "npub1abc@nostr"
    assert parsed["subject"] == "Hello world"
    assert parsed["body"] == "First line.\nSecond line."
    assert parsed["message_id"].startswith("<") and parsed["message_id"].endswith(">")
    assert parsed["date"]  # Date заполнена


def test_unicode_subject_and_body():
    mail = mm.build_mail(
        from_addr="cryter@cryter-mail.v2.site",
        to_addr="alice@nostr",
        subject="Привет, мир! 📮",
        body="Тело с русским текстом и эмодзи 🚀\nВторая строка.",
    )
    parsed = mm.parse_mail(mail)
    assert parsed["subject"] == "Привет, мир! 📮"
    assert parsed["body"] == "Тело с русским текстом и эмодзи 🚀\nВторая строка."


def test_unique_message_ids():
    m1 = mm.build_mail("a@x", "b@y", "s", "b")
    m2 = mm.build_mail("a@x", "b@y", "s", "b")
    assert mm.parse_mail(m1)["message_id"] != mm.parse_mail(m2)["message_id"]


def test_thread_headers():
    mail = mm.build_mail(
        from_addr="a@x", to_addr="b@y", subject="Re: hello", body="reply",
        in_reply_to="<orig@x>", references="<orig@x> <prev@y>",
    )
    parsed = mm.parse_mail(mail)
    assert parsed["in_reply_to"] == "<orig@x>"
    assert parsed["references"] == "<orig@x> <prev@y>"


def test_explicit_message_id_and_date():
    import datetime

    dt = datetime.datetime(2026, 8, 25, 12, 0, 0, tzinfo=datetime.timezone.utc)
    mail = mm.build_mail(
        "a@x", "b@y", "s", "b", date=dt, message_id="<custom@x>"
    )
    parsed = mm.parse_mail(mail)
    assert parsed["message_id"] == "<custom@x>"
    assert "2026" in parsed["date"]


def test_size_limit():
    big_body = "x" * (mm.MAX_MAIL_SIZE + 1000)
    try:
        mm.build_mail("a@x", "b@y", "s", big_body)
        assert False, "должен был выкинуть ValueError"
    except ValueError:
        pass


def test_extract_addresses():
    mail = mm.build_mail("cryter@cryter-mail.v2.site", "bob@nostr", "s", "b")
    frm, to = mm.extract_addresses(mail)
    assert frm == "cryter@cryter-mail.v2.site"
    assert to == "bob@nostr"


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


# ── вложения (multipart/mixed) ──────────────────────────

def test_build_parse_with_attachment():
    import base64
    pdf = b"%PDF-1.4 fake pdf content \x00\x01\x02"
    att = {"filename": "док.pdf", "mime": "application/pdf", "data_base64": base64.b64encode(pdf).decode()}
    mail = mm.build_mail("a@x", "b@x", "С вложением", "Привет!", attachments=[att])
    assert "multipart/mixed" in mail
    assert 'filename="док.pdf"' in mail
    parsed = mm.parse_mail(mail)
    assert parsed["subject"] == "С вложением"
    assert parsed["body"].strip() == "Привет!"
    assert len(parsed["attachments"]) == 1
    got = parsed["attachments"][0]
    assert got["filename"] == "док.pdf"
    assert got["mime"] == "application/pdf"
    assert base64.b64decode(got["data_base64"]) == pdf


def test_build_parse_multiple_attachments():
    import base64
    atts = [
        {"filename": "a.txt", "mime": "text/plain", "data_base64": base64.b64encode(b"hello").decode()},
        {"filename": "b.png", "mime": "image/png", "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()},
    ]
    mail = mm.build_mail("a@x", "b@x", "Два файла", "Тело", attachments=atts)
    parsed = mm.parse_mail(mail)
    assert len(parsed["attachments"]) == 2
    assert [a["filename"] for a in parsed["attachments"]] == ["a.txt", "b.png"]


def test_parse_without_attachments_returns_empty():
    mail = mm.build_mail("a@x", "b@x", "Обычное", "Без вложений")
    parsed = mm.parse_mail(mail)
    assert parsed["attachments"] == []


def test_build_attachment_too_big_raises():
    import base64
    big = base64.b64encode(b"x" * 70000).decode()
    try:
        mm.build_mail("a@x", "b@x", "Большой", "Тело", attachments=[{"filename": "big.bin", "mime": "application/octet-stream", "data_base64": big}])
        raise AssertionError("должен был кинуть ValueError")
    except ValueError:
        pass
