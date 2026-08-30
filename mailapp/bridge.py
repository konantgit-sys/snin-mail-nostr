"""Мосты (MailBridge) v3 (Фаза 4): подписчик БЕЗ приватных ключей + очередь.

Архитектура v3 (2026-08-29):
- v1: поток на ящик × релей.
- v2: общий SharedSubscriber, каждый мост расшифровывал сам (все nsec в одном
  процессе, расшифровка в потоке WS — блокировала приём).
- v3: подписчик НЕ держит приватные ключи вообще (в процессе с сетевым
  периметром WS нет секретов). Событие → очередь (mail_queue, INSERT —
  миллисекунды). Расшифровывают воркеры (mailapp.worker) — отдельные процессы,
  каждый только со своей группой ключей.

Публикация (отправка): воркеры API публикуют напрямую через publish_direct
(мост в отдельном процессе — get_bridge() → None). NO_BRIDGE=1 (тесты) —
подписка не стартует, publish_direct возвращает [].
"""
from __future__ import annotations

import json
import logging
import os
import threading

from .config import CFG, DB, OWNERS, RELAYS
from . import queue as queue_mod

_pubkeys: list[str] = []
_subscriber = None
_lock = threading.Lock()


class SharedSubscriber:
    """Один общий подписчик на все pubkey владельцев. Поток на релей.

    События не расшифровывает: кладёт в mail_queue (owner = первый p-тег).
    """

    def __init__(self, pubkeys: list, relays: list):
        self.pubkeys = pubkeys
        self.relays = relays
        self._stop = threading.Event()
        self._ws_list: list = []
        self._ws_lock = threading.Lock()
        self._subid = "mb-shared-1"

    def _filter(self) -> dict:
        return {"kinds": [1059, 1301], "#p": list(self.pubkeys), "limit": 100}

    def start(self):
        for url in self.relays:
            threading.Thread(target=self._run_relay, args=(url,), daemon=True).start()
        logging.getLogger("mailbridge").info(
            "подписчик: %d владельцев × %d релеев (ключи не загружены, события → очередь)",
            len(self.pubkeys), len(self.relays))

    def stop(self):
        self._stop.set()
        for ws in list(self._ws_list):
            try:
                ws.close()
            except Exception:
                pass

    def add_owner(self, pubkey_hex: str) -> None:
        """Новый владелец: добавляем pubkey и пере-подписываемся."""
        if pubkey_hex in self.pubkeys:
            return
        self.pubkeys.append(pubkey_hex)
        self._resubscribe()

    def _resubscribe(self):
        """CLOSE старой подписки + REQ с обновлённым #p на каждом живом ws."""
        filter_ = self._filter()
        with self._ws_lock:
            for ws in list(self._ws_list):
                try:
                    ws.send(json.dumps(["CLOSE", self._subid]))
                    ws.send(json.dumps(["REQ", self._subid, filter_]))
                except Exception:
                    pass

    def _run_relay(self, url: str):
        import websocket

        while not self._stop.is_set():
            try:
                def on_open(ws):
                    with self._ws_lock:
                        if ws not in self._ws_list:
                            self._ws_list.append(ws)
                    ws.send(json.dumps(["REQ", self._subid, self._filter()]))

                def on_message(ws, message):
                    try:
                        arr = json.loads(message)
                    except Exception:
                        return
                    if not isinstance(arr, list) or not arr:
                        return
                    if arr[0] == "EVENT":
                        ev = arr[1] if len(arr) == 2 else arr[2]
                        if isinstance(ev, str):
                            try:
                                ev = json.loads(ev)
                            except Exception:
                                return
                        if isinstance(ev, dict):
                            self._dispatch(ev)
                    elif arr[0] == "EOSE":
                        pass  # история загружена, дальше — стрим

                ws = websocket.WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=lambda ws, err: logging.getLogger("mailbridge").debug("%s error: %s", url, err),
                    on_close=lambda ws, *a: self._forget(ws),
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logging.getLogger("mailbridge").debug("%s crashed: %s", url, e)
            finally:
                self._forget(ws)
            if not self._stop.is_set():
                logging.getLogger("mailbridge").info("реконнект %s через 5с", url)
                self._stop.wait(5)

    def _forget(self, ws):
        with self._ws_lock:
            try:
                if ws in self._ws_list:
                    self._ws_list.remove(ws)
            except Exception:
                pass

    def _dispatch(self, ev: dict):
        """Событие → очередь. Расшифровку делает воркер (не блокируем WS-поток)."""
        try:
            queue_mod.enqueue(ev)
        except Exception as e:
            logging.getLogger("mailbridge").debug("enqueue: %s", e)


