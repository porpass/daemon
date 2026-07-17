"""Reaper — requeue jobs abandoned by crashed workers.

A worker stamps ``claimed_at`` at claim time and refreshes it on a heartbeat
while the job runs. If a worker dies mid-job, its ``claimed_at`` stops advancing;
once it is older than a configurable threshold the job is considered abandoned
and is put back to ``queued`` so another worker can pick it up.

The requeue is the mirror of the claim: it only ever resets the
daemon-owned lifecycle columns (``status``, ``claimed_by``, ``claimed_at``,
``started_at``) and never touches the web-owned ones (``output_dir``,
``results_deleted``, ``results_deleted_at``, ``rerun_of``). An active job is
protected from the reaper by its own heartbeat keeping ``claimed_at`` fresh.
"""

from __future__ import annotations

import pymysql

from .logging_conf import get_logger

log = get_logger(__name__)

# Rows still 'running' whose heartbeat went stale past the threshold.
_STALE_SELECT = (
    "SELECT job_id, claimed_by, claimed_at FROM processing_jobs "
    "WHERE status = 'running' AND claimed_at < (NOW() - INTERVAL %s SECOND)"
)

# The requeue. The same status + staleness guard makes it safe under multiple
# reapers: whoever runs it first flips the row to 'queued', and the others'
# UPDATE no longer matches.
_REQUEUE_SQL = (
    "UPDATE processing_jobs "
    "SET status = 'queued', claimed_by = NULL, claimed_at = NULL, started_at = NULL "
    "WHERE status = 'running' AND claimed_at < (NOW() - INTERVAL %s SECOND)"
)


def requeue_stale_jobs(
    conn: pymysql.connections.Connection, stale_seconds: int
) -> int:
    """Requeue every job left ``running`` past ``stale_seconds``. Returns the count.

    Logs each requeued job for audit. Safe to call frequently; when nothing is
    stale it does a single cheap indexed read and returns 0.
    """
    with conn.cursor() as cur:
        cur.execute(_STALE_SELECT, (stale_seconds,))
        stale = cur.fetchall()
        affected = cur.execute(_REQUEUE_SQL, (stale_seconds,)) if stale else 0
    conn.commit()

    for row in stale:
        log.warning(
            "requeued abandoned job %s (was claimed_by=%s, claimed_at=%s, "
            "stale > %ss)",
            row["job_id"], row["claimed_by"], row["claimed_at"], stale_seconds,
        )
    return affected
