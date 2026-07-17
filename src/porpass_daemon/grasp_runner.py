"""Run GRaSP against a rendered job.toml, capturing its output.

Invokes ``{grasp} run <job.toml> -v`` as a subprocess and streams the merged
stdout+stderr into ``run.log`` in the job directory as it is produced — so a long
run is observable live and the log survives even if the daemon dies mid-run. A
non-zero exit is reported (not raised) so the caller can mark the job failed; a
GRaSP that cannot be launched at all raises :class:`GraspError`.

``run.log`` is the record the web preserves through a results-delete (Contract C
``kind: "log"``), so it gets a small header/footer framing GRaSP's own output.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .logging_conf import get_logger

log = get_logger(__name__)

# Grace period between SIGTERM and SIGKILL when cancelling a run.
_CANCEL_GRACE_SECONDS = 10.0


class GraspError(Exception):
    """GRaSP could not be launched (e.g. executable missing/not runnable)."""


@dataclass(frozen=True)
class GraspResult:
    """Outcome of a GRaSP run."""

    returncode: int
    run_log: Path
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.cancelled and self.returncode == 0


def _cancel_watch(
    proc: subprocess.Popen, cancel_event: threading.Event, run_log
) -> None:
    """Terminate ``proc`` if ``cancel_event`` is set before it exits.

    Polls both so it returns promptly whether the run is cancelled or finishes
    on its own. On cancel: SIGTERM, a grace period, then SIGKILL.
    """
    while True:
        if cancel_event.wait(timeout=0.5):
            log.warning("cancel requested; terminating GRaSP pid %s", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=_CANCEL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("GRaSP pid %s did not exit on SIGTERM; killing", proc.pid)
                proc.kill()
            return
        if proc.poll() is not None:
            return  # finished on its own


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_grasp(
    grasp_bin: str,
    job_toml: str | Path,
    run_log: str | Path,
    *,
    cwd: str | Path | None = None,
    cancel_event: threading.Event | None = None,
) -> GraspResult:
    """Run ``{grasp_bin} run <job_toml> -v``, streaming output into ``run_log``.

    If ``cancel_event`` is provided and set during the run, GRaSP is terminated
    and the result is marked ``cancelled``. Returns a :class:`GraspResult` with
    the process exit code (0 = success). Raises :class:`GraspError` only if GRaSP
    could not be started at all.
    """
    job_toml = Path(job_toml)
    run_log = Path(run_log)
    cmd = [grasp_bin, "run", str(job_toml), "-v"]
    log.info("running GRaSP: %s", " ".join(cmd))

    with open(run_log, "w", encoding="utf-8") as logf:
        logf.write("=== porpass-daemon GRaSP run ===\n")
        logf.write(f"=== command : {' '.join(cmd)}\n")
        logf.write(f"=== started : {_now()}\n\n")
        logf.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd) if cwd is not None else None,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as exc:
            logf.write(f"\n=== ERROR: GRaSP executable {grasp_bin!r} not found ===\n")
            raise GraspError(
                f"GRaSP executable {grasp_bin!r} not found on PATH"
            ) from exc
        except OSError as exc:
            logf.write(f"\n=== ERROR: could not run GRaSP: {exc} ===\n")
            raise GraspError(
                f"could not run GRaSP {grasp_bin!r}: {exc}"
            ) from exc

        watcher: threading.Thread | None = None
        if cancel_event is not None:
            watcher = threading.Thread(
                target=_cancel_watch, args=(proc, cancel_event, run_log),
                name=f"grasp-cancel-{proc.pid}", daemon=True,
            )
            watcher.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            logf.write(line)
            logf.flush()
        returncode = proc.wait()
        if watcher is not None:
            watcher.join(timeout=1)

        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled:
            logf.write(f"\n=== GRaSP cancelled (exit {returncode}) at {_now()} ===\n")
        else:
            logf.write(f"\n=== GRaSP exited {returncode} at {_now()} ===\n")

    log.info("GRaSP for %s %s (log: %s)", job_toml.name,
             "cancelled" if cancelled else f"exited {returncode}", run_log)
    return GraspResult(returncode=returncode, run_log=run_log, cancelled=cancelled)