def init_bridge():
    """Стартует ОДИН общий подписчик (без ключей). NO_BRIDGE=1 — пропустить (тесты)."""
    global _pubkeys, _subscriber
    with _lock:
        if _pubkeys or os.environ.get("NO_BRIDGE") == "1":
            return
        _setup_logging()

        pubkeys = [o["pubkey_hex"] for o in OWNERS if o.get("pubkey_hex")]

        # старые письма (до мульти-ящика) — первому владельцу
        try:
            import sqlite3
            with sqlite3.connect(DB, timeout=15) as conn:
                conn.execute("UPDATE inbox SET owner=? WHERE owner=''", (OWNERS[0]["pubkey_hex"],))
                conn.commit()
        except Exception:
            pass

        if not pubkeys:
            return
        _pubkeys = pubkeys
        _subscriber = SharedSubscriber(pubkeys, RELAYS)
        _subscriber.start()


def add_owner(o: dict) -> bool:
    """Динамическая регистрация владельца: pubkey в подписку (без ключей).

    Вызывается при регистрации нового ящика (POST /api/register).
    В NO_BRIDGE (тесты) — только регистрация в cfg, без подписки.
    """
    global _pubkeys, _subscriber
    with _lock:
        from . import config as cfg
        if o["pubkey_hex"] in _pubkeys:
            return False
        if o["pubkey_hex"] not in cfg.OWNER_INDEX:
            cfg.OWNERS.append(o)
            cfg.OWNER_INDEX[o["pubkey_hex"]] = o
        if os.environ.get("NO_BRIDGE") == "1":
            _pubkeys.append(o["pubkey_hex"])
            return True
        if _subscriber is not None:
            _subscriber.add_owner(o["pubkey_hex"])
        else:
            # подписчик ещё не стартовал — стартуем с текущим списком
            _pubkeys = list(_pubkeys) + [o["pubkey_hex"]]
            _subscriber = SharedSubscriber(_pubkeys, RELAYS)
            _subscriber.start()
        return True


def get_bridge(owner: str | None = None):
    """Совместимость: мост больше не держит MailBridge (Фаза 4) → всегда None.

    Отправка идёт через publish_direct (см. routers/mail.py).
    """
    return None


def publish_direct(event: dict, relays: list | None = None) -> list:
    """Публикация события напрямую из воркера API.

    Мост живёт в отдельном процессе (Фаза 0), поэтому воркер публикует сам.
    Публикация stateless — подписка не нужна, воркер шлёт EVENT сам.
    NO_BRIDGE=1 (тесты) — не публикуем, возвращаем [] (как раньше).
    """
    import time

    import websocket

    if os.environ.get("NO_BRIDGE") == "1":
        return []
    relays = relays or RELAYS
    accepted: list = []
    payload = json.dumps(["EVENT", event], separators=(",", ":"))
    for url in relays:
        try:
            ws = websocket.create_connection(url, timeout=6)
            ws.send(payload)
            deadline = time.time() + 4
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
            logging.getLogger("mailbridge").debug("publish_direct %s: %s", url, e)
    logging.getLogger("mailbridge").info("publish_direct: %d/%d релеев", len(accepted), len(relays))
    return accepted


def _setup_logging():
    """Логи моста в веб-режиме: mailbridge → INFO → stdout (backend.log)."""
    logger = logging.getLogger("mailbridge")
    if logger.handlers:  # уже настроен
        return
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
    logger.propagate = False


def run_bridge_process():
    """Отдельный процесс подписчика (Фаза 0+4): без ключей, события → очередь.

    Периодически синхронизирует владельцев из БД, чтобы новые регистрации
    получали подписку без рестарта.
    """
    import time as _t

    _setup_logging()
    init_bridge()
    log = logging.getLogger("mail.bridge")
    log.info("bridge process started (subscriber, %d owners, ключи не загружены)", len(OWNERS))
    while True:
        _t.sleep(60)
        try:
            from .auth import sync_owners_from_accounts
            sync_owners_from_accounts()
            for o in list(OWNERS):
                add_owner(o)
        except Exception as e:
            log.error("sync owners: %s", e)


if __name__ == "__main__":
    run_bridge_process()
