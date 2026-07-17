"""Input resolution — fetch an observation's source files from the archives.

For a claimed job the daemon must make the observation's PDS/DARTS files
available locally for GRaSP. The flow, driven entirely by the schema artifact
and the database (no instrument-specific processing hardcoded):

1. From the job's ``observation_id`` look up the observation and its instrument.
2. For each input *role* the schema artifact requires (``label_file``,
   ``science_file``, ``auxiliary_file``), map the role to a DB ``file_type`` and
   resolve its ``file_url`` from the instrument's files table.
3. Make each file available in a per-job temp dir, preserving the archive
   filename so a PDS label's internal references to its science/aux files still
   resolve. An http(s) reference is downloaded; a local filesystem path (e.g.
   LRS EDR staged on a mounted disk) is copied.
4. Return a mapping of role -> local path. The caller renders these into the
   job.toml ``[input]`` block and cleans up the temp dir after the run.

The role->file_type and instrument->table maps below are the daemon-owned bridge
between the schema's role vocabulary and the DB's file_type vocabulary; neither
the contract nor the schema carries this mapping.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .logging_conf import get_logger
from .models import Job
from .schema import SchemaArtifact

log = get_logger(__name__)

# Schema input role -> DB file_type code. LRS has no auxiliary_file (and no AUX
# file_type), which is fine: only roles present in a given artifact's `inputs`
# are resolved.
ROLE_TO_FILE_TYPE = {
    "label_file": "LBL",
    "science_file": "SCI",
    "auxiliary_file": "AUX",
}

# Instrument abbreviation -> per-instrument files table. Fixed allowlist so the
# table name is never derived directly from a value interpolated into SQL.
INSTRUMENT_FILES_TABLE = {
    "LRS": "lrs_files",
    "SHARAD": "sharad_files",
    "MARSIS": "marsis_files",
}

# Download tuning.
_CONNECT_TIMEOUT = 30.0
_READ_TIMEOUT = 300.0
_CHUNK_BYTES = 1 << 20  # 1 MiB
_RETRY_TOTAL = 3


class InputResolutionError(Exception):
    """Inputs could not be resolved or downloaded for a job."""


@dataclass
class ResolvedInputs:
    """Locally-available inputs for one job.

    ``files`` maps each schema input role to its downloaded path inside
    ``temp_dir``. Call :meth:`cleanup` after the run regardless of outcome.
    """

    temp_dir: Path
    files: dict[str, Path]

    def cleanup(self) -> None:
        """Remove the temp dir and everything in it. Safe to call twice."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def file_type_for_role(role: str) -> str:
    """Return the DB file_type for a schema input role, or raise."""
    try:
        return ROLE_TO_FILE_TYPE[role]
    except KeyError:
        raise InputResolutionError(
            f"no file_type mapping for input role {role!r}"
        ) from None


def files_table_for_instrument(instrument_abbr: str) -> str:
    """Return the files table for an instrument abbreviation, or raise."""
    try:
        return INSTRUMENT_FILES_TABLE[instrument_abbr.upper()]
    except KeyError:
        raise InputResolutionError(
            f"no files table for instrument {instrument_abbr!r}"
        ) from None


def local_filename_for_url(url: str) -> str:
    """Derive a safe local filename from a URL, preserving the archive name.

    Uses the URL path's basename (percent-decoded). Any path separators are
    stripped so the result cannot escape the download directory.
    """
    name = os.path.basename(unquote(urlparse(url).path))
    name = name.replace("/", "_").replace("\\", "_").strip()
    if not name or name in (".", ".."):
        raise InputResolutionError(f"cannot derive a filename from URL {url!r}")
    return name


# --------------------------------------------------------------------------- #
# Database resolution
# --------------------------------------------------------------------------- #

