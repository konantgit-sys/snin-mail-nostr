"""Мосты (MailBridge): ОДИН общий подписчик на ВСЕ ящики.

Архитектура v2 (2026-08-27):
- Раньше: по потоку на каждый ящик × релей = 3 ящика × 3 релея = 9 соединений.
- Теперь: мосты создаются для КАЖДОГО владельца (нужны для расшифровки своим
  ключом, квот и уведомлений), но БЕЗ собственных потоков. Подписку ведёт один
  общий SharedSubscriber: поток на релей (3 потока на все ящики), filter #p =
  [все pubkey владельцев]. Событие передаётся каждому мосту — расшифрует тот,
  чей ключ подходит, и сохранит письмо со своим owner.

Все мосты пишут в общую БД (inbox.db), письма помечаются owner (pubkey владельца).
NO_BRIDGE=1 (тесты) — мосты не стартуют, get_bridge() вернёт None.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from .config import BASE, CFG, DB, OWNERS, RELAYS, LIMITS

_bridges: dict[str, object] = {}
_subscriber = None
_lock = threading.Lock()


def _build_bridge(o: dict):
    """Создаёт MailBridge для владельца без запуска потоков (только обработка)."""
    from mailbridge.mail_bridge import MailBridge  # local import: тяжёлый
    nsec = _nsec_for(o)
    if not nsec:
        return None
    return MailBridge(
        privkey_hex=nsec,
        relays=RELAYS,
        db_path=DB,
        telegram_token=CFG.get("telegram_token", ""),
        telegram_chat_id=CFG.get("telegram_chat_id", ""),
        owner=o["pubkey_hex"],
        label=o["label"],
        max_inbox=LIMITS["max_mails_per_user"],
    )


class SharedSubscriber:
    """Один общий подписчик на все pubkey владельцев. Поток на релей."""

    def __init__(self, bridges: list, relays: list):
        self.bridges = bridges            # list[MailBridge] (без своих потоков)
        self.relays = relays
        self._stop = threading.Event()
        self._ws_list: list = []
        self._ws_lock = threading.Lock()
        self._subid = "mb-shared-1"

    def _pubkeys(self) -> list:
        return [b.pubkey for b in self.bridges]

    def start(self):
        for url in self.relays:
            threading.Thread(target=self._run_relay, args=(url,), daemon=True).start()
        logging.getLogger("mailbridge").info(
            "общий подписчик: %d владельцев × %d релеев (1 поток на релей)", len(self.bridges), len(self.relays))

    def stop(self):
        self._stop.set()
        for ws in list(self._ws_list):
            try:
                ws.close()
            except Exception:
                pass

    def add_bridge(self, b) -> None:
        """Новый владелец: добавляем в общий список и пере-подписываемся."""
        self.bridges.append(b)
        self._resubscribe()

    def _resubscribe(self):
        """CLOSE старой подписки + REQ с обновлённым #p на каждом живом ws."""
        filter_ = {"kinds": [1059, 1301], "#p": self._pubkeys(), "limit": 100}
        with self._ws_lock:
            for ws in list(self._ws_list):
                try:
                    ws.send(json.dumps(["CLOSE", self._subid]))
                    ws.send(json.dumps(["REQ", self._subid, filter_]))
                except Exception:
                    pass

    def _run_relay(self, url: str):
        """Подписка на релей с экспоненциальным бэкоффом и алертом.

        2026-09-11: раньше при обрыве шёл фиксированный реконнект каждые 5с
        без единого уведомления — почта молча простояла 16 часов, потому что
        релей отдавал 1013 (слоты занимала утечка в relay_gateway).
        Теперь: задержка растёт 5→10→20→40→60с, при удержании соединения
        ≥30с она сбрасывается, а на 6-й неудаче подряд уходит один алерт в
        группу (не чаще раза в 30 минут).
        """
        import websocket
        log = logging.getLogger("mailbridge")
        delay, fails = 5, 0
        while not self._stop.is_set():
            connected_at = None

            try:
                def on_open(ws):
                    nonlocal connected_at
                    with self._ws_lock:
                        if ws not in self._ws_list:
                            self._ws_list.append(ws)
                    connected_at = time.time()
                    # история релея читается при каждом подключении: сохраняем письма,
                    # но НЕ шлём их в Telegram (иначе рестарт моста = залп повторов)
                    for b in self.bridges:
                        b.suppress_notify = True
                    filter_ = {"kinds": [1059, 1301], "#p": self._pubkeys(), "limit": 100}
                    ws.send(json.dumps(["REQ", self._subid, filter_]))

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
                        # история отдана — дальше живой поток, уведомления включаем
                        for b in self.bridges:
                            b.suppress_notify = False

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

            if self._stop.is_set():
                break

            # соединение жило долго → это не «релей лежит», сбрасываем бэкофф
            if connected_at and (time.time() - connected_at) >= 30:
                delay, fails = 5, 0
            else:
                fails += 1
                delay = min(delay * 2, 60)
                log.warning("релей %s недоступен (%d подряд), повтор через %dс", url, fails, delay)
                if fails == 6:
                    self._alert(url, fails)
            self._stop.wait(delay)

    def _alert(self, url: str, fails: int) -> None:
        """Один алерт в группу при длительной недоступности релея (не чаще 1/30 мин)."""
        now = time.time()
        if now - getattr(self, "_last_alert", 0.0) < 1800:
            return
        self._last_alert = now
        text = (f"⚠️ SNIN Mail: релей {url} недоступен, {fails} неудачных попыток подряд.\n"
                f"Письма не принимаются. Проверь слоты на релее (:8198) и утечку в relay_gateway.")
        for b in self.bridges:
            try:
                if getattr(b, "telegram_token", "") and getattr(b, "telegram_chat_id", ""):
                    b.notify_telegram(text)
                    return
            except Exception:
                continue

    def _forget(self, ws):
        with self._ws_lock:
            try:
                if ws in self._ws_list:
                    self._ws_list.remove(ws)
            except Exception:
                pass

    def _dispatch(self, ev: dict):
        """Передаёт событие каждому мосту: расшифрует тот, чей ключ подходит."""
        for b in list(self.bridges):
            try:
                if b.handle_event(ev):
                    return  # письмо принято — событие больше не нужно
            except Exception as e:
                logging.getLogger("mailbridge").debug("dispatch: %s", e)


