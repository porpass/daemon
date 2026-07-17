"""Database access for the daemon.

Uses plain ``pymysql`` with a fresh connection per unit of work and explicit
transaction control, mirroring the pattern in the web's
``gis/routers/vectors.py``. No ORM, no pool — job throughput is low and
correctness of the atomic-claim ``UPDATE`` matters more than connection reuse.

This module only opens connections and centralises credential handling. The
queries that mutate ``processing_jobs`` (claim, heartbeat, complete, requeue)
live with the components that own those state transitions.
"""

from __future__ import annotations

import json

import pymysql
import pymysql.cursors

from .config import Config
from .logging_conf import get_logger
from .models import Job

log = get_logger(__name__)


def connect(config: Config) -> pymysql.connections.Connection:
    """Open a MariaDB connection using daemon configuration.

    Returns a connection with a ``DictCursor`` and ``autocommit`` disabled so
    callers control transaction boundaries explicitly. READ COMMITTED isolation
    matches the web service and keeps the atomic claim's row read fresh.
    """
    conn = pymysql.connect(
        host=config.db_host,
        port=config.db_port,
        database=config.db_database,
        user=config.db_username,
        password=config.db_password,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    return conn


# --------------------------------------------------------------------------- #
# Atomic claim
# --------------------------------------------------------------------------- #

# The race-safe claim: the `status = 'queued'` guard means only one worker's
# UPDATE can match a given row, so concurrent workers never double-claim. We
# proceed only when it affected exactly one row. `started_at` is stamped here
# alongside `claimed_at`; web-owned columns (output_dir, results_deleted,
# results_deleted_at, rerun_of) are never touched.
_CLAIM_SQL = (
    "UPDATE processing_jobs "
    "SET status = 'running', claimed_by = %s, claimed_at = NOW(), started_at = NOW() "
    "WHERE job_id = %s AND status = 'queued'"
)


def claim_next_job(
    conn: pymysql.connections.Connection,
    worker_id: str,
    *,
    batch_size: int = 10,
) -> Job | None:
    """Atomically claim the oldest queued job for this worker.

    Reads a small batch of queued job ids (oldest first) and attempts the
    guarded claim UPDATE on each until one succeeds — tolerating other workers
    grabbing candidates in between. Returns the claimed :class:`Job`, or ``None``
    when nothing was claimable this cycle.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id FROM processing_jobs "
            "WHERE status = 'queued' "
            "ORDER BY submitted_at, job_id "
            "LIMIT %s",
            (batch_size,),
        )
        candidate_ids = [row["job_id"] for row in cur.fetchall()]
    conn.commit()

    for job_id in candidate_ids:
        with conn.cursor() as cur:
            affected = cur.execute(_CLAIM_SQL, (worker_id, job_id))
        conn.commit()
        if affected == 1:
            log.info("claimed job %s as %s", job_id, worker_id)
            return _load_job(conn, job_id)
        # affected == 0: another worker claimed it first; try the next candidate.

    return None


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #

# Refresh claimed_at while we still own a running job. The claimed_by + status
# guards mean a job the reaper already requeued (claimed_by cleared, status back
# to 'queued') is NOT resurrected — the UPDATE simply affects 0 rows and the
# caller stops heartbeating.
_HEARTBEAT_SQL = (
    "UPDATE processing_jobs SET claimed_at = NOW() "
    "WHERE job_id = %s AND claimed_by = %s AND status = 'running'"
)


def touch_heartbeat(
    conn: pymysql.connections.Connection, job_id: int, worker_id: str
) -> int:
    """Bump ``claimed_at`` for a job this worker is running. Returns rows affected.

    A return of 0 means we no longer own the job (reaped or completed
    elsewhere); the caller should stop heartbeating.
    """
    with conn.cursor() as cur:
        affected = cur.execute(_HEARTBEAT_SQL, (job_id, worker_id))
    conn.commit()
    return affected


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #

# Terminal transitions. The claimed_by + status='running' guard means we only
# finalize a job we still own — if the reaper requeued it (and another worker
# re-claimed it), our completion affects 0 rows instead of clobbering theirs.
# claimed_at is cleared; web-owned columns are never touched.
_SUCCEED_SQL = (
    "UPDATE processing_jobs "
    "SET status = 'succeeded', completed_at = NOW(), claimed_at = NULL "
    "WHERE job_id = %s AND claimed_by = %s AND status = 'running'"
)
_FAIL_SQL = (
    "UPDATE processing_jobs "
    "SET status = 'failed', completed_at = NOW(), claimed_at = NULL, "
    "error_message = %s "
    "WHERE job_id = %s AND claimed_by = %s AND status = 'running'"
)


def mark_succeeded(
    conn: pymysql.connections.Connection, job_id: int, worker_id: str
) -> int:
    """Mark a job succeeded. Returns rows affected (0 if no longer owned)."""
    with conn.cursor() as cur:
        affected = cur.execute(_SUCCEED_SQL, (job_id, worker_id))
    conn.commit()
    return affected


def mark_failed(
    conn: pymysql.connections.Connection,
    job_id: int,
    worker_id: str,
    error_message: str,
) -> int:
    """Mark a job failed with a concise error_message. Returns rows affected."""
    with conn.cursor() as cur:
        affected = cur.execute(_FAIL_SQL, (error_message, job_id, worker_id))
    conn.commit()
    return affected


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

# The web sets cancel_requested; the daemon only reads it (never writes it) and
# owns the status -> 'cancelled' transition. The claimed_by + status guard means
# we only observe/act on a cancel for a job we currently own and are running.
_CANCEL_CHECK_SQL = (
    "SELECT cancel_requested FROM processing_jobs "
    "WHERE job_id = %s AND claimed_by = %s AND status = 'running'"
)
_CANCELLED_SQL = (
    "UPDATE processing_jobs "
    "SET status = 'cancelled', completed_at = NOW(), claimed_at = NULL, "
    "error_message = %s "
    "WHERE job_id = %s AND claimed_by = %s AND status = 'running'"
)


def is_cancel_requested(
    conn: pymysql.connections.Connection, job_id: int, worker_id: str
) -> bool:
    """True if the web has flagged this running, owned job for cancellation.

    Raises the underlying pymysql error (e.g. unknown column before the schema
    change lands); the caller decides how to degrade.
    """
    with conn.cursor() as cur:
        cur.execute(_CANCEL_CHECK_SQL, (job_id, worker_id))
        row = cur.fetchone()
    conn.commit()
    return bool(row and row["cancel_requested"])


def mark_cancelled(
    conn: pymysql.connections.Connection,
    job_id: int,
    worker_id: str,
    error_message: str = "cancelled by user request",
) -> int:
    """Mark a job cancelled. Returns rows affected (0 if no longer owned)."""
    with conn.cursor() as cur:
        affected = cur.execute(_CANCELLED_SQL, (error_message, job_id, worker_id))
    conn.commit()
    return affected


def _load_job(conn: pymysql.connections.Connection, job_id: int) -> Job:
    """Load a claimed job row into a :class:`Job`.

    ``config`` is parsed from the authoritative DB column (not the on-disk
    ``config.json``).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, user_id, observation_id, config, output_dir "
            "FROM processing_jobs WHERE job_id = %s",
            (job_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:  # pragma: no cover - claimed row vanished; treat as fatal
        raise RuntimeError(f"claimed job {job_id} disappeared before load")
    return Job(
        job_id=row["job_id"],
        user_id=row["user_id"],
        observation_id=row["observation_id"],
        config=json.loads(row["config"]),
        output_dir=row["output_dir"],
    )
