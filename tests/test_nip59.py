"""
NIP-59 tests: round-trip wrap/unwrap, структура слоёв, подписи, приватность.
"""

import json
import os


from mailbridge import nip44, nip59


def test_roundtrip_mail():
    """rumor kind:1301 → wrap → unwrap → тот же rumor и sender."""
    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    mail = json.dumps({
        "headers": {"From": "cryter@cryter-mail.v2.site", "To": "alice@nostr", "Subject": "Hello"},
        "body": "Привет, это тестовое письмо 📮",
    }, ensure_ascii=False)

    rumor = nip59.create_rumor(sender_pub, 1301, mail, [["p", recipient_pub]])
    gw = nip59.wrap(rumor, sender_priv, recipient_pub)

    # структура gift wrap
    assert gw["kind"] == 1059
    assert [t[0] for t in gw["tags"]] == ["p"]
    assert gw["tags"][0][1] == recipient_pub
    assert gw["pubkey"] != sender_pub  # эфемерный ключ скрывает отправителя
    assert gw["id"] and gw["sig"]

    # unwrap
    rumor_back, sender_back = nip59.unwrap(gw, recipient_priv)
    assert rumor_back == rumor
    assert sender_back == sender_pub


def test_seal_structure():
    """seal: kind 13, tags пустые, подписан реальным отправителем."""
    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    rumor = nip59.create_rumor(sender_pub, 1301, "secret", [])
    gw = nip59.wrap(rumor, sender_priv, recipient_pub)

    # распаковываем вручную: gift wrap → seal
    my_pub = nip44.pubkey_from_privkey(recipient_priv)
    ck_gw = nip44.get_conversation_key(recipient_priv, gw["pubkey"])
    seal = json.loads(nip44.decrypt(gw["content"], ck_gw))

    assert seal["kind"] == 13
    assert seal["tags"] == []  # NIP-59: tags MUST always be empty
    assert seal["pubkey"] == sender_pub  # реальный отправитель
    assert seal["id"] and seal["sig"]
    # подпись seal валидна
    assert nip59.verify_signature(seal["pubkey"], seal["id"], seal["sig"])


def test_gift_wrap_signature_valid():
    """gift wrap подписан эфемерным ключом — подпись валидна, но автор ≠ sender."""
    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    rumor = nip59.create_rumor(sender_pub, 1301, "x", [])
    gw = nip59.wrap(rumor, sender_priv, recipient_pub)

    assert nip59.verify_signature(gw["pubkey"], gw["id"], gw["sig"])
    assert gw["pubkey"] != sender_pub


def test_unwrap_wrong_recipient_fails():
    """Чужой получатель не может расшифровать."""
    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    stranger_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    rumor = nip59.create_rumor(sender_pub, 1301, "secret", [])
    gw = nip59.wrap(rumor, sender_priv, recipient_pub)

    try:
        nip59.unwrap(gw, stranger_priv)
        assert False, "чужой ключ не должен расшифровать"
    except ValueError:
        pass  # ожидаемо


def test_wrap_mail_helper():
    """wrap_mail: быстрый путь rumor(1301) → gift wrap."""
    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    gw = nip59.wrap_mail(sender_priv, recipient_pub, 1301, "body", [["p", recipient_pub]])
    assert gw["kind"] == 1059

    rumor, sender = nip59.unwrap(gw, recipient_priv)
    assert rumor["kind"] == 1301
    assert rumor["content"] == "body"
    assert sender == sender_pub


def test_created_at_tweak():
    """created_at у seal и gift wrap может отличаться (анти time-analysis), но в прошлом."""
    import time as _time

    sender_priv = nip59.new_private_key()
    recipient_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)

    now = int(_time.time())
    rumor = nip59.create_rumor(sender_pub, 1301, "x", [], created_at=now)
    gw = nip59.wrap(rumor, sender_priv, recipient_pub, created_at=now)

    assert abs(gw["created_at"] - now) <= 60
    assert gw["created_at"] <= now + 60  # не в далёком будущем


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
