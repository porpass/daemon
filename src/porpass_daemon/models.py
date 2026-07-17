"""Small dataclasses shared across daemon components.

These model the daemon's own view of the data — not the full DB rows. Contract
shapes (schema artifact / config / manifest) are handled in their own modules
as they are added.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    """A claimed ``processing_jobs`` row, as far as the daemon needs it.

    ``config`` is the authoritative Contract B document parsed from
    ``processing_jobs.config`` (the DB, not the on-disk ``config.json``).
    """

    job_id: int
    user_id: int
    observation_id: int
    config: dict
    output_dir: str | None


@dataclass(frozen=True)
class InputFile:
    """One resolved input file for a job.

    ``role`` is the schema ``inputs`` key (e.g. ``label_file``); ``url`` is the
    ``file_url`` resolved from the instrument's files table; ``local_path`` is
    the download destination in the per-job temp dir.
    """

    role: str
    url: str
    local_path: str
