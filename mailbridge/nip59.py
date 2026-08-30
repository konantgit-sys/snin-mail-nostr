"""
NIP-59 — Gift Wrap (seal kind:13 → gift wrap kind:1059).

Спека: https://github.com/nostr-protocol/nips/blob/master/59.md
Поверх NIP-44 (src/mailbridge/nip44.py).

Слои шифрования:
  rumor      — unsigned event (содержимое, любой kind, напр. 1301 для почты)
  seal       — kind:13, подписан РЕАЛЬНЫМ отправителем,
               content = NIP-44(rumor), tags ВСЕГДА пустые
  gift wrap  — kind:1059, подписан СЛУЧАЙНЫМ одноразовым ключом,
               content = NIP-44(seal), tags = [["p", <получатель>]]

Релеи видят только gift wrap: отправитель скрыт (эфемерный ключ),
получатель виден только в p-теге, содержимое зашифровано дважды.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

import secp256k1

from .nip44 import get_conversation_key, encrypt, decrypt, pubkey_from_privkey

# ─────────────────────────── NIP-01 helpers ───────────────────────────


def event_id(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> str:
    """NIP-01 event id: sha256(serialized [0, pubkey, created_at, kind, tags, content])."""
    serialized = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def sign_event(
    pubkey: str, created_at: int, kind: int, tags: list, content: str, privkey_hex: str
) -> tuple[str, str]:
    """Подписывает событие (BIP-340 schnorr, raw-режим — стандарт nostr). Возвращает (id, sig)."""
    eid = event_id(pubkey, created_at, kind, tags, content)
    priv = secp256k1.PrivateKey(bytes.fromhex(privkey_hex), raw=True)
    sig = priv.schnorr_sign(bytes.fromhex(eid), None, raw=True).hex()
    return eid, sig


def verify_signature(pubkey: str, eid: str, sig: str) -> bool:
    """Проверяет BIP-340 подпись события.

    pubkey может быть 64 hex (X-only, 32 байта) или 66 hex (compressed
    с префиксом 02/03). secp256k1-py требует 33 байта с префиксом —
    для X-only добавляем 02 (BIP-340, Y-чётность не важна).
    """
    try:
        pk_bytes = bytes.fromhex(pubkey)
        if len(pk_bytes) == 32:
            pk_bytes = b"\x02" + pk_bytes
        if len(pk_bytes) != 33:
            return False
        pub = secp256k1.PublicKey(pk_bytes, raw=True)
        return pub.schnorr_verify(bytes.fromhex(eid), bytes.fromhex(sig), None, raw=True)
    except Exception:
        return False


def new_private_key() -> str:
    """Случайный приватный ключ (hex, 32 байта) — для эфемерной подписи gift wrap."""
    return os.urandom(32).hex()


# ─────────────────────────── rumor ───────────────────────────


def create_rumor(
    pubkey_hex: str,
    kind: int,
    content: str,
    tags: list | None = None,
    created_at: int | None = None,
) -> dict:
    """Unsigned событие (rumor). id/sig пустые — подписывается только внутри seal."""
    return {
        "id": "",
        "pubkey": pubkey_hex,
        "kind": kind,
        "content": content,
        "created_at": created_at if created_at is not None else int(time.time()),
        "tags": tags if tags is not None else [],
    }


# ─────────────────────────── wrap / unwrap ───────────────────────────


def _jbytes(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def wrap(
    rumor: dict,
    sender_privkey_hex: str,
    recipient_pubkey_hex: str,
    created_at: int | None = None,
) -> dict:
    """Оборачивает rumor в seal (kind:13) и gift wrap (kind:1059).

    Возвращает готовый gift wrap event (id + sig заполнены) — его и публикуем.
    """
    t = created_at if created_at is not None else int(time.time())
    sender_pub = pubkey_from_privkey(sender_privkey_hex)

    # 1) seal: NIP-44(rumor), подписан реальным отправителем, tags=[]
    ck_seal = get_conversation_key(sender_privkey_hex, recipient_pubkey_hex)
    seal_content = encrypt(_jbytes(rumor).decode("utf-8"), ck_seal)
    seal_id, seal_sig = sign_event(sender_pub, t, 13, [], seal_content, sender_privkey_hex)
    seal = {
        "id": seal_id,
        "pubkey": sender_pub,
        "kind": 13,
        "content": seal_content,
        "created_at": t,
        "tags": [],
        "sig": seal_sig,
    }

    # 2) gift wrap: NIP-44(seal), подписан случайным ключом, p-тег = получатель
    #    timestamp слегка сдвигаем (анти time-analysis, спека: SHOULD be tweaked)
    ephem_priv = new_private_key()
    ephem_pub = pubkey_from_privkey(ephem_priv)
    ck_gw = get_conversation_key(ephem_priv, recipient_pubkey_hex)
    gw_content = encrypt(_jbytes(seal).decode("utf-8"), ck_gw)
    t_gw = t + random.randint(-60, 60)
    gw_tags = [["p", recipient_pubkey_hex]]
    gw_id, gw_sig = sign_event(ephem_pub, t_gw, 1059, gw_tags, gw_content, ephem_priv)

    return {
        "id": gw_id,
        "pubkey": ephem_pub,
        "kind": 1059,
        "content": gw_content,
        "created_at": t_gw,
        "tags": gw_tags,
        "sig": gw_sig,
    }


def unwrap(gift_wrap: dict, my_privkey_hex: str) -> tuple[dict, str]:
    """Разворачивает gift wrap, адресованный нам.

    Возвращает (rumor, sender_pubkey_hex). Кидает ValueError если:
      - нет p-тега на нас;
      - расшифровка не удалась (не нам адресовано / повреждено).
    """
    my_pub = pubkey_from_privkey(my_privkey_hex)

    p_tags = [t[1] for t in gift_wrap.get("tags", []) if isinstance(t, list) and t and t[0] == "p"]
    if p_tags and my_pub not in p_tags:
        raise ValueError("gift wrap не адресован нам (p-тег чужой)")

    gw_pubkey = gift_wrap.get("pubkey", "")
    if not gw_pubkey:
        raise ValueError("gift wrap без pubkey")

    # расшифровываем seal
    ck_gw = get_conversation_key(my_privkey_hex, gw_pubkey)
    try:
        seal_json = decrypt(gift_wrap["content"], ck_gw)
    except Exception as e:
        raise ValueError(f"не удалось расшифровать seal: {e}") from e
    seal = json.loads(seal_json)

    if seal.get("kind") != 13:
        raise ValueError(f"ожидался seal kind:13, получили kind:{seal.get('kind')}")

    # расшифровываем rumor
    ck_seal = get_conversation_key(my_privkey_hex, seal["pubkey"])
    try:
        rumor_json = decrypt(seal["content"], ck_seal)
    except Exception as e:
        raise ValueError(f"не удалось расшифровать rumor: {e}") from e
    rumor = json.loads(rumor_json)

    return rumor, seal["pubkey"]


# ─────────────────────────── API для почты ───────────────────────────


def wrap_mail(
    sender_privkey_hex: str,
    recipient_pubkey_hex: str,
    kind: int,
    content: str,
    tags: list | None = None,
    created_at: int | None = None,
) -> dict:
    """Удобная обёртка: rumor(kind=1301, content=письмо) → gift wrap."""
    sender_pub = pubkey_from_privkey(sender_privkey_hex)
    rumor = create_rumor(sender_pub, kind, content, tags, created_at)
    return wrap(rumor, sender_privkey_hex, recipient_pubkey_hex, created_at)
