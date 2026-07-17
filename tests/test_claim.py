"""Tests for the atomic job claim.

The claim's real race-safety lives in the SQL `WHERE status='queued'` guard,
which is exercised live against MariaDB. These unit tests use a fake connection
to verify the daemon's handling of the affected-row count: it skips candidates
another worker grabbed (UPDATE affected 0) and only proceeds on a win (1).
"""

from __future__ import annotations

import json

from porpass_daemon import db
from porpass_daemon.models import Job


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._result: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=()) -> int:
        self._conn.executed.append((sql, params))
        if sql.startswith("SELECT job_id FROM processing_jobs"):
            self._result = [{"job_id": j} for j in self._conn.queued]
            return len(self._result)
        if sql.startswith("UPDATE processing_jobs"):
            _worker_id, job_id = params
            if job_id in self._conn.taken:      # another worker already has it
                return 0
            self._conn.taken.add(job_id)
            return 1
        if sql.startswith("SELECT job_id, user_id"):
            (job_id,) = params
            self._result = [self._conn.rows[job_id]]
            return 1
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, queued, taken=(), rows=None):
        self.queued = list(queued)
        self.taken = set(taken)
        self.rows = rows or {}
        self.executed: list[tuple] = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


def _row(job_id: int) -> dict:
    return {
        "job_id": job_id,
        "user_id": 1,
        "observation_id": 100 + job_id,
        "config": json.dumps({"instrument": "SHARAD", "product": "EDR"}),
        "output_dir": f"/storage/processing/1/{job_id}",
    }


def test_claim_success() -> None:
    conn = _FakeConn(queued=[6], rows={6: _row(6)})
    job = db.claim_next_job(conn, "worker-a")
    assert isinstance(job, Job)
    assert job.job_id == 6
    assert job.observation_id == 106
    assert job.config["instrument"] == "SHARAD"       # parsed from DB config
    assert job.output_dir == "/storage/processing/1/6"


def test_claim_skips_taken_candidate() -> None:
    # First candidate already taken by another worker; claim the next one.
    conn = _FakeConn(queued=[6, 7], taken=[6], rows={7: _row(7)})
    job = db.claim_next_job(conn, "worker-b")
    assert job is not None and job.job_id == 7


def test_claim_none_when_all_taken() -> None:
    conn = _FakeConn(queued=[6, 7], taken=[6, 7])
    assert db.claim_next_job(conn, "worker-c") is None


def test_claim_none_when_empty() -> None:
    conn = _FakeConn(queued=[])
    assert db.claim_next_job(conn, "worker-d") is None


def test_claim_sql_guards_status_and_avoids_web_owned_columns() -> None:
    conn = _FakeConn(queued=[6], rows={6: _row(6)})
    db.claim_next_job(conn, "worker-e")
    update = next(sql for sql, _ in conn.executed if sql.startswith("UPDATE"))
    # race-safe guard + only the columns the daemon owns
    assert "status = 'queued'" in update
    assert "claimed_by" in update and "claimed_at" in update and "started_at" in update
    for web_owned in ("output_dir", "results_deleted", "results_deleted_at", "rerun_of"):
        assert web_owned not in update
