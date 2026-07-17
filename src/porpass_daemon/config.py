"""Runtime configuration for the PORPASS processing daemon.

Configuration comes entirely from the environment, loaded from a ``.env`` file
when present (mirroring how ``gis/main.py`` bootstraps the web's Python service).
The DB and storage variable names are shared verbatim with porpass-web so one
set of credentials serves the whole system.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def load_env() -> None:
    """Load a repo-local ``.env`` if one exists.

    Called once at process start. In production the systemd unit supplies the
    environment directly, so a missing ``.env`` is not an error.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)


@dataclass(frozen=True)
class Config:
    """Resolved daemon configuration."""

    # Database (MariaDB) — names match porpass-web's .env and gis/.
    db_host: str
    db_port: int
    db_database: str
    db_username: str
    db_password: str

    # Shared porpass-storage mount root.
    storage_path: Path

    # GRaSP executable, invoked as `{grasp_bin} run <job.toml> -v`.
    grasp_bin: str

    # Daemon behaviour.
    poll_interval: float
    heartbeat_interval: float
    reaper_stale_seconds: int
    cancel_poll_interval: float
    worker_id: str
    publish_schemas_on_start: bool

    @property
    def schemas_dir(self) -> Path:
        """Directory schema artifacts are published to."""
        return self.storage_path / "schemas"

    @property
    def processing_dir(self) -> Path:
        """Root of the per-user/per-job processing tree."""
        return self.storage_path / "processing"


def _default_worker_id() -> str:
    """Derive a ``{hostname}-{pid}`` worker identity.

    Distinguishes concurrent workers in ``processing_jobs.claimed_by`` (a
    varchar(64)); the hostname is truncated defensively to stay within bounds.
    """
    host = socket.gethostname().split(".")[0][:48]
    return f"{host}-{os.getpid()}"


def load_config() -> Config:
    """Build a :class:`Config` from the current environment.

    Call :func:`load_env` first so a ``.env`` file is honoured.
    """
    storage_raw = os.environ.get("PORPASS_STORAGE_PATH", "").strip()
    if not storage_raw:
        raise RuntimeError(
            "PORPASS_STORAGE_PATH is required — set it to the porpass-storage "
            "mount root shared with porpass-web."
        )

    worker_id = os.environ.get("DAEMON_WORKER_ID", "").strip() or _default_worker_id()

    return Config(
        db_host=os.environ.get("DB_HOST", "localhost"),
        db_port=int(os.environ.get("DB_PORT", "3306")),
        db_database=os.environ.get("DB_DATABASE", "porpass"),
        db_username=os.environ.get("DB_USERNAME", "porpass"),
        db_password=os.environ.get("DB_PASSWORD", ""),
        storage_path=Path(storage_raw),
        grasp_bin=os.environ.get("GRASP_BIN", "grasp"),
        poll_interval=float(os.environ.get("DAEMON_POLL_INTERVAL", "5")),
        heartbeat_interval=float(os.environ.get("DAEMON_HEARTBEAT_INTERVAL", "30")),
        reaper_stale_seconds=int(os.environ.get("DAEMON_REAPER_STALE_SECONDS", "300")),
        cancel_poll_interval=float(os.environ.get("DAEMON_CANCEL_POLL_INTERVAL", "5")),
        worker_id=worker_id,
        publish_schemas_on_start=os.environ.get(
            "DAEMON_PUBLISH_SCHEMAS_ON_START", "1"
        ).strip()
        not in ("0", "false", "False", ""),
    )
