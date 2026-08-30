"""Тесты очереди писем (Фаза 4): enqueue/claim/finish/reclaim/metrics.

Проверяем гарантии спеки: гонка (1 задача — 1 воркер), фильтр по группе,
переживание рестарта (reclaim), попытки до failed, метрики.
"""
import json
import sqlite3
import time

import pytest

import mailapp.queue as q


@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    """Очередь на временной БД."""
    db = str(tmp_path / "queue.db")
    monkeypatch.setattr(q, "DB", db)
    q.ensure_schema()
    return db


def _ev(owner_hex: str | None = None) -> dict:
    tags = [] if owner_hex is None else [["p", owner_hex]]
    return {"id": "e" * 64, "kind": 1059, "pubkey": "a" * 64, "tags": tags, "content": "x", "sig": "s" * 128}


def test_enqueue_extracts_owner(queue_db):
    qid = q.enqueue(_ev("B" * 64))
    with sqlite3.connect(queue_db) as c:
        row = c.execute("SELECT owner, status FROM mail_queue WHERE id=?", (qid,)).fetchone()
    assert row == ("B" * 64, "pending")


def test_enqueue_no_p_tag_owner_empty(queue_db):
    qid = q.enqueue(_ev())
    with sqlite3.connect(queue_db) as c:
        owner = c.execute("SELECT owner FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert owner == ""


def test_claim_race_single_winner(queue_db):
    """Два воркера на одну задачу — забирает ровно один."""
    q.enqueue(_ev("A" * 64))
    first = q.claim(groups={"A" * 64}, worker="w1")
    second = q.claim(groups={"A" * 64}, worker="w2")
    assert first is not None
    assert second is None  # задача уже processing
    with sqlite3.connect(queue_db) as c:
        status, worker = c.execute("SELECT status, worker FROM mail_queue WHERE id=?", (first["id"],)).fetchone()
    assert (status, worker) == ("processing", "w1")


def test_claim_group_filter(queue_db):
    """Воркер группы B не берёт задачу владельца A."""
    q.enqueue(_ev("A" * 64))
    got = q.claim(groups={"B" * 64}, worker="wB")
    assert got is None


def test_claim_any_owner_when_empty(queue_db):
    """Задача без owner доступна любому воркеру."""
    q.enqueue(_ev())
    got = q.claim(groups={"B" * 64}, worker="wB")
    assert got is not None
    assert got["owner"] == ""


def test_finish_ok_marks_done(queue_db):
    qid = q.enqueue(_ev("A" * 64))
    row = q.claim(groups={"A" * 64}, worker="w1")
    q.finish(row["id"], True)
    with sqlite3.connect(queue_db) as c:
        status = c.execute("SELECT status FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert status == "done"


def test_finish_fail_counts_attempts_then_failed(queue_db):
    qid = q.enqueue(_ev("A" * 64))
    for i in range(q.MAX_ATTEMPTS):
        row = q.claim(groups={"A" * 64}, worker="w1")
        q.finish(row["id"], False, "не расшифровано")
    with sqlite3.connect(queue_db) as c:
        status, attempts = c.execute("SELECT status, attempts FROM mail_queue WHERE id=?", (qid,)).fetchone()
    assert status == "failed"
    assert attempts == q.MAX_ATTEMPTS


def test_finish_fail_returns_to_pending_before_max(queue_db):
    qid = q.enqueue(_ev("A" * 64))
    row = q.claim(groups={"A" * 64}, worker="w1")
    q.finish(row["id"], False, "попробуй ещё")
    with sqlite3.connect(queue_db) as c:
        status, attempts = c.execute("SELECT status, attempts FROM mail_queue WHERE id=?", (qid,)).fetchone()
    assert status == "pending"  # вернулась в очередь
    assert attempts == 1


def test_reclaim_returns_stale_processing(queue_db):
    """Воркер упал → задача processing с истёкшим started_at → снова pending."""
    qid = q.enqueue(_ev("A" * 64))
    row = q.claim(groups={"A" * 64}, worker="w1")
    # искусственно состарим started_at
    with sqlite3.connect(queue_db) as c:
        c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, qid))
        c.commit()
    n = q.reclaim_stale(timeout=60)
    assert n == 1
    with sqlite3.connect(queue_db) as c:
        status = c.execute("SELECT status FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert status == "pending"
    # и задача снова доступна
    assert q.claim(groups={"A" * 64}, worker="w2") is not None


def test_queue_survives_restart(queue_db):
    """Очередь переживает «рестарт»: задача в БД, новый воркер её обработает."""
    qid = q.enqueue(_ev("A" * 64))
    row = q.claim(groups={"A" * 64}, worker="old")
    # «воркер упал», ничего не сделал — состарим started_at
    with sqlite3.connect(queue_db) as c:
        c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, qid))
        c.commit()
    # новый воркер: reclaim + claim → та же задача
    q.reclaim_stale(timeout=60)
    again = q.claim(groups={"A" * 64}, worker="new")
    assert again is not None
    assert again["id"] == row["id"]
    q.finish(again["id"], True)
    with sqlite3.connect(queue_db) as c:
        status = c.execute("SELECT status FROM mail_queue WHERE id=?", (qid,)).fetchone()[0]
    assert status == "done"


def test_metrics_counts(queue_db):
    q.enqueue(_ev("A" * 64))
    row = q.claim(groups={"A" * 64}, worker="w1")
    q.finish(row["id"], True)
    q.heartbeat("w1", 0)
    m = q.metrics()
    assert m["pending"] == 0
    assert m["done_1m"] == 1
    assert m["workers_alive"] == 1
    assert m["workers_total"] == 1


def test_enqueue_deduplicates_same_event(queue_db):
    """Повтор того же события (с другого релея) в очередь не попадает."""
    ev = _ev("A" * 64)
    first = q.enqueue(ev)
    second = q.enqueue(ev)
    assert first is not None
    assert second is None  # дедуп по event id
    with sqlite3.connect(queue_db) as c:
        n = c.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0]
    assert n == 1


def test_enqueue_different_events_both_queued(queue_db):
    q.enqueue({"id": "e" + "1" * 63, "kind": 1059, "tags": [["p", "A" * 64]], "content": "x"})
    q.enqueue({"id": "e" + "2" * 63, "kind": 1059, "tags": [["p", "A" * 64]], "content": "y"})
    with sqlite3.connect(queue_db) as c:
        n = c.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0]
    assert n == 2
