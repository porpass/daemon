"""The daemon's main poll loop and per-job processing.

The loop reaps abandoned jobs, atomically claims the next queued one, and runs it
end to end: resolve + download inputs, render job.toml, run GRaSP, write the
manifest, and record the terminal status. A heartbeat keeps the claim fresh (and
the job safe from the reaper) for the duration.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pymysql

from . import db, grasp_runner, inputs, manifest, reaper, render, schema
from .config import Config
from .inputs import ResolvedInputs
from .logging_conf import get_logger
from .models import Job

log = get_logger(__name__)


def _schema_filename(instrument: str, product: str) -> str:
    """Artifact filename for a combo (product spaces slugged to underscores)."""
    return f"{instrument}_{product.replace(' ', '_')}.schema.json"


def _truncate(message: str, limit: int = 1000) -> str:
    """Keep error_message concise; full detail lives in run.log."""
    return message if len(message) <= limit else message[: limit - 1] + "…"


# Provenance/log files kept when discarding a cancelled job's partial output.
_KEEP_ON_CANCEL = {"config.json", "job.toml", "run.log", "manifest.json"}


def _delete_partial_products(out_dir: str) -> None:
    """Remove a cancelled run's partial products, keeping provenance + run.log."""
    root = Path(out_dir)
    for path in root.rglob("*"):
        if path.is_file() and path.name not in _KEEP_ON_CANCEL \
                and not path.name.startswith("."):
            try:
                path.unlink()
            except OSError as exc:
                log.error("could not delete partial product %s: %s", path, exc)


class Heartbeat:
    """Refreshes ``claimed_at`` for a running job on a background thread.

    Uses its own DB connection (pymysql connections are not thread-safe). If a
    heartbeat update affects 0 rows the job is no longer ours (reaped or
    completed elsewhere), so the thread stops on its own.
    """

    def __init__(self, config: Config, job_id: int) -> None:
        self._config = config
        self._job_id = job_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Heartbeat":
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._job_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        cfg = self._config
        try:
            conn = db.connect(cfg)
        except pymysql.Error as exc:
            log.error("heartbeat could not connect for job %s: %s; job unprotected",
                      self._job_id, exc)
            return
        try:
            # wait() returns True when stop is set (exit), False on interval timeout.
            while not self._stop.wait(cfg.heartbeat_interval):
                try:
                    if db.touch_heartbeat(conn, self._job_id, cfg.worker_id) == 0:
                        log.warning(
                            "job %s no longer owned by %s; stopping heartbeat",
                            self._job_id, cfg.worker_id,
                        )
                        return
                except pymysql.Error as exc:
                    log.error("heartbeat update failed for job %s: %s",
                              self._job_id, exc)
        finally:
            conn.close()


