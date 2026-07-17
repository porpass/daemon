"""Tests for the reaper requeue and the heartbeat update.

Both are single guarded UPDATEs whose real timing behaviour is verified live
against MariaDB. These unit tests use a fake connection to lock in the SQL
guards (status / staleness / ownership) and the affected-row handling, and to
assert neither statement ever touches a web-owned column.
"""

from __future__ import annotations

from porpass_daemon import db, reaper

WEB_OWNED = ("output_dir", "results_deleted", "results_deleted_at", "rerun_of")


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
        if sql.startswith("SELECT job_id, claimed_by, claimed_at"):
            self._result = list(self._conn.stale)
            return len(self._result)
        if sql.startswith("UPDATE processing_jobs SET status = 'queued'"):
            return len(self._conn.stale)          # reaper requeue
        if sql.startswith("UPDATE processing_jobs SET claimed_at"):
            return self._conn.heartbeat_affected  # heartbeat
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, stale=(), heartbeat_affected=1):
        self.stale = list(stale)
        self.heartbeat_affected = heartbeat_affected
        self.executed: list[tuple] = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


def _stale_row(job_id: int) -> dict:
    return {"job_id": job_id, "claimed_by": "dead-worker", "claimed_at": "2026-07-10 11:54:29"}


# --------------------------------------------------------------------------- #
# Reaper
# --------------------------------------------------------------------------- #

def test_requeue_stale_jobs_counts_and_commits() -> None:
    conn = _FakeConn(stale=[_stale_row(6), _stale_row(9)])
    assert reaper.requeue_stale_jobs(conn, 300) == 2
    assert conn.commits >= 1


def test_requeue_none_skips_update() -> None:
    conn = _FakeConn(stale=[])
    assert reaper.requeue_stale_jobs(conn, 300) == 0
    # only the SELECT ran; no UPDATE when nothing is stale
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.executed)


def test_requeue_sql_guards_and_web_owned() -> None:
    conn = _FakeConn(stale=[_stale_row(6)])
    reaper.requeue_stale_jobs(conn, 300)
    update = next(sql for sql, _ in conn.executed if sql.startswith("UPDATE"))
    assert "status = 'running'" in update            # only touches running rows
    assert "claimed_at < (NOW() - INTERVAL" in update  # staleness guard
    assert "status = 'queued'" in update             # ...and requeues them
    for col in ("claimed_by = NULL", "claimed_at = NULL", "started_at = NULL"):
        assert col in update
    for col in WEB_OWNED:
        assert col not in update


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #

def test_touch_heartbeat_ownership_guard() -> None:
    conn = _FakeConn(heartbeat_affected=1)
    assert db.touch_heartbeat(conn, 6, "worker-1") == 1
    sql, params = conn.executed[-1]
    assert "SET claimed_at = NOW()" in sql
    assert "claimed_by = %s" in sql and "status = 'running'" in sql
    assert params == (6, "worker-1")
    for col in WEB_OWNED:
        assert col not in sql


def test_touch_heartbeat_zero_when_not_owned() -> None:
    conn = _FakeConn(heartbeat_affected=0)  # reaped/completed elsewhere
    assert db.touch_heartbeat(conn, 6, "worker-1") == 0
