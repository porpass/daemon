"""Contract A — schema artifacts.

Three responsibilities, split across this package:

1. **Generate** the artifacts (:mod:`~porpass_daemon.schema.generate`). The
   daemon owns the generator; GRaSP is imported only as a data source through
   its stable ``grasp.introspection`` surface. The web-form curation that drives
   it (titles, dropdown order, visibility rules, default hints) lives in
   :mod:`~porpass_daemon.schema.curation`.

2. **Publish** the version-matched artifacts to shared storage on startup, so
   the web's config forms stay in lock-step with the running GRaSP version (see
   the web repo's ``docs/processing_contracts.md`` §2).

3. **Load / validate** an artifact into a light typed wrapper. The renderer
   walks the same structure to build ``job.toml``, so parsing and validation
   live here once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..logging_conf import get_logger

log = get_logger(__name__)

# The only schema_version this daemon understands. Contract A is frozen at 1.1;
# a mismatch means GRaSP and the daemon are out of step and must be reconciled
# on the contract side, not papered over here.
SCHEMA_VERSION = "1.1"


class SchemaError(ValueError):
    """A schema artifact is malformed, missing, or an unsupported version."""


@dataclass(frozen=True)
class SchemaArtifact:
    """A parsed, validated Contract A document.

    ``data`` is the raw JSON dict; the accessors expose the pieces the daemon
    cares about without copying. ``path`` is the file it was loaded from, or
    ``None`` when parsed from memory.
    """

    data: dict[str, Any]
    path: Path | None = None

    @property
    def schema_version(self) -> str:
        return self.data["schema_version"]

    @property
    def grasp_version(self) -> str:
        return self.data.get("grasp_version", "")

    @property
    def instrument(self) -> str:
        return self.data["instrument"]

    @property
    def product(self) -> str:
        return self.data["product"]

    @property
    def inputs(self) -> dict[str, dict[str, Any]]:
        """Map of input role -> ``{required, help}`` (Contract A ``inputs``)."""
        return self.data["inputs"]

    @property
    def stages(self) -> list[dict[str, Any]]:
        return self.data["stages"]

    @property
    def globals(self) -> list[dict[str, Any]]:
        return self.data["globals"]

    def sections(self) -> list[dict[str, Any]]:
        """All sections — stages then globals — in wire order."""
        return [*self.data["stages"], *self.data["globals"]]

    def section(self, key: str) -> dict[str, Any] | None:
        """Return the section with the given canonical ``key``, or ``None``."""
        for sec in self.sections():
            if sec.get("key") == key:
                return sec
        return None


def _validate(data: Any, source: str) -> None:
    """Raise :class:`SchemaError` unless ``data`` is a well-formed artifact."""
    if not isinstance(data, dict):
        raise SchemaError(f"{source}: schema root is not a JSON object")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SchemaError(
            f"{source}: unsupported schema_version {version!r} "
            f"(this daemon supports {SCHEMA_VERSION!r})"
        )

    for key in ("instrument", "product", "inputs", "stages", "globals"):
        if key not in data:
            raise SchemaError(f"{source}: missing required key {key!r}")

    if not isinstance(data["inputs"], dict):
        raise SchemaError(f"{source}: 'inputs' must be an object")
    if not isinstance(data["stages"], list) or not isinstance(data["globals"], list):
        raise SchemaError(f"{source}: 'stages' and 'globals' must be arrays")


def parse_artifact(data: dict[str, Any], source: str = "<memory>") -> SchemaArtifact:
    """Validate an in-memory schema dict and wrap it."""
    _validate(data, source)
    return SchemaArtifact(data=data)


def load_artifact(path: str | Path) -> SchemaArtifact:
    """Read, validate, and wrap a schema artifact from disk."""
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as exc:
        raise SchemaError(f"schema artifact not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{p}: invalid JSON — {exc}") from exc
    _validate(data, str(p))
    return SchemaArtifact(data=data, path=p)


def publish_schemas(config: Config) -> list[Path]:
    """Regenerate the schema artifacts into ``{storage}/schemas/``.

    Generates one ``{INSTRUMENT}_{PRODUCT}.schema.json`` per supported combo
    in-process via :mod:`~porpass_daemon.schema.generate`, then validates every
    file written so a bad artifact surfaces here rather than downstream in the
    web. Returns the written paths.

    GRaSP is imported lazily (the generator needs it as a data source), so a
    daemon running without GRaSP importable still starts — this raises
    :class:`SchemaError` and the best-effort startup path logs and continues.

    Raises :class:`SchemaError` if GRaSP is unavailable or generation fails.
    """
    out_dir = config.schemas_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("generating schema artifacts into %s", out_dir)

    try:
        from .generate import export_schema
    except ImportError as exc:
        raise SchemaError(
            f"GRaSP is not importable, so schema artifacts cannot be "
            f"generated: {exc}"
        ) from exc

    try:
        written = export_schema(out_dir)
    except Exception as exc:  # noqa: BLE001 — surface any generator failure uniformly
        raise SchemaError(f"schema generation failed: {exc}") from exc

    for path in written:
        load_artifact(path)  # validate; raises SchemaError on a bad artifact

    log.info("published %d schema artifact(s) to %s", len(written), out_dir)
    return written
