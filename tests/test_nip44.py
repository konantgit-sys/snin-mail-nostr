"""
Тесты NIP-44 по официальным векторам paulmillr/nip44 (nip44.vectors.json).
Запуск: python3 -m pytest tests/test_nip44.py -q   (или python3 tests/test_nip44.py)
"""

import hashlib
import json
import os


from mailbridge import nip44

VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "..", "docs", "nip44.vectors.json")))["v2"]


def test_get_conversation_key_valid():
    for v in VECTORS["valid"]["get_conversation_key"]:
        got = nip44.get_conversation_key(v["sec1"], v["pub2"]).hex()
        assert got == v["conversation_key"], f"sec1={v['sec1'][:8]}... got={got[:16]} expected={v['conversation_key'][:16]}"


def test_get_message_keys_valid():
    gm = VECTORS["valid"]["get_message_keys"]
    ck_bytes = bytes.fromhex(gm["conversation_key"])
    for k in gm["keys"]:
        chacha_key, chacha_nonce, hmac_key = nip44.get_message_keys(ck_bytes, bytes.fromhex(k["nonce"]))
        assert chacha_key.hex() == k["chacha_key"]
        assert chacha_nonce.hex() == k["chacha_nonce"]
        assert hmac_key.hex() == k["hmac_key"]


def test_calc_padded_len_valid():
    for unpadded, padded in VECTORS["valid"]["calc_padded_len"]:
        assert nip44.calc_padded_len(unpadded) == padded, f"len={unpadded}"


def test_encrypt_decrypt_valid():
    for v in VECTORS["valid"]["encrypt_decrypt"]:
        pub2 = nip44.pubkey_from_privkey(v["sec2"])
        conv1 = nip44.get_conversation_key(v["sec1"], pub2).hex()
        assert conv1 == v["conversation_key"], "conv key mismatch"
        payload = nip44.encrypt(v["plaintext"], bytes.fromhex(conv1), nonce=bytes.fromhex(v["nonce"]))
        assert payload == v["payload"], f"payload mismatch for {v['plaintext']!r}"
        # обратное направление: (sec2, pub1)
        pub1 = nip44.pubkey_from_privkey(v["sec1"])
        conv2 = nip44.get_conversation_key(v["sec2"], pub1).hex()
        assert conv2 == v["conversation_key"], "conv key not symmetric"
        plain = nip44.decrypt(payload, bytes.fromhex(conv2))
        assert plain == v["plaintext"]


def test_encrypt_decrypt_long_msg_valid():
    for v in VECTORS["valid"]["encrypt_decrypt_long_msg"]:
        plaintext = v["pattern"] * v["repeat"]
        payload = nip44.encrypt(plaintext, bytes.fromhex(v["conversation_key"]), nonce=bytes.fromhex(v["nonce"]))
        assert hashlib.sha256(plaintext.encode()).hexdigest() == v["plaintext_sha256"]
        assert hashlib.sha256(payload.encode()).hexdigest() == v["payload_sha256"]
        # и расшифровка возвращает оригинал
        dec = nip44.decrypt(payload, bytes.fromhex(v["conversation_key"]))
        assert dec == plaintext


def test_roundtrip_utf8():
    priv_a = "0000000000000000000000000000000000000000000000000000000000000001"
    priv_b = "0000000000000000000000000000000000000000000000000000000000000002"
    pub_b = nip44.pubkey_from_privkey(priv_b)
    conv = nip44.get_conversation_key(priv_a, pub_b)
    for msg in ["Привет, мир! 🚀", "a", "x" * 100, "длинный текст" * 300, "emoji: ⚡🤖📮", "line1\nline2\ttab"]:
        payload = nip44.encrypt(msg, conv)
        assert nip44.decrypt(payload, conv) == msg, f"roundtrip failed: {msg[:30]}"
    # случайные nonce не ломают
    p1 = nip44.encrypt("test", conv)
    p2 = nip44.encrypt("test", conv)
    assert p1 != p2, "same nonce reused!"


def test_invalid_encrypt_msg_lengths():
    for length in VECTORS["invalid"]["encrypt_msg_lengths"]:
        conv = bytes(32)  # не важен, упадёт на длине
        try:
            nip44.encrypt("x" * length, conv, nonce=bytes(32))
            raise AssertionError(f"length {length} should fail")
        except ValueError:
            pass


def test_invalid_get_conversation_key():
    for v in VECTORS["invalid"]["get_conversation_key"]:
        try:
            nip44.get_conversation_key(v["sec1"], v["pub2"])
            raise AssertionError(f"should fail: {v.get('note')}")
        except Exception:
            pass


def test_invalid_decrypt():
    for v in VECTORS["invalid"]["decrypt"]:
        try:
            nip44.decrypt(v["payload"], bytes.fromhex(v["conversation_key"]))
            raise AssertionError(f"should fail: {v.get('note')}")
        except Exception:
            pass


def test_mac_tamper_detected():
    priv_a = "0000000000000000000000000000000000000000000000000000000000000001"
    priv_b = "0000000000000000000000000000000000000000000000000000000000000002"
    conv = nip44.get_conversation_key(priv_a, nip44.pubkey_from_privkey(priv_b))
    payload = nip44.encrypt("secret message", conv)
    tampered = payload[:-4] + ("AAAA" if not payload.endswith("AAAA") else "BBBB")
    try:
        nip44.decrypt(tampered, conv)
        raise AssertionError("tampered payload decrypted!")
    except ValueError:
        pass


if __name__ == "__main__":
    import traceback

    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
                passed += 1
            except Exception:
                print(f"❌ {name}")
                traceback.print_exc()
    print(f"\n{passed} тестов прошло")