def _lookup_observation(
    conn: pymysql.connections.Connection, observation_id: int
) -> tuple[str, str]:
    """Return ``(native_id, instrument_abbr)`` for an observation, or raise."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.native_id, i.instrument_abbr "
            "FROM observations o "
            "JOIN instruments i ON i.instrument_id = o.instrument_id "
            "WHERE o.observation_id = %s",
            (observation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise InputResolutionError(f"observation {observation_id} not found")
    return row["native_id"], row["instrument_abbr"]


def _lookup_file_url(
    conn: pymysql.connections.Connection,
    table: str,
    observation_id: int,
    file_type: str,
) -> str:
    """Resolve exactly one ``file_url`` for an (observation, file_type).

    Zero matches (a required input is missing) or more than one (ambiguous)
    both raise :class:`InputResolutionError`.
    """
    # `table` comes from the fixed INSTRUMENT_FILES_TABLE allowlist, never from
    # user input; values stay parameterised.
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT file_url FROM {table} "
            "WHERE observation_id = %s AND file_type = %s",
            (observation_id, file_type),
        )
        rows = cur.fetchall()
    if not rows:
        raise InputResolutionError(
            f"no {file_type} file for observation {observation_id} in {table}"
        )
    if len(rows) > 1:
        raise InputResolutionError(
            f"{len(rows)} {file_type} files for observation {observation_id} "
            f"in {table}; expected exactly one"
        )
    return rows[0]["file_url"]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def build_session(retries: int = _RETRY_TOTAL) -> requests.Session:
    """Return a requests Session that retries transient archive failures."""
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download_file(
    url: str,
    dest_dir: Path,
    session: requests.Session,
    *,
    timeout: tuple[float, float] = (_CONNECT_TIMEOUT, _READ_TIMEOUT),
) -> Path:
    """Stream ``url`` into ``dest_dir`` under its archive filename.

    Verifies the written size against ``Content-Length`` when the server
    provides it, so a truncated transfer is caught. Returns the local path.
    """
    name = local_filename_for_url(url)
    dest = dest_dir / name
    log.info("downloading %s -> %s", url, dest)

    try:
        with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as resp:
            if resp.status_code != 200:
                raise InputResolutionError(
                    f"GET {url} returned HTTP {resp.status_code}"
                )
            declared = resp.headers.get("Content-Length")
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK_BYTES):
                    fh.write(chunk)
                    written += len(chunk)
    except requests.RequestException as exc:
        raise InputResolutionError(f"failed to download {url}: {exc}") from exc

    if declared is not None and int(declared) != written:
        raise InputResolutionError(
            f"truncated download of {url}: got {written} bytes, "
            f"expected {declared}"
        )
    return dest


def is_remote_ref(ref: str) -> bool:
    """True if ``ref`` is an http(s) URL; False for a local filesystem path.

    Input references in the DB are usually PDS/DARTS URLs, but some collections
    (e.g. LRS EDR) are staged on a locally-mounted disk and stored as absolute
    paths. Anything without an http/https scheme is treated as a local path.
    """
    return urlparse(ref).scheme.lower() in ("http", "https")


def copy_local_file(source: str, dest_dir: Path) -> Path:
    """Copy a locally-staged input into ``dest_dir`` under its own filename."""
    src = Path(source)
    if not src.is_file():
        raise InputResolutionError(f"local input file not found: {source}")
    dest = dest_dir / src.name
    log.info("copying local input %s -> %s", src, dest)
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        raise InputResolutionError(f"failed to copy {source}: {exc}") from exc
    return dest


def fetch_input(ref: str, dest_dir: Path, session: requests.Session) -> Path:
    """Make an input available in ``dest_dir``: download a URL, or copy a path."""
    if is_remote_ref(ref):
        return download_file(ref, dest_dir, session)
    return copy_local_file(ref, dest_dir)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def resolve_inputs(
    conn: pymysql.connections.Connection,
    job: Job,
    artifact: SchemaArtifact,
    *,
    dest_parent: Path | None = None,
    session: requests.Session | None = None,
) -> ResolvedInputs:
    """Resolve and fetch all required inputs for a job.

    Fetches into a fresh temp dir (under ``dest_parent`` if given, else the
    system temp) — downloading http(s) references and copying local paths. Only
    roles present in the artifact's ``inputs`` are fetched; a role marked
    ``required`` that cannot be resolved fails the whole call.

    On any failure the partial temp dir is cleaned up before raising, so callers
    never leak a directory on the error path.
    """
    native_id, instrument_abbr = _lookup_observation(conn, job.observation_id)
    if artifact.instrument.upper() != instrument_abbr.upper():
        raise InputResolutionError(
            f"artifact instrument {artifact.instrument!r} does not match "
            f"observation {job.observation_id} instrument {instrument_abbr!r}"
        )
    table = files_table_for_instrument(instrument_abbr)

    owns_session = session is None
    session = session or build_session()
    temp_dir = Path(tempfile.mkdtemp(prefix=f"porpass-job-{job.job_id}-", dir=dest_parent))
    files: dict[str, Path] = {}

    try:
        for role, spec in artifact.inputs.items():
            required = bool(spec.get("required", True))
            file_type = file_type_for_role(role)
            try:
                ref = _lookup_file_url(conn, table, job.observation_id, file_type)
            except InputResolutionError:
                if required:
                    raise
                log.info("optional input %s (%s) absent; skipping", role, file_type)
                continue
            files[role] = fetch_input(ref, temp_dir, session)

        log.info(
            "resolved %d input(s) for job %s (%s %s) into %s",
            len(files), job.job_id, instrument_abbr, native_id, temp_dir,
        )
        return ResolvedInputs(temp_dir=temp_dir, files=files)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        if owns_session:
            session.close()
