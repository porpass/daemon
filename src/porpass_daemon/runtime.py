"""Runtime version manifest published to shared storage.

The web frontend shows the running daemon and GRaSP versions in its footer.
Rather than expose an HTTP surface, the daemon writes a small JSON manifest to
``{PORPASS_STORAGE_PATH}/runtime.json`` on startup and the web reads it cold on
each request. That keeps the web decoupled from daemon uptime: if the daemon is
down, the file still reports the versions it last ran with.

Format (the contract the web builds against; see README)::

    {
      "daemon":       "0.1.0a2",
      "grasp":        "0.6.0a1",
      "published_at": "2026-07-19T15:22:00Z"
    }

Version strings are reported verbatim, pre-release suffixes included
(``0.1.0a2``, ``0.1.0-alpha.4``) — nothing is normalised or stripped.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from . import __version__ as DAEMON_VERSION
from .config import Config
from .logging_conf import get_logger

log = get_logger(__name__)

MANIFEST_NAME = "runtime.json"
UNKNOWN_VERSION = "unknown"


def _grasp_version() -> str:
    """Report the GRaSP version the daemon is running against.

    Prefers installed distribution metadata, falling back to
    ``grasp.__version__`` (which GRaSP itself derives from that metadata).
    Returns ``"unknown"`` rather than raising: GRaSP being absent must not stop
    the daemon from starting, and a footer reading "unknown" beats no manifest.
    """
    try:
        return _dist_version("grasp")
    except PackageNotFoundError:
        pass

    try:
        import grasp
    except ImportError:
        return UNKNOWN_VERSION

    return str(getattr(grasp, "__version__", "") or UNKNOWN_VERSION)


def build_manifest() -> dict[str, str]:
    """Build the manifest payload.

    The daemon's own version comes from the module constant rather than
    installed distribution metadata on purpose: an editable install's
    ``.dist-info`` can go stale after a version bump, which would make the
    manifest advertise a version the running code isn't.
    """
    return {
        "daemon": DAEMON_VERSION,
        "grasp": _grasp_version(),
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def publish_runtime_manifest(config: Config) -> Path:
    """Write ``{storage}/runtime.json``, replacing any previous manifest.

    The payload is written to a temporary file and moved into place with
    :func:`os.replace`, so the web — which reads the file cold on every request
    — can never observe a partial write. The mode is set explicitly *before* the
    move, because the web runs as a different user and the daemon's umask would
    otherwise decide whether the footer works.
    """
    storage = config.storage_path
    storage.mkdir(parents=True, exist_ok=True)

    target = storage / MANIFEST_NAME
    tmp = storage / f"{MANIFEST_NAME}.tmp"

    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(build_manifest(), f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o644)     # world-readable: the web runs as another user
        os.replace(tmp, target)  # atomic within the storage directory
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

    return target
