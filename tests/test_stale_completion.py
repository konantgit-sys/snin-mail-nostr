"""Characterization tests for the stale-completion gap in the mail queue.

Finding (external review, board seq 29406): `finish()` updates a queue row by
`id` only, so a worker whose task was reclaimed after a stall can still write to
the row that a *different* worker now holds.

These tests DESCRIBE THE CURRENT BEHAVIOUR on purpose: they stay green so the
defect is visible in CI and cannot silently change. When the fenced contract
(id + status + worker lease) lands, they must be rewritten to assert rejection —
that rewrite is the fix's acceptance criterion.
"""
import sqlite3
import time

import pytest

import mailapp.queue as q

OWNER = "A" * 64


@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    db = str(tmp_path / "queue.db")
    monkeypatch.setattr(q, "DB", db)
    q.ensure_schema()
    return db


def _ev():
    return {"id": "e" * 64, "kind": 1059, "pubkey": "a" * 64,
            "tags": [["p", OWNER]], "content": "x", "sig": "s" * 128}


def _row(db, rid):
    with sqlite3.connect(db) as c:
        return c.execute("SELECT status, worker, attempts FROM mail_queue WHERE id=?", (rid,)).fetchone()


def _stale_then_reclaim(db, rid, tmp_path):
    """A claims → stalls past the window → reclaim → B claims. Returns B's id."""
    q.claim(groups={OWNER}, worker="A")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, rid))
        c.commit()
    assert q.reclaim_stale(timeout=60) == 1
    assert q.claim(groups={OWNER}, worker="B") is not None
    return _row(db, rid)


def test_stale_success_overwrites_active_row(queue_db):
    """DEFECT: A's late finish(ok=True) marks B's active row done (B's work is discarded)."""
    rid = q.enqueue(_ev())
    before = _stale_then_reclaim(queue_db, rid, None)
    assert before[0] == "processing" and before[1] == "B"

    q.finish(rid, True)  # A resumes; only the row id is passed

    after = _row(queue_db, rid)
    assert after[0] == "done", "current behaviour: stale success lands on the live row"
    assert after[1] == "B", "the row still credits worker B — attribution is now wrong"


def test_stale_failure_mutates_active_row(queue_db):
    """DEFECT: A's late finish(ok=False) pushes B's active row back to pending and bumps attempts."""
    rid = q.enqueue(_ev())
    before = _stale_then_reclaim(queue_db, rid, None)
    assert before[0] == "processing" and before[2] == 0

    q.finish(rid, False, error="stale holder resumed")  # A resumes

    after = _row(queue_db, rid)
    assert after[0] == "pending", "current behaviour: B's work is reset to pending"
    assert after[2] == 1, "current behaviour: the retry counter is charged to B for A's failure"


def test_fenced_contract_would_reject_stale_holder(queue_db):
    """The proposed contract (match id + status + worker) rejects the stale holder.

    This is the acceptance criterion for the fix: 0 rows for the old holder,
    1 row for the current holder.
    """
    rid = q.enqueue(_ev())
    _stale_then_reclaim(queue_db, rid, None)

    with sqlite3.connect(queue_db) as c:
        stale = c.execute(
            "UPDATE mail_queue SET status='done', processed_at=? "
            "WHERE id=? AND status='processing' AND worker=?",
            (int(time.time()), rid, "A")).rowcount
        holder = c.execute(
            "UPDATE mail_queue SET status='done', processed_at=? "
            "WHERE id=? AND status='processing' AND worker=?",
            (int(time.time()), rid, "B")).rowcount
        c.commit()

    assert stale == 0, "stale holder must not affect the row"
    assert holder == 1, "current holder must still be able to finish"
    assert _row(queue_db, rid)[0] == "done"