def init_bridge():
    """Стартует ОДИН общий подписчик на все ящики. NO_BRIDGE=1 — пропустить (тесты)."""
    global _bridges, _subscriber
    with _lock:
        if _bridges or os.environ.get("NO_BRIDGE") == "1":
            return
        _setup_logging()
        sys.path.insert(0, os.path.join(BASE, "..", "..", "projects", "nostr-mail-bridge", "src"))

        bridges = []
        for o in OWNERS:
            b = _build_bridge(o)
            if b is None:
                continue
            _bridges[o["pubkey_hex"]] = b
            bridges.append(b)

        # старые письма (до мульти-ящика) — первому владельцу
        try:
            import sqlite3
            with sqlite3.connect(DB, timeout=15) as conn:
                conn.execute("UPDATE inbox SET owner=? WHERE owner=''", (OWNERS[0]["pubkey_hex"],))
                conn.commit()
        except Exception:
            pass

        if not bridges:
            return
        _subscriber = SharedSubscriber(bridges, RELAYS)
        _subscriber.start()


def _nsec_for(o: dict) -> str | None:
    """Приватный ключ владельца: из mail_keys (зашифрованное хранилище) → fallback config."""
    try:
        from .auth import get_mail_key
        k = get_mail_key(o["pubkey_hex"])
        if k:
            return k
    except Exception:
        pass
    return o.get("nsec_hex") or None


def add_owner(o: dict) -> bool:
    """Динамическая регистрация владельца: добавляем в общий подписчик.

    Вызывается при регистрации нового ящика (POST /api/register).
    В NO_BRIDGE (тесты) — только регистрация в cfg, без подписки.
    """
    global _bridges, _subscriber
    with _lock:
        from . import config as cfg
        if o["pubkey_hex"] in _bridges:
            return False
        if o["pubkey_hex"] not in cfg.OWNER_INDEX:
            cfg.OWNERS.append(o)
            cfg.OWNER_INDEX[o["pubkey_hex"]] = o
        if os.environ.get("NO_BRIDGE") == "1":
            _bridges[o["pubkey_hex"]] = None
            return True
        b = _build_bridge(o)
        if b is None:
            return False
        _bridges[o["pubkey_hex"]] = b
        if _subscriber is not None:
            _subscriber.add_bridge(b)
        else:
            # подписчик ещё не стартовал (init_bridge ещё не вызывался) — стартуем
            _subscriber = SharedSubscriber([b], RELAYS)
            _subscriber.start()
        return True


def get_bridge(owner: str | None = None):
    """Мост владельца (по умолчанию — первый). None в тестах (NO_BRIDGE)."""
    if not _bridges:
        return None
    if owner and owner in _bridges:
        return _bridges[owner]
    return _bridges[list(_bridges)[0]]


def _setup_logging():
    """Логи моста в веб-режиме: mailbridge → INFO → stdout (backend.log).
    Раньше basicConfig был только в CLI main() — в веб-режиме логи терялись."""
    logger = logging.getLogger("mailbridge")
    if logger.handlers:  # уже настроен
        return
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
    logger.propagate = False


def main() -> None:
    """Точка входа отдельного процесса моста (start.sh: python3 -m mailapp.bridge).

    2026-09-11: блока не было вообще, поэтому `python3 -m mailapp.bridge`
    импортировал модуль и завершался с кодом 0 — процесс моста не жил, подписки
    на релей не было, и письма не принимались. Держим процесс живым явно.
    """
    init_bridge()
    if _subscriber is None:
        logging.getLogger("mailbridge").error("мост не стартовал: подписчик не создан")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        if _subscriber is not None:
            _subscriber.stop()


if __name__ == "__main__":
    main()
