"""Contract C — write manifest.json for a finished job.

After a run the daemon scans the job directory and records every produced file:
its path (relative to the job dir), a ``kind`` from the fixed enum, size in
bytes, a content type, and ``deleted: false`` (only the web ever flips that).
run.log is included with ``kind: "log"`` — the one kind the web preserves through
a results-delete.

The provenance/config files (config.json, job.toml, and manifest.json itself)
are not products and are managed by the web separately, so they are excluded.

Kind classification is driven by file extension, with clutter-simulation native
data products (which carry GRaSP's ``csim`` level token in their name) mapped to
``cluttergram``. This mapping is the one place the daemon leans on GRaSP's output
naming; it is intentionally small and easy to adjust.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_conf import get_logger

log = get_logger(__name__)

MANIFEST_VERSION = "1.0"
MANIFEST_NAME = "manifest.json"

# Provenance/config files the web keeps and manages itself — never products.
_EXCLUDED_NAMES = {"config.json", "job.toml", MANIFEST_NAME}

# GRaSP's clutter-sim outputs are named via _build_base("csim", ...).
_CSIM_TOKEN = "csim"

# extension -> (kind, content_type). Kinds are the Contract C enum:
# data | image | segy | state | cluttergram | log | other
_EXT_MAP: dict[str, tuple[str, str]] = {
    ".h5": ("state", "application/x-hdf5"),
    ".sgy": ("segy", "application/octet-stream"),
    ".segy": ("segy", "application/octet-stream"),
    ".img": ("data", "application/octet-stream"),
    ".grsp": ("data", "text/plain"),
    ".csv": ("data", "text/csv"),
    ".bmp": ("image", "image/bmp"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".tif": ("image", "image/tiff"),
    ".tiff": ("image", "image/tiff"),
    ".log": ("log", "text/plain"),
    ".json": ("other", "application/json"),
    ".txt": ("other", "text/plain"),
}
_DEFAULT = ("other", "application/octet-stream")


def classify(path: Path) -> tuple[str, str]:
    """Return ``(kind, content_type)`` for a produced file."""
    if path.name == "run.log":
        return ("log", "text/plain")
    kind, content_type = _EXT_MAP.get(path.suffix.lower(), _DEFAULT)
    # Native clutter-sim data products (.img/.csv/.grsp with the csim token) are
    # cluttergrams; rendered images stay 'image' so the web can preview them.
    if kind == "data" and _CSIM_TOKEN in path.stem.lower():
        kind = "cluttergram"
    return (kind, content_type)


def build_manifest(job_dir: str | Path, grasp_version: str) -> dict[str, Any]:
    """Scan ``job_dir`` and build the Contract C manifest document."""
    job_dir = Path(job_dir)
    files: list[dict[str, Any]] = []

    for path in sorted(job_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(job_dir)
        if rel.name in _EXCLUDED_NAMES or rel.name.startswith("."):
            continue
        kind, content_type = classify(path)
        files.append(
            {
                "path": str(rel),               # relative, no leading slash, no ..
                "kind": kind,
                "bytes": path.stat().st_size,
                "content_type": content_type,
                "deleted": False,               # only the web flips this
            }
        )

    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grasp_version": grasp_version,
        "files": files,
    }


def write_manifest(job_dir: str | Path, grasp_version: str) -> Path:
    """Build and write ``manifest.json`` into ``job_dir``. Returns its path."""
    doc = build_manifest(job_dir, grasp_version)
    path = Path(job_dir) / MANIFEST_NAME
    path.write_text(json.dumps(doc, indent=2) + "\n")
    log.info("wrote manifest with %d file(s) to %s", len(doc["files"]), path)
    return path
