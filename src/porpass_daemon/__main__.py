"""Entry point for the PORPASS processing daemon.

    porpass-daemon                              # run the worker poll loop (default)
    porpass-daemon --publish-schemas            # publish schema artifacts to storage
    porpass-daemon --regenerate-schema --out D  # generate schema artifacts into D
    porpass-daemon --reap-once                  # requeue stale 'running' jobs and exit
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from . import db, reaper, schema
from .config import load_config, load_env
from .logging_conf import configure_logging, get_logger
from .worker import WorkerLoop

log = get_logger("porpass_daemon")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="porpass-daemon", description=__doc__)
    parser.add_argument(
        "--reap-once",
        action="store_true",
        help="Requeue stale 'running' jobs once, then exit.",
    )
    parser.add_argument(
        "--publish-schemas",
        action="store_true",
        help="Publish schema artifacts to the configured storage, then exit.",
    )
    parser.add_argument(
        "--regenerate-schema",
        action="store_true",
        help="Generate schema artifacts into --out, then exit. Needs neither "
             "database nor storage configuration.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Target directory for --regenerate-schema (created if missing).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Process entry point. Returns a process exit code."""
    args = _parse_args(argv)

    load_env()
    configure_logging()

    # Generating artifacts into an explicit directory needs neither the database
    # nor the storage mount, so it runs before config is resolved.
    if args.regenerate_schema:
        if args.out is None:
            log.error("--regenerate-schema requires --out <dir>")
            return 2
        try:
            from .schema.generate import export_schema
        except ImportError as exc:
            log.error("GRaSP is not importable, so schema artifacts cannot be "
                      "generated: %s", exc)
            return 1
        try:
            written = export_schema(args.out)
        except Exception as exc:  # noqa: BLE001 — report generator failure cleanly
            log.error("schema generation failed: %s", exc)
            return 1
        for path in written:
            print(path)
        log.info("generated %d schema artifact(s) into %s", len(written), args.out)
        return 0

    try:
        config = load_config()
    except RuntimeError as exc:
        log.error("configuration error: %s", exc)
        return 2

    if args.publish_schemas:
        try:
            written = schema.publish_schemas(config)
        except schema.SchemaError as exc:
            log.error("schema publish failed: %s", exc)
            return 1
        for path in written:
            print(path)
        return 0

    if args.reap_once:
        conn = db.connect(config)
        try:
            n = reaper.requeue_stale_jobs(conn, config.reaper_stale_seconds)
        finally:
            conn.close()
        log.info("reaper requeued %d stale job(s)", n)
        return 0

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("received signal %d; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    worker = WorkerLoop(config, stop_event)
    try:
        worker.run()
    except Exception:  # noqa: BLE001 — top-level guard: log and exit non-zero
        log.exception("worker loop crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
