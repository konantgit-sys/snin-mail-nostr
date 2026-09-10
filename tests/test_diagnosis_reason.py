"""Причина отказа задачи должна быть настоящей, а не фразой-заглушкой.

Регресс на прод-инцидент 31.08–10.09.2026: 153 задачи подряд упали с текстом
«не расшифровано ни одним ключом группы», хотя ключ был, NIP-44 распаковывал
событие, и настоящая причина («внутри gift wrap kind=25910», протокол
ContextVM) уходила только в debug-лог, который в проде выключен. Владелец
10 дней видел ложную причину.

Контракт: mail_bridge.handle_event() при отказе обязан заполнить last_error,
а воркер — записать именно эту причину в mail_queue.error.
"""

import json
import os
import tempfile
import time

from mailbridge import mail_message as mm
from mailbridge import nip44, nip59
from mailbridge.mail_bridge import MailBridge

FOREIGN_KIND = 25910  # чужой протокол (ContextVM), не письмо


def _make_bridge(privkey_hex, tmpdir):
    return MailBridge(
        privkey_hex=privkey_hex,
        relays=["wss://test.local"],
        db_path=os.path.join(tmpdir, "inbox.db"),
    )


def _wrap_foreign(recipient_pub: str, inner_kind: int = FOREIGN_KIND) -> dict:
    """gift wrap, внутри которого чужое событие вместо NIP-59 seal (kind:13).

    Именно так выглядит прод-трафик: 1059 → NIP-44 распаковывается → внутри
    событие чужого kind, а не seal.
    """
    now = int(time.time())
    inner_priv = nip59.new_private_key()
    inner_pub = nip44.pubkey_from_privkey(inner_priv)
    iid, isig = nip59.sign_event(inner_pub, now, inner_kind, [], '{"jsonrpc":"2.0"}', inner_priv)
    inner = {
        "id": iid,
        "pubkey": inner_pub,
        "kind": inner_kind,
        "content": '{"jsonrpc":"2.0"}',
        "created_at": now,
        "tags": [],
        "sig": isig,
    }
    ephem = nip59.new_private_key()
    ephem_pub = nip44.pubkey_from_privkey(ephem)
    ck = nip44.get_conversation_key(ephem, recipient_pub)
    content = nip44.encrypt(json.dumps(inner, ensure_ascii=False), ck)
    gid, gsig = nip59.sign_event(ephem_pub, now, 1059, [["p", recipient_pub]], content, ephem)
    return {
        "id": gid,
        "pubkey": ephem_pub,
        "kind": 1059,
        "content": content,
        "created_at": now,
        "tags": [["p", recipient_pub]],
        "sig": gsig,
    }


def _wrap_real_mail(recipient_priv: str) -> dict:
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
    sender_priv = nip59.new_private_key()
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    mail = mm.build_mail(from_addr="alice@nostr", to_addr="bob@nostr", subject="Hi", body="Body")
    rumor = nip59.create_rumor(sender_pub, 1301, mail, [["p", recipient_pub]])
    return nip59.wrap(rumor, sender_priv, recipient_pub)


def test_foreign_kind_inside_gift_wrap_is_reported():
    """Главный кейс инцидента: причина названа, а не спрятана за заглушкой."""
    with tempfile.TemporaryDirectory() as td:
        recipient = nip59.new_private_key()
        br = _make_bridge(recipient, td)
        assert br.handle_event(_wrap_foreign(nip44.pubkey_from_privkey(recipient))) is False
        assert str(FOREIGN_KIND) in br.last_error
        assert "seal" in br.last_error or "kind" in br.last_error


def test_unsupported_kind_is_reported():
    with tempfile.TemporaryDirectory() as td:
        recipient = nip59.new_private_key()
        br = _make_bridge(recipient, td)
        assert br.handle_event({"kind": FOREIGN_KIND, "content": "x", "tags": []}) is False
        assert str(FOREIGN_KIND) in br.last_error


def test_success_clears_previous_reason():
    """last_error не должен переживать успешную доставку."""
    with tempfile.TemporaryDirectory() as td:
        recipient = nip59.new_private_key()
        br = _make_bridge(recipient, td)
        br.handle_event(_wrap_foreign(nip44.pubkey_from_privkey(recipient)))
        assert br.last_error
        assert br.handle_event(_wrap_real_mail(recipient)) is True
        assert br.last_error == ""


def test_worker_takes_reason_from_bridge():
    from mailapp import worker

    class _FakeBridge:
        last_error = "внутри gift wrap kind=25910, а письмом считаем kind=1301"

    assert "25910" in worker._fail_reason(_FakeBridge(), "")
    assert worker._fail_reason(_FakeBridge(), "boom") == "boom"


def test_misleading_phrase_is_gone_from_worker():
    """Фраза-заглушка не должна вернуться в код воркера."""
    path = os.path.join(os.path.dirname(__file__), "..", "mailapp", "worker.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    bad = "не расшифровано ни одним ключом группы"
    assert bad not in src
    assert "_fail_reason" in src
