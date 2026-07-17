"""Logging setup for the daemon.

The gis/ service has no logging convention to mirror, so the daemon introduces
its own: a single stream handler with a timestamped, level-tagged format that
reads well both in a terminal and in journald under systemd.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once, writing to stderr.

    Idempotent: repeated calls do not stack handlers.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. ``get_logger(__name__)``."""
    return logging.getLogger(name)
