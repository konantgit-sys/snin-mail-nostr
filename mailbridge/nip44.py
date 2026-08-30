"""
NIP-44 (v2) — Encrypted Payloads.

Реализация по спеке https://github.com/nostr-protocol/nips/blob/master/44.md
Проверена тест-векторами из https://github.com/paulmillr/nip44 (nip44.vectors.json).

Алгоритм v2:
  - conversation key: ECDH (secp256k1, unhashed X) + HKDF-extract(salt='nip44-v2')
  - message keys: HKDF-expand(PRK=conv_key, info=nonce(32), L=76) → chacha_key(32), chacha_nonce(12), hmac_key(32)
  - payload: base64( 0x02 | nonce(32) | chacha20(padded) | hmac_sha256(hmac_key, nonce||ciphertext)(32) )
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import math
import os

import secp256k1
from Crypto.Cipher import ChaCha20

# ─────────────────────────── HKDF (RFC 5869) ───────────────────────────

def _hkdf_extract(ikm: bytes, salt: bytes) -> bytes:
    return hmac_mod.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    n = math.ceil(length / 32)
    if n > 255:
        raise ValueError("HKDF expand: too long")
    out = b""
    t = b""
    for i in range(1, n + 1):
        t = hmac_mod.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]


# ─────────────────────────── secp256k1 ECDH ───────────────────────────

def _parse_xonly_pubkey(public_key_hex: str) -> secp256k1.PublicKey:
    """Парсит 32-байтовый X-only публичный ключ (BIP340) в точку на кривой.
    Пробуем префикс 0x02 (чётный Y), при неудаче — 0x03 (нечётный Y)."""
    x = bytes.fromhex(public_key_hex)
    if len(x) != 32:
        raise ValueError("invalid pubkey length")
    for prefix in (b"\x02", b"\x03"):
        try:
            return secp256k1.PublicKey(prefix + x, raw=True)
        except Exception:
            continue
    raise ValueError("invalid public key (not on curve)")


def get_conversation_key(private_key_hex: str, public_key_hex: str) -> bytes:
    """Conv key между приватным ключом A и публичным B. Симметрично: conv(a,B)==conv(b,A)."""
    priv = secp256k1.PrivateKey(bytes.fromhex(private_key_hex), raw=True)
    pub = _parse_xonly_pubkey(public_key_hex)
    # ECDH unhashed: shared point = a·B, берём X-координату (BIP340 bytes(P)).
    # Пакет secp256k1 (whisper) не умеет unhashed ecdh — используем tweak_mul (то же умножение точки на скаляр).
    shared_point = pub.tweak_mul(priv.serialize()) if isinstance(priv.serialize(), bytes) else None
    if shared_point is None:
        shared_point = pub.tweak_mul(bytes.fromhex(priv.serialize()))
    shared_x = shared_point.serialize()[1:]  # 32B X-coordinate
    return _hkdf_extract(shared_x, b"nip44-v2")


def get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(conversation_key) != 32:
        raise ValueError("invalid conversation_key length")
    if len(nonce) != 32:
        raise ValueError("invalid nonce length")
    keys = _hkdf_expand(conversation_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]


def pubkey_from_privkey(private_key_hex: str) -> str:
    """X-only public key (32B hex) из приватного ключа."""
    priv = secp256k1.PrivateKey(bytes.fromhex(private_key_hex), raw=True)
    return priv.pubkey.serialize(compressed=True)[1:].hex()


# ─────────────────────────── padding ───────────────────────────

MIN_PLAINTEXT = 1
# Официальные реализации (paulmillr/nip44, nostr-tools) ограничивают 64KB
# (maxPlaintextSize 0xffff) — extended 6-байтовый префикс есть в спеке,
# но клиенты его не принимают. Для совместимости с Nmail держим тот же лимит.
MAX_PLAINTEXT = 65535
EXTENDED_THRESHOLD = 65536
# лимиты payload (как в официальной JS-реализации): base64 <= 87472, raw <= 65603
MAX_PAYLOAD_LEN = 87472
MAX_DATA_LEN = 65603


def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(unpadded_len - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((unpadded_len - 1) / chunk) + 1)


def pad(plaintext: bytes) -> bytes:
    unpadded_len = len(plaintext)
    if not (MIN_PLAINTEXT <= unpadded_len <= MAX_PLAINTEXT):
        raise ValueError("invalid plaintext length")
    if unpadded_len >= EXTENDED_THRESHOLD:
        prefix = b"\x00\x00" + unpadded_len.to_bytes(4, "big")
    else:
        prefix = unpadded_len.to_bytes(2, "big")
    suffix = bytes(calc_padded_len(unpadded_len) - unpadded_len)
    return prefix + plaintext + suffix


def unpad(padded: bytes) -> bytes:
    first_two = int.from_bytes(padded[0:2], "big")
    if first_two == 0:
        unpadded_len = int.from_bytes(padded[2:6], "big")
        if unpadded_len < EXTENDED_THRESHOLD:
            raise ValueError("invalid padding")
        prefix_len = 6
    else:
        unpadded_len = first_two
        prefix_len = 2
    unpadded = padded[prefix_len : prefix_len + unpadded_len]
    if (
        unpadded_len == 0
        or len(unpadded) != unpadded_len
        or len(padded) != prefix_len + calc_padded_len(unpadded_len)
    ):
        raise ValueError("invalid padding")
    return unpadded


# ─────────────────────────── chacha20 / mac ───────────────────────────

def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    cipher = ChaCha20.new(key=key, nonce=nonce12)
    return cipher.encrypt(data)


def _hmac_aad(key: bytes, message: bytes, aad: bytes) -> bytes:
    if len(aad) != 32:
        raise ValueError("AAD associated data must be 32 bytes")
    return hmac_mod.new(key, aad + message, hashlib.sha256).digest()


def _is_equal_ct(a: bytes, b: bytes) -> bool:
    return hmac_mod.compare_digest(a, b)


# ─────────────────────────── payload ───────────────────────────

def decode_payload(payload: str) -> tuple[bytes, bytes, bytes]:
    if len(payload) == 0 or payload[0] == "#":
        raise ValueError("unknown version")
    if len(payload) < 132:
        raise ValueError("invalid payload size")
    if len(payload) > MAX_PAYLOAD_LEN:
        raise ValueError("invalid payload length")
    data = base64.b64decode(payload)
    dlen = len(data)
    if dlen < 99:
        raise ValueError("invalid data size")
    if dlen > MAX_DATA_LEN:
        raise ValueError("invalid data length")
    vers = data[0]
    if vers != 2:
        raise ValueError(f"unknown version {vers}")
    nonce = data[1:33]
    ciphertext = data[33 : dlen - 32]
    mac = data[dlen - 32 : dlen]
    return nonce, ciphertext, mac


def encrypt(plaintext: str, conversation_key: bytes, nonce: bytes | None = None) -> str:
    """Шифрует строку UTF-8 → base64 payload."""
    if nonce is None:
        nonce = os.urandom(32)
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    padded = pad(plaintext.encode("utf-8"))
    ciphertext = _chacha20(chacha_key, chacha_nonce, padded)
    mac = _hmac_aad(hmac_key, ciphertext, nonce)
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode("ascii")


def decrypt(payload: str, conversation_key: bytes) -> str:
    """Расшифровывает base64 payload → строка UTF-8."""
    nonce, ciphertext, mac = decode_payload(payload)
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    calculated_mac = _hmac_aad(hmac_key, ciphertext, nonce)
    if not _is_equal_ct(calculated_mac, mac):
        raise ValueError("invalid MAC")
    padded_plaintext = _chacha20(chacha_key, chacha_nonce, ciphertext)
    return unpad(padded_plaintext).decode("utf-8")


def encrypt_str(plaintext: str, sender_privkey_hex: str, recipient_pubkey_hex: str) -> str:
    """Удобная обёртка: шифрует для получателя (публичный ключ)."""
    conv = get_conversation_key(sender_privkey_hex, recipient_pubkey_hex)
    return encrypt(plaintext, conv)


def decrypt_str(payload: str, my_privkey_hex: str, sender_pubkey_hex: str) -> str:
    """Удобная обёртка: расшифровывает от отправителя (публичный ключ)."""
    conv = get_conversation_key(my_privkey_hex, sender_pubkey_hex)
    return decrypt(payload, conv)
