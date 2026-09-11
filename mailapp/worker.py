"""Воркер очереди (Фаза 4): расшифровывает письма из mail_queue.

Пул: N воркеров (N = min(CPU, аккаунты/5), на сервере 1 ядро → 1).
Каждый воркер держит ключи ТОЛЬКО своей группы владельцев
(sha256(pubkey)[0] % N == group_id) — приватные ключи не дублируются
по всем процессам и не попадают в процесс подписчика (WS-периметр).

Подписку на релеи воркеры не ведут — её держит мост-подписчик (без ключей).
Падение воркера не теряет письма: задача остаётся processing → reclaim_stale
вернёт её в pending, другой/новый воркер заберёт.

Новые регистрации: каждые 60с воркер синхронизирует владельцев из БД
(accounts) и догружает ключи своей группы — как мост-подписчик.

Запуск: python3 -m mailapp.worker   (env: MAIL_WORKERS, MAIL_WORKER_ID)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time

from .config import BASE, CFG, DB, OWNERS, RELAYS
from . import queue as q

SYNC_INTERVAL = 60  # сек: синхронизация владельцев из БД
RECLAIM_INTERVAL = 30  # сек: периодический возврат зависших задач (независимо от простоя очереди)


def reclaim_due(last_reclaim: float, now_mono: float, interval: int = RECLAIM_INTERVAL) -> bool:
    """Пора ли снова вернуть зависшие задачи в очередь.

    Решение привязано к monotonic-времени, а не к числу пустых итераций:
    при непрерывной нагрузке очередь никогда не пуста, empty_loops остаётся 0,
    и триггер по пустой очереди не срабатывает вообще.
    """
    return now_mono - last_reclaim >= interval


def _group_of(pubkey_hex: str, n: int) -> int:
    """Стабильное распределение владельцев по воркерам."""
    return hashlib.sha256(pubkey_hex.encode()).digest()[0] % max(1, n)


def _fail_reason(bridge, err: str = "") -> str:
    """Настоящая причина отказа задачи.

    Раньше в БД уходила общая формулировка про «ключи группы» — она
    подставлялась при любом отказе без исключения и скрывала реальную причину
    (чужой kind внутри gift wrap, пустой From и т.п.). Порядок выбора:
    исключение → причина от моста → честная общая формулировка.
    """
    if err:
        return err
    return getattr(bridge, "last_error", "") or ""


def _is_permanent(bridge) -> bool:
    """Отказ, который повтор не исправит (чужой kind, неразбираемый контент).

    Такие задачи уходят в failed сразу: раньше воркер тратил на них три
    попытки, то есть три расшифровки на событие, которое письмом не станет.
    """
    return bool(getattr(bridge, "last_error_permanent", False))


def _setup_logging():
    logger = logging.getLogger("mail.worker")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
    logger.propagate = False


def _load_bridges(my_owners: list) -> dict:
    """MailBridge только для своих владельцев (ключи группы, не всех)."""
    from .auth import get_mail_key
    from mailbridge.mail_bridge import MailBridge

    bridges: dict = {}
    sys.path.insert(0, os.path.join(BASE, "..", "..", "projects", "nostr-mail-bridge", "src"))
    for o in my_owners:
        nsec = None
        try:
            nsec = get_mail_key(o["pubkey_hex"])
        except Exception:
            pass
        nsec = nsec or o.get("nsec_hex")
        if not nsec:
            logging.getLogger("mail.worker").warning("нет ключа у %s", o.get("label"))
            continue
        try:
            bridges[o["pubkey_hex"]] = MailBridge(
                privkey_hex=nsec,
                relays=RELAYS,
                db_path=DB,
                telegram_token=CFG.get("telegram_token", ""),
                telegram_chat_id=CFG.get("telegram_chat_id", ""),
                owner=o["pubkey_hex"],
                label=o.get("label") or "Крайтер",
                max_inbox=int(CFG.get("limits", {}).get("max_mails_per_user", 500)),
            )
        except Exception as e:
            logging.getLogger("mail.worker").error("bridge %s: %s", o.get("label"), e)
    return bridges


def _sync_owners_and_keys(i: int, n: int, bridges: dict) -> set:
    """Подтягивает новых владельцев из БД и догружает ключи своей группы.

    Возвращает актуальный набор pubkey группы (для claim).
    """
    from .auth import sync_owners_from_accounts

    sync_owners_from_accounts()
    for o in OWNERS:
        if _group_of(o["pubkey_hex"], n) == i and o["pubkey_hex"] not in bridges:
            new_b = _load_bridges([o])
            if new_b:
                bridges.update(new_b)
    return {o["pubkey_hex"] for o in OWNERS if _group_of(o["pubkey_hex"], n) == i}


def run_worker(worker_id: int = 0, total: int = 1) -> None:
    _setup_logging()
    log = logging.getLogger("mail.worker")
    n = max(1, total)
    i = worker_id % n
    q.ensure_schema()
    q.cleanup_seen()  # подчистить старые id виденных событий
    q.cleanup_workers(ttl=600)  # забыть мёртвые heartbeat (workers_total честный)

    # зависшие задачи от упавших воркеров → обратно в очередь
    reclaimed = q.reclaim_stale()
    if reclaimed:
        log.info("reclaim: %d зависших задач возвращены в очередь", reclaimed)

    my_pubkeys = {o["pubkey_hex"] for o in OWNERS if _group_of(o["pubkey_hex"], n) == i}
    bridges = _load_bridges([o for o in OWNERS if _group_of(o["pubkey_hex"], n) == i])
    log.info(
        "воркер %d/%d (pid %d): владельцев в группе %d, ключей %d, очередь %s",
        i, n, os.getpid(), len(my_pubkeys), len(bridges), DB,
    )
    wid = f"w{i}-{os.getpid()}"
    empty_loops = 0
    last_sync = time.time()
    last_sync_state = None  # (владельцы, ключи) — логировать только при изменении
    last_reclaim = time.monotonic()  # таймер возврата зависших задач, не зависит от empty_loops
    while True:
        try:
            if time.time() - last_sync > SYNC_INTERVAL:
                my_pubkeys = _sync_owners_and_keys(i, n, bridges)
                state = (len(my_pubkeys), len(bridges))
                if state != last_sync_state:
                    log.info("sync владельцев: группа %d → %d (ключей %d)", i, len(my_pubkeys), len(bridges))
                    last_sync_state = state
                last_sync = time.time()

            # Возврат зависших задач по таймеру, а не по пустой очереди.
            # Между заданиями: обработчик, который никогда не возвращается, не прерывается.
            now_mono = time.monotonic()
            if reclaim_due(last_reclaim, now_mono):
                n_reclaimed = q.reclaim_stale()
                last_reclaim = now_mono
                if n_reclaimed:
                    log.info("reclaim (таймер): %d зависших задач возвращены в очередь", n_reclaimed)

            q.heartbeat(wid, i)
            row = q.claim(my_pubkeys, wid)
            if row is None:
                empty_loops += 1
                time.sleep(0.2 if empty_loops % 5 == 0 else 0.8)
                continue
            empty_loops = 0
            ev = json.loads(row["payload"])
            owner = row["owner"]
            candidates = [bridges[owner]] if owner in bridges else list(bridges.values())
            ok, err = False, ""
            reason, permanent = "", False
            for br in candidates:
                try:
                    if br.handle_event(ev):
                        ok = True
                        break
                    reason = _fail_reason(br, err)
                    permanent = _is_permanent(br)
                except Exception as e:
                    err = str(e)
                    reason = str(e)
                    permanent = False  # исключение может быть временным — повторим
            if ok:
                q.finish(row["id"], True, lease=row.get("lease", ""))
                log.info("письмо %s → inbox (owner %s…)", ev.get("id", "?")[:12], owner[:8])
            else:
                reason = reason or "не принято ни одним мостом (причина не сообщена)"
                q.finish(row["id"], False, reason, lease=row.get("lease", ""), permanent=permanent)
                log.info("задача %d не принята%s: %s",
                         row["id"], " окончательно" if permanent else "", reason)
        except Exception as e:
            log.error("воркер: %s", e)
            time.sleep(2)


if __name__ == "__main__":
    total = int(os.environ.get("MAIL_WORKERS", "1"))
    wid = int(os.environ.get("MAIL_WORKER_ID", "0"))
    run_worker(wid, total)
