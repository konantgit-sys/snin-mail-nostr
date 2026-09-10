"""Stale-completion gap in the mail queue — acceptance tests for the fix.

Finding (external review, board seq 29406, pinned revision 2ffd690): `finish()`
updated a queue row by `id` only, so a worker whose task had been reclaimed
after a stall could still write to the row that a *different* worker now holds —
marking the new holder's active row done, or pushing its row back to pending and
charging it a retry it never earned.

Contract now implemented (this file is the fix's acceptance criterion):
  * claim() issues a fresh lease token and stores it on the row;
  * finish() matches id + status='processing' + lease, so a stale holder's
    completion changes 0 rows and cannot touch the current holder;
  * the current holder still completes normally (1 row);
  * attempts are incremented atomically inside the UPDATE, not read first;
  * reclaim_stale() clears the lease when it returns a row to pending.
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
        return c.execute(
            "SELECT status, worker, attempts, lease FROM mail_queue WHERE id=?", (rid,)
        ).fetchone()


def _stale_then_reclaim(db, rid):
    """A claims → stalls past the window → reclaim → B claims.

    Returns (stale_lease, live_lease) — A's and B's claim tokens.
    """
    stale = q.claim(groups={OWNER}, worker="A")
    assert stale is not None and stale["id"] == rid
    with sqlite3.connect(db) as c:
        c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, rid))
        c.commit()
    assert q.reclaim_stale(timeout=60) == 1, "reclaim must return the stalled row to pending"
    live = q.claim(groups={OWNER}, worker="B")
    assert live is not None and live["id"] == rid
    return stale["lease"], live["lease"]


def test_reclaim_clears_lease_and_new_claim_issues_fresh_one(queue_db):
    rid = q.enqueue(_ev())
    stale_lease, live_lease = _stale_then_reclaim(queue_db, rid)
    assert stale_lease and live_lease
    assert stale_lease != live_lease, "each claim must issue a fresh lease token"
    assert _row(queue_db, rid)[3] == live_lease


def test_stale_success_is_rejected(queue_db):
    """A's late finish(ok=True) must not mark B's active row done."""
    rid = q.enqueue(_ev())
    stale_lease, live_lease = _stale_then_reclaim(queue_db, rid)

    accepted = q.finish(rid, True, lease=stale_lease)

    assert accepted is False, "stale holder must be rejected"
    row = _row(queue_db, rid)
    assert row[0] == "processing", "B's active row must stay processing"
    assert row[1] == "B"
    assert row[3] == live_lease, "B's lease must survive the stale write"

    assert q.finish(rid, True, lease=live_lease) is True, "current holder must complete"
    assert _row(queue_db, rid)[0] == "done"


def test_stale_failure_is_rejected(queue_db):
    """A's late finish(ok=False) must not reset B's row or charge B with a retry."""
    rid = q.enqueue(_ev())
    stale_lease, live_lease = _stale_then_reclaim(queue_db, rid)

    accepted = q.finish(rid, False, error="stale holder resumed", lease=stale_lease)

    assert accepted is False
    row = _row(queue_db, rid)
    assert row[0] == "processing", "B's work must not be reset to pending"
    assert row[2] == 0, "B must not be charged attempts for A's failure"

    assert q.finish(rid, False, error="real failure", lease=live_lease) is True
    row = _row(queue_db, rid)
    assert row[0] == "pending" and row[2] == 1, "the live failure counts once"


def test_finish_without_lease_is_rejected(queue_db):
    """A legacy caller passing only the row id can no longer write to a claimed row."""
    rid = q.enqueue(_ev())
    stale_lease, live_lease = _stale_then_reclaim(queue_db, rid)

    assert q.finish(rid, True) is False
    assert q.finish(rid, False, error="legacy caller") is False
    row = _row(queue_db, rid)
    assert row[0] == "processing" and row[2] == 0

    assert q.finish(rid, True, lease=live_lease) is True


def test_attempts_are_incremented_atomically_until_failed(queue_db):
    """MAX_ATTEMPTS reached through finish(ok=False) inside the UPDATE."""
    rid = q.enqueue(_ev())
    for i in range(q.MAX_ATTEMPTS):
        row = q.claim(groups={OWNER}, worker="w")
        assert row is not None and row["id"] == rid
        assert q.finish(rid, False, error=f"boom {i}", lease=row["lease"]) is True
    assert _row(queue_db, rid)[0] == "failed"
    assert _row(queue_db, rid)[2] == q.MAX_ATTEMPTS
    assert q.claim(groups={OWNER}, worker="w") is None, "failed row must not be claimable"


def test_migration_adds_lease_to_legacy_db(tmp_path, monkeypatch):
    """A queue created before this fix gets the lease column on next ensure_schema()."""
    db = str(tmp_path / "legacy.db")
    monkeypatch.setattr(q, "DB", db)
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE mail_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT DEFAULT '',"
            " payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
            " attempts INTEGER NOT NULL DEFAULT 0, worker TEXT DEFAULT '',"
            " created_at INTEGER NOT NULL, started_at INTEGER DEFAULT 0,"
            " processed_at INTEGER DEFAULT 0, error TEXT DEFAULT '')"
        )
        c.execute("INSERT INTO mail_queue (payload, created_at) VALUES ('{}', ?)", (int(time.time()),))
        c.commit()

    q.ensure_schema()

    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(mail_queue)").fetchall()}
    assert "lease" in cols
    row = q.claim(groups=None, worker="w1")
    assert row is not None and row["lease"], "legacy row must be claimable with a lease"
    assert row["lease"] != ""
