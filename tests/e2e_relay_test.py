"""
E2E-тест моста с реальным релеем (требует сеть).

Полный контур:
  1. генерируем ключи (отправитель → получатель=«мост»)
  2. gift wrap публикуется на публичный релей
  3. читаем kind:1059 с релея (REQ #p на получателя)
  4. unwrap → rumor kind:1301 → subject/body совпали

Запуск: PYTHONPATH=./deps:./src python3 tests/e2e_relay_test.py [wss://relay...]
Не является частью unit-прогона: реальная сеть, ~30-60 сек.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import websocket

from mailbridge import mail_message as mm
from mailbridge import nip44, nip59
from mailbridge.mail_bridge import MailBridge

RELAYS = ["wss://relay.damus.io", "wss://nos.lol", "wss://offchain.pub"]


def main():
    relay = sys.argv[1] if len(sys.argv) > 1 else RELAYS[0]
    print(f"Релей: {relay}")

    recipient_priv = nip59.new_private_key()
    sender_priv = nip59.new_private_key()
    recipient_pub = nip44.pubkey_from_privkey(recipient_priv)
    sender_pub = nip44.pubkey_from_privkey(sender_priv)
    print(f"получатель: {recipient_pub[:16]}…")
    print(f"отправитель: {sender_pub[:16]}…")

    if os.path.exists("/tmp/e2e_inbox.db"):
        os.remove("/tmp/e2e_inbox.db")
    bridge = MailBridge(recipient_priv, relays=[relay], db_path="/tmp/e2e_inbox.db")

    # 1. письмо
    mail = mm.build_mail(
        "e2e@nostr", "bridge@cryter-mail.v2.site", "E2E тест моста", "Привет из e2e! 🚀"
    )
    rumor = nip59.create_rumor(sender_pub, 1301, mail, [["p", recipient_pub]])
    gw = nip59.wrap(rumor, sender_priv, recipient_pub)
    print(f"gift wrap id: {gw['id'][:16]}…")

    # 2. публикация на релей
    accepted = bridge.publish(gw)
    print(f"приняли релеи: {accepted}")
    if relay not in accepted:
        print("❌ релей не принял событие — сетевой тест не прошёл")
        return 1

    # 3. читаем обратно с релея
    print("читаю обратно (REQ kind:1059 #p)...")
    got = []

    def on_message(ws, message):
        try:
            arr = json.loads(message)
        except Exception:
            return
        if arr and arr[0] == "EVENT":
            # ["EVENT", <subid>, <event>] (ответ на REQ) или ["EVENT", <event>]
            ev = arr[1] if len(arr) == 2 else arr[2]
            if isinstance(ev, str):  # некоторые релеи шлют event как json-строку
                try:
                    ev = json.loads(ev)
                except Exception:
                    return
            if isinstance(ev, dict):
                got.append(ev)

    ws = websocket.WebSocketApp(
        relay,
        on_message=on_message,
        on_open=lambda ws: ws.send(json.dumps(
            ["REQ", "e2e", {"kinds": [1059], "#p": [recipient_pub], "limit": 5}]
        )),
    )

    import threading

    # run_forever блокирует — запускаем в потоке, ждём с таймаутом
    threading.Thread(
        target=ws.run_forever, kwargs={"ping_interval": 10, "ping_timeout": 5}, daemon=True
    ).start()

    deadline = time.time() + 45
    while time.time() < deadline and not got:
        time.sleep(0.5)
    try:
        ws.close()
    except Exception:
        pass

    if not got:
        print("❌ событие не вернулось с релея за 45 сек — контур не замкнут")
        return 1

    # 4. расшифровка через мост
    event = got[0]
    print(f"получено событие: {event['id'][:16]}…")
    accepted_by_bridge = bridge.handle_event(event)
    if not accepted_by_bridge:
        print("❌ мост не принял письмо (unwrap/parse не сработал)")
        return 1

    with bridge._connect() as conn:
        row = conn.execute("SELECT subject, body, from_addr FROM inbox").fetchone()
    print(f"✅ письмо в inbox: subject={row[0]!r} body={row[1]!r} from={row[2]!r}")
    assert row[0] == "E2E тест моста"
    assert "e2e" in row[1]
    print("\n✅✅ E2E КОНТУР ЗАМКНУТ: публикация → релей → чтение → расшифровка → inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
