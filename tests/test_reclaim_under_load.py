"""Возврат зависших задач при НЕПРЕРЫВНОЙ нагрузке (находка Recluse's Codex).

Сценарий, который до этой правки не покрывался:
воркер умирает после claim J; замена стартует раньше, чем истечёт RECLAIM_TIMEOUT;
дальше подходящая pending-работа не заканчивается. Старый триггер возврата жил
внутри ветки «очередь пуста» (empty_loops % 30 == 0), а успешный claim обнуляет
empty_loops — значит при непрерывной нагрузке эта ветка не выполнялась вообще,
и J могла остаться в processing неограниченно долго.

Тест планирования/состояния очереди: без сети, без IMAP, без реальных часов.
"""
import json
import sqlite3
import time

import pytest

import mailapp.queue as q
from mailapp.worker import RECLAIM_INTERVAL, reclaim_due


@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    """Очередь на временной БД (тот же приём, что в test_queue.py)."""
    db = str(tmp_path / "queue.db")
    monkeypatch.setattr(q, "DB", db)
    q.ensure_schema()
    return db


def _ev(owner_hex: str) -> dict:
    return {"id": "e" * 64, "kind": 1059, "pubkey": "a" * 64,
            "tags": [["p", owner_hex]], "content": "x", "sig": "s" * 128}


# ── 1. Границы решения ────────────────────────────────────────────────────

def test_reclaim_due_boundaries():
    """Реже интервала — нет, на границе и позже — да."""
    assert reclaim_due(100.0, 100.0 + RECLAIM_INTERVAL - 0.001) is False
    assert reclaim_due(100.0, 100.0) is False
    assert reclaim_due(100.0, 100.0 + RECLAIM_INTERVAL) is True
    assert reclaim_due(100.0, 100.0 + 3 * RECLAIM_INTERVAL) is True


# ── 2. Почему старый триггер молчал под нагрузкой ────────────────────────

def test_old_empty_loop_trigger_never_fires_under_load():
    """Фиксируем сам дефект числами, чтобы регресс был виден сразу.

    Два прогона по 180 циклов:
      * непрерывная нагрузка — claim всегда успешен, ветка «очередь пуста»
        не выполняется ни разу, старый триггер даёт 0 вызовов;
      * новая формула поверх того же прогона даёт 6 вызовов.
    Обратите внимание: проверка старого триггера стоит ВНУТРИ ветки «пусто» —
    иначе 0 % 30 == 0 давало бы ложное срабатывание на каждом цикле.
    """
    cycles = 180
    empty_loops = 0
    old_triggers = 0
    new_triggers = 0
    last_reclaim = 0.0

    for t in range(1, cycles + 1):
        claimed = True  # непрерывная нагрузка: работа есть всегда
        if claimed:
            empty_loops = 0
        else:
            empty_loops += 1
            if empty_loops % 30 == 0:
                old_triggers += 1

        if reclaim_due(last_reclaim, float(t)):
            new_triggers += 1
            last_reclaim = float(t)

    assert old_triggers == 0, "под непрерывной нагрузкой старый триггер не вызывался"
    assert new_triggers == cycles // RECLAIM_INTERVAL == 6


# ── 3. Задача реально возвращается и обрабатывается ──────────────────────

def test_job_returns_to_pending_under_continuous_load(queue_db):
    """J, чей воркер умер, возвращается в pending по таймеру, а не по простою."""
    qid = q.enqueue(_ev("A" * 64))
    assert q.claim(groups={"A" * 64}, worker="w1") is not None, "J должна быть взята в работу"

    # воркер w1 «умер»: состарим claim за предел RECLAIM_TIMEOUT
    with sqlite3.connect(queue_db) as c:
        c.execute("UPDATE mail_queue SET started_at=? WHERE id=?",
                  (int(time.time()) - (q.RECLAIM_TIMEOUT + 1), qid))
        c.commit()

    # 180 секунд непрерывной нагрузки: работа в очереди есть всегда
    last_reclaim = 0.0
    reclaimed_total = 0
    for t in range(1, 181):
        if reclaim_due(last_reclaim, float(t)):
            reclaimed_total += q.reclaim_stale()
            last_reclaim = float(t)

    assert reclaimed_total >= 1, "зависшая J должна быть возвращена в очередь"
    with sqlite3.connect(queue_db) as c:
        status = c.execute("SELECT status FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert status == "pending"

    # и задача снова доступна живому воркеру
    row = q.claim(groups={"A" * 64}, worker="w2")
    assert row is not None and row["id"] == qid

    # повторный явный reclaim уже не находит, что возвращать
    assert q.reclaim_stale() == 0


def test_no_premature_reclaim_before_interval(queue_db):
    """Свежий claim не отбирается: ни по таймеру, ни по timeout."""
    qid = q.enqueue(_ev("A" * 64))
    assert q.claim(groups={"A" * 64}, worker="w1") is not None

    # таймер молчит весь первый интервал (проверяем до границы, не включая её)
    last_reclaim = 0.0
    for t in range(0, RECLAIM_INTERVAL):
        assert reclaim_due(last_reclaim, float(t)) is False

    assert q.reclaim_stale() == 0, "свежий claim не должен возвращаться"
    with sqlite3.connect(queue_db) as c:
        status = c.execute("SELECT status FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert status == "processing"
