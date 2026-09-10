#!/usr/bin/env python3
"""Queue fencing probe — reproduces the stale-completion scenario on the real code.

Scenario (from an external review of this repo):
  1. worker A claims job J;
  2. J ages past RECLAIM_TIMEOUT, reclaim_stale() puts it back to pending;
  3. worker B claims J and starts working;
  4. A resumes and calls finish(J, ok) with only the row id.

Question this probe answers with measurements, not opinion:
  - does A's late finish overwrite B's active row?
  - does a fenced variant (id + status='processing' + worker match) reject it?
  - does the fence still let the current holder finish?

Runs on a throwaway SQLite file. Never touches the live queue.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# the protocol core lives in a sibling repo (nostr-mail-bridge/src), same as tests/conftest.py does
_MB = os.environ.get("NOSTR_MAIL_BRIDGE_SRC", "")
if _MB:
    for _p in (_MB, os.path.join(os.path.dirname(_MB), "deps")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
else:
    _MBD = os.path.expanduser("~/data/projects/nostr-mail-bridge")
    for _p in (os.path.join(_MBD, "src"), os.path.join(_MBD, "deps")):
        if os.path.exists(_p) and _p not in sys.path:
            sys.path.insert(0, _p)

import mailapp.queue as q  # noqa: E402

OWNER = "A" * 64
result = {"schema": "snin-mail-queue-fence-probe/1.0", "code_revision": None,
          "repo": "https://github.com/konantgit-sys/snin-mail-nostr"}


def _try_commit():
    try:
        import subprocess
        return subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


result["code_revision"] = _try_commit()
result["reclaim_timeout_s"] = q.RECLAIM_TIMEOUT
result["max_attempts"] = q.MAX_ATTEMPTS


def _event():
    return {"id": "e" * 64, "kind": 1059, "pubkey": "a" * 64,
            "tags": [["p", OWNER]], "content": "x", "sig": "s" * 128}


def _row(db, rid):
    with sqlite3.connect(db) as c:
        r = c.execute("SELECT status, worker, attempts FROM mail_queue WHERE id=?",
                      (rid,)).fetchone()
    return {"status": r[0], "worker": r[1], "attempts": r[2]}


def _fenced_update(db, rid, worker, ok):
    """Proposed contract: success/failure must match id + status + worker atomically."""
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "UPDATE mail_queue SET status=?, processed_at=?, error='' "
            "WHERE id=? AND status='processing' AND worker=?",
            ("done" if ok else "pending", int(time.time()), rid, worker))
        c.commit()
        return cur.rowcount


def scenario(ok: bool):
    with tempfile.TemporaryDirectory() as tmp:
        q.DB = os.path.join(tmp, "queue.db")
        q.ensure_schema()
        rid = q.enqueue(_event())

        a = q.claim(groups={OWNER}, worker="A")
        with sqlite3.connect(q.DB) as c:  # simulate A stalling past the reclaim window
            c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, rid))
            c.commit()
        reclaimed = q.reclaim_stale(timeout=60)
        b = q.claim(groups={OWNER}, worker="B")
        before = _row(q.DB, rid)

        q.finish(rid, ok)          # A resumes: only the row id is passed
        after_legacy = _row(q.DB, rid)

        # same state replayed against the proposed fenced contract
        q.DB = os.path.join(tmp, "queue2.db")
        q.ensure_schema()
        rid2 = q.enqueue(_event())
        q.claim(groups={OWNER}, worker="A")
        with sqlite3.connect(q.DB) as c:
            c.execute("UPDATE mail_queue SET started_at=? WHERE id=?", (int(time.time()) - 9999, rid2))
            c.commit()
        q.reclaim_stale(timeout=60)
        q.claim(groups={OWNER}, worker="B")
        rejected = _fenced_update(q.DB, rid2, "A", ok)   # stale holder A
        holder = _fenced_update(q.DB, rid2, "B", ok)     # current holder B
        after_fenced = _row(q.DB, rid2)

        return {
            "late_finish_ok": ok,
            "claim_A_then_B": {"a_claimed": a is not None, "reclaimed": reclaimed,
                               "b_claimed": b is not None, "row_before_late_finish": before},
            "legacy_finish": {"rows_affected_expected": 1, "row_after": after_legacy,
                              "stale_write_landed": after_legacy != before},
            "fenced_contract": {"stale_holder_rows_affected": rejected,
                                "current_holder_rows_affected": holder,
                                "row_after": after_fenced},
        }


c1 = scenario(True)
c2 = scenario(False)
result["case_late_success"] = c1
result["case_late_failure"] = c2
result["verdict"] = {
    "stale_success_overwrites_active_row": c1["legacy_finish"]["stale_write_landed"],
    "stale_failure_mutates_active_row": c2["legacy_finish"]["stale_write_landed"],
    "fenced_rejects_stale_holder": (c1["fenced_contract"]["stale_holder_rows_affected"] == 0
                                    and c2["fenced_contract"]["stale_holder_rows_affected"] == 0),
    "fenced_allows_current_holder": (c1["fenced_contract"]["current_holder_rows_affected"] == 1
                                     and c2["fenced_contract"]["current_holder_rows_affected"] == 1),
}
result["scope_note"] = ("Queue-state only: this shows a lost/duplicated decryption task row, "
                        "not an observed duplicate external email. No live DB, worker or relay was touched.")

print(json.dumps(result, ensure_ascii=False, indent=1))