class CancelWatcher:
    """Polls for a cancel request and trips ``cancel_event`` when one arrives.

    Uses its own DB connection and a short interval (independent of the
    heartbeat). Degrades gracefully: if the query fails — e.g. the
    ``cancel_requested`` column doesn't exist yet — it logs once and stops
    watching, so cancellation is simply unavailable rather than fatal.
    """

    def __init__(
        self, config: Config, job_id: int, cancel_event: threading.Event
    ) -> None:
        self._config = config
        self._job_id = job_id
        self._cancel_event = cancel_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "CancelWatcher":
        self._thread = threading.Thread(
            target=self._run, name=f"cancel-watch-{self._job_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        cfg = self._config
        try:
            conn = db.connect(cfg)
        except pymysql.Error as exc:
            log.error("cancel-watch could not connect for job %s: %s",
                      self._job_id, exc)
            return
        try:
            while not self._stop.wait(cfg.cancel_poll_interval):
                try:
                    if db.is_cancel_requested(conn, self._job_id, cfg.worker_id):
                        log.info("cancel requested for job %s", self._job_id)
                        self._cancel_event.set()
                        return
                except pymysql.Error as exc:
                    log.warning(
                        "cancel check failed for job %s (cancel disabled this "
                        "run): %s", self._job_id, exc,
                    )
                    return
        finally:
            conn.close()


class WorkerLoop:
    """Long-lived poll loop for one worker process."""

    def __init__(self, config: Config, stop_event: threading.Event) -> None:
        self._config = config
        self._stop = stop_event

    def run(self) -> None:
        """Poll for work until the stop event is set."""
        cfg = self._config
        log.info(
            "worker %s starting (db=%s@%s, storage=%s, poll=%.1fs)",
            cfg.worker_id,
            cfg.db_database,
            cfg.db_host,
            cfg.storage_path,
            cfg.poll_interval,
        )
        self._check_db()
        if cfg.publish_schemas_on_start:
            self._publish_schemas()

        conn = db.connect(cfg)
        try:
            self._poll_loop(conn)
        finally:
            conn.close()

        log.info("worker %s stopped", cfg.worker_id)

    def _poll_loop(self, conn: pymysql.connections.Connection) -> None:
        """Claim and handle jobs until asked to stop."""
        cfg = self._config
        while not self._stop.is_set():
            try:
                conn.ping(reconnect=True)
                reaper.requeue_stale_jobs(conn, cfg.reaper_stale_seconds)
                job = db.claim_next_job(conn, cfg.worker_id)
            except pymysql.Error as exc:
                log.error("claim poll failed: %s", exc)
                self._stop.wait(cfg.poll_interval)
                continue

            if job is None:
                self._stop.wait(cfg.poll_interval)
                continue

            self._handle_job(conn, job)

    def _handle_job(self, conn: pymysql.connections.Connection, job: Job) -> None:
        """Run a claimed job end to end and record its terminal status.

        The heartbeat keeps the claim fresh for the whole run. Any failure —
        missing schema, unresolved/undownloadable inputs, a GRaSP that won't
        launch or exits non-zero, or anything unexpected — marks the job failed
        with a concise error_message (full detail is in run.log). The input temp
        dir is always cleaned up.
        """
        cb = job.config
        out_dir = job.output_dir
        grasp_version = cb.get("grasp_version", "")
        log.info(
            "handling job %s: %s %s obs=%s out=%s",
            job.job_id, cb.get("instrument"), cb.get("product"),
            job.observation_id, out_dir,
        )

        if not out_dir:
            self._fail(conn, job, "job has no output_dir set by the web", grasp_version)
            return

        resolved: ResolvedInputs | None = None
        cancel_event = threading.Event()
        try:
            with Heartbeat(self._config, job.job_id), \
                    CancelWatcher(self._config, job.job_id, cancel_event):
                artifact = self._load_artifact(job)
                grasp_version = artifact.grasp_version or grasp_version

                resolved = inputs.resolve_inputs(conn, job, artifact)

                # A cancel that arrived during the (uninterruptible) download is
                # honoured here, before we spend time on GRaSP.
                if cancel_event.is_set():
                    self._finalize_cancel(conn, job, out_dir, grasp_version)
                    return

                toml_text = render.render_job_toml(
                    artifact, cb.get("overrides", {}), out_dir, resolved.files
                )
                render.write_job_toml(Path(out_dir) / "job.toml", toml_text)

                result = grasp_runner.run_grasp(
                    self._config.grasp_bin,
                    Path(out_dir) / "job.toml",
                    Path(out_dir) / "run.log",
                    cwd=out_dir,
                    cancel_event=cancel_event,
                )

                if result.cancelled:
                    self._finalize_cancel(conn, job, out_dir, grasp_version)
                elif result.ok:
                    manifest.write_manifest(out_dir, grasp_version)
                    self._succeed(conn, job)
                else:
                    manifest.write_manifest(out_dir, grasp_version)
                    self._fail(
                        conn, job,
                        f"GRaSP exited {result.returncode}; see run.log",
                        grasp_version, artifacts_written=True,
                    )
        except (schema.SchemaError, inputs.InputResolutionError,
                grasp_runner.GraspError) as exc:
            self._fail(conn, job, str(exc), grasp_version)
        except Exception as exc:  # noqa: BLE001 — never let one job kill the loop
            log.exception("job %s crashed unexpectedly", job.job_id)
            self._fail(conn, job, f"unexpected error: {exc}", grasp_version)
        finally:
            if resolved is not None:
                resolved.cleanup()

    def _load_artifact(self, job: Job):
        """Load the schema artifact for the job's (instrument, product)."""
        cb = job.config
        name = _schema_filename(cb["instrument"], cb["product"])
        return schema.load_artifact(self._config.schemas_dir / name)

    def _succeed(self, conn: pymysql.connections.Connection, job: Job) -> None:
        conn.ping(reconnect=True)  # the GRaSP run may have been long
        if db.mark_succeeded(conn, job.job_id, self._config.worker_id):
            log.info("job %s succeeded", job.job_id)
        else:
            log.warning("job %s not marked succeeded (no longer owned)", job.job_id)

    def _finalize_cancel(
        self, conn: pymysql.connections.Connection, job: Job,
        out_dir: str, grasp_version: str,
    ) -> None:
        """Discard partial output, write the manifest, mark the job cancelled."""
        log.warning("job %s cancelled; discarding partial output", job.job_id)
        _delete_partial_products(out_dir)
        run_log = Path(out_dir) / "run.log"
        if not run_log.exists():
            run_log.write_text(
                f"=== job {job.job_id} cancelled before GRaSP produced a log ===\n"
            )
        try:
            manifest.write_manifest(out_dir, grasp_version)
        except OSError as exc:
            log.error("could not write manifest for cancelled job %s: %s",
                      job.job_id, exc)
        conn.ping(reconnect=True)
        if db.mark_cancelled(conn, job.job_id, self._config.worker_id):
            log.warning("job %s cancelled", job.job_id)
        else:
            log.warning("job %s not marked cancelled (no longer owned)", job.job_id)

    def _fail(
        self,
        conn: pymysql.connections.Connection,
        job: Job,
        message: str,
        grasp_version: str,
        *,
        artifacts_written: bool = False,
    ) -> None:
        """Record a failure: ensure run.log + manifest exist, then mark failed."""
        out_dir = job.output_dir
        if out_dir and not artifacts_written:
            try:
                run_log = Path(out_dir) / "run.log"
                if not run_log.exists():
                    run_log.write_text(
                        f"=== job {job.job_id} failed before GRaSP produced a log ===\n"
                        f"{message}\n"
                    )
                manifest.write_manifest(out_dir, grasp_version)
            except OSError as exc:
                log.error("could not write failure artifacts for job %s: %s",
                          job.job_id, exc)
        conn.ping(reconnect=True)
        if db.mark_failed(conn, job.job_id, self._config.worker_id, _truncate(message)):
            log.warning("job %s failed: %s", job.job_id, message)
        else:
            log.warning("job %s not marked failed (no longer owned)", job.job_id)

    def _check_db(self) -> None:
        """Fail fast if the database is unreachable at startup."""
        conn = db.connect(self._config)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            log.info(
                "database connection ok (%s@%s:%d/%s)",
                self._config.db_username,
                self._config.db_host,
                self._config.db_port,
                self._config.db_database,
            )
        finally:
            conn.close()

    def _publish_schemas(self) -> None:
        """Publish schema artifacts on startup, best-effort.

        Keeping the web's forms in step with the running GRaSP is the goal, but
        a publish failure (e.g. GRaSP not installed in a dev env) must not stop
        the daemon from claiming and running jobs — so failures are logged and
        swallowed here. Use ``porpass-daemon --publish-schemas`` for a run whose
        exit code reflects publish success.
        """
        try:
            schema.publish_schemas(self._config)
        except schema.SchemaError as exc:
            log.warning("schema publish skipped: %s", exc)
