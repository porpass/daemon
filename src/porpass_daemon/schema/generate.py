"""Build and export the per-(instrument, product) Contract A schema artifact.

The daemon owns this generator. GRaSP is imported strictly as a *data source*,
entirely through its stable :mod:`grasp.introspection` surface — the
stage/dataclass registry, the correctness-only enum choices, the per-instrument
defaults table, the section-alias map, and the support matrix. Everything
editorial (titles, dropdown order, visibility rules, default hints) comes from
:mod:`~porpass_daemon.schema.curation`.

Each artifact is one JSON object with a fixed shape (``schema_version``,
``grasp_version``, ``generated_at``, ``instrument``, ``product``,
``target_body``, ``inputs``, ``stages``, ``globals``) that the PORPASS web app
consumes to render job forms.

This module imports ``grasp`` at import time, so import it lazily from callers
that must survive a missing GRaSP (e.g. the best-effort startup publish).
"""

from __future__ import annotations

import json
import typing
from dataclasses import Field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from grasp.introspection import (
    ATTR_TO_LONG,
    GLOBALS,
    STAGES,
    WINDOW_FIELDS,
    MetaDataInfo,
    get_choices,
    get_defaults,
    load_support,
)

from . import SCHEMA_VERSION
from .curation import (
    DEFAULT_NOTES,
    INPUT_FILES,
    INPUT_HELP,
    STAGE_DISABLES,
    STAGE_TITLES,
    TARGET_BODY,
    VISIBLE_WHEN,
    WINDOW_ALIASES,
    WINDOW_DISPLAY_ORDER,
)
from .docstrings import field_help, stage_help

__all__ = ["build_schema", "export_schema", "choice_entries"]


########################################################################
# Public API
########################################################################

def build_schema(instrument: str, product_type: str) -> dict[str, Any]:
    """Assemble the schema dict for one ``(instrument, product_type)`` combo.

    Args:
        instrument: ``"SHARAD"``, ``"MARSIS"``, or ``"LRS"`` (case-insensitive).
        product_type: PDS product type, e.g. ``"EDR"``, ``"RDR"``, ``"US_RDR"``
            (case-insensitive).

    Returns:
        A dict conforming to the Contract A schema. Serialise with
        :func:`json.dumps` or write via :func:`export_schema`.

    Raises:
        ValueError: If the combo is not present in GRaSP's support matrix.
    """
    instrument = instrument.upper()
    product_type = product_type.upper()

    support = load_support()
    if instrument not in support:
        raise ValueError(f"Unknown instrument {instrument!r}; support matrix "
                         f"has {sorted(support)}")
    if product_type not in support[instrument]:
        raise ValueError(
            f"Unknown product_type {product_type!r} for {instrument}; "
            f"support matrix has {sorted(support[instrument])}"
        )
    stage_support = support[instrument][product_type]

    defaults_table = get_defaults(instrument, MetaDataInfo(product_type=product_type))

    stages = [
        _build_stage(stage_attr, cls, defaults_table, stage_support,
                     instrument, product_type)
        for stage_attr, cls in STAGES
    ]
    globals_ = [
        _build_stage(stage_attr, cls, defaults_table, stage_support,
                     instrument, product_type)
        for stage_attr, cls in GLOBALS
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "grasp_version":  _grasp_version(),
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "instrument":     instrument,
        "product":        product_type,
        "target_body":    TARGET_BODY[instrument],
        "inputs":         _build_inputs(instrument, product_type),
        "stages":         stages,
        "globals":        globals_,
    }


def export_schema(
    target_dir: str | Path,
    instrument: str | None = None,
    product_type: str | None = None,
) -> list[Path]:
    """Write one JSON schema file per combo into ``target_dir``.

    Args:
        target_dir: Directory to write to. Created if it doesn't exist.
        instrument: If given, only write this instrument's files.
        product_type: If given (with ``instrument``), only write this one
            combo's file.

    Returns:
        List of paths written, in the order they were emitted.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    combos = _select_combos(instrument, product_type)
    written: list[Path] = []
    for inst, product in combos:
        schema = build_schema(inst, product)
        path = target_dir / f"{inst}_{product}.schema.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
            f.write("\n")
        written.append(path)
    return written


def choice_entries(
    stage_attr: str,
    field_name: str,
    instrument: str | None = None,
    product_type: str | None = None,
) -> list[dict] | None:
    """Return the ordered choice-object list emitted for an enum field.

    Each entry is a dict with keys:

    - ``value``: the accepted string (uppercase, exactly as GRaSP's validator
      matches).
    - ``ui``: whether the web form advertises this option (True) or keeps it as
      a hidden alias (False).
    - ``alias_of``: present only when ``ui`` is False and the value is a
      non-canonical alias of another entry in the same list.

    For window-typed fields, the ui:true entries appear first in
    :data:`~porpass_daemon.schema.curation.WINDOW_DISPLAY_ORDER`; every other
    window name GRaSP accepts is emitted as ui:false with
    :data:`~porpass_daemon.schema.curation.WINDOW_ALIASES` supplying the
    canonical value.

    For all other enum fields, all entries are ui:true, sorted for determinism,
    with no aliases.

    Returns:
        List of dicts, or ``None`` if the field has no bounded choice list.
    """
    valid = get_choices(stage_attr, field_name, instrument, product_type)
    if valid is None:
        return None

    if _is_window_field(stage_attr, field_name):
        entries: list[dict] = []
        # Canonical, ui:true, in the curated display order.
        for value in WINDOW_DISPLAY_ORDER:
            if value in valid:
                entries.append({"value": value, "ui": True})
        # Aliases + any other value GRaSP accepts but we don't advertise.
        remaining = sorted(valid - set(WINDOW_DISPLAY_ORDER))
        for value in remaining:
            entry: dict = {"value": value, "ui": False}
            canonical = WINDOW_ALIASES.get(value)
            if canonical is not None and canonical in valid:
                entry["alias_of"] = canonical
            entries.append(entry)
        return entries

    return [{"value": v, "ui": True} for v in sorted(valid)]


########################################################################
# Helpers
########################################################################

def _is_window_field(stage_attr: str, field_name: str) -> bool:
    """True iff ``(stage_attr, field_name)`` selects a window name.

    Uses GRaSP's declared :data:`grasp.introspection.WINDOW_FIELDS` set, which
    is derived by value equality — so this no longer depends on every window
    field's choice set being the same ``WINDOW_NAMES`` object.
    """
    return (stage_attr, field_name) in WINDOW_FIELDS


def _select_combos(
    instrument: str | None,
    product_type: str | None,
) -> list[tuple[str, str]]:
    support = load_support()
    if instrument is None:
        return [
            (inst, product)
            for inst in sorted(support)
            for product in sorted(support[inst])
        ]
    inst = instrument.upper()
    if inst not in support:
        raise ValueError(f"Unknown instrument {instrument!r}")
    if product_type is None:
        return [(inst, product) for product in sorted(support[inst])]
    product = product_type.upper()
    if product not in support[inst]:
        raise ValueError(
            f"Unknown product_type {product_type!r} for {instrument}"
        )
    return [(inst, product)]


def _build_stage(
    stage_attr: str,
    cls: type,
    defaults_table: dict[str, dict[str, Any]],
    stage_support: dict[str, bool],
    instrument: str,
    product_type: str,
) -> dict[str, Any]:
    """Assemble one stage or globals block."""
    long_name = ATTR_TO_LONG.get(stage_attr, stage_attr)
    supported = stage_support.get(stage_attr, True)  # globals are always "supported"
    stage_defaults = defaults_table.get(stage_attr, {})
    helps = field_help(cls)

    field_dicts = [
        _build_field(stage_attr, f, stage_defaults, helps, instrument, product_type)
        for f in _dataclass_fields(cls, stage_attr)
    ]

    out: dict[str, Any] = {
        "key":        long_name,
        "dataclass":  cls.__name__,
        "supported":  supported,
        "title":      STAGE_TITLES.get(stage_attr, stage_attr),
        "help":       stage_help(cls),
        "fields":     field_dicts,
    }
    for (s, field_name, value), disables in STAGE_DISABLES.items():
        if s == stage_attr:
            out.setdefault("disables", []).append({
                "when":   {"field": field_name, "equals": value},
                "stages": list(disables),
            })
    return out


def _dataclass_fields(cls: type, stage_attr: str) -> list[Field]:
    """Return the dataclass fields for one stage.

    For the top-level ``general`` block we only want the scalar globals
    (``out_dir``, ``verbose``) — not the nested stage-parameter fields like
    ``preprocessing`` / ``range_compression`` / etc.
    """
    if stage_attr == "general":
        return [f for f in fields(cls) if f.name in ("out_dir", "verbose")]
    return list(fields(cls))


def _build_field(
    stage_attr: str,
    field: Field,
    stage_defaults: dict[str, Any],
    helps: dict[str, str],
    instrument: str,
    product_type: str,
) -> dict[str, Any]:
    """Assemble one field entry."""
    name = field.name
    typing_info = _field_typing_info(
        stage_attr, name, field.type, instrument, product_type
    )

    default = stage_defaults.get(name, None)
    entry: dict[str, Any] = {
        "name":    name,
        "default": default,
        "help":    helps.get(name, ""),
    }
    entry.update(typing_info)
    if default is None:
        note = DEFAULT_NOTES.get((stage_attr, name))
        if note is not None:
            entry["default_note"] = note
    visible = VISIBLE_WHEN.get((stage_attr, name))
    if visible is not None:
        entry["visible_when"] = dict(visible)
    return entry


def _field_typing_info(
    stage_attr: str,
    field_name: str,
    annotation: Any,
    instrument: str,
    product_type: str,
) -> dict[str, Any]:
    """Return the type-shaped payload for one field.

    Shape:

    - Non-enum:  ``{"type": "<bool|int|float|str>"}``
    - Enum:      ``{"type": "enum", "value_type": "<str|int>",
                    "choices": [{"value": ..., "ui": bool,
                                 "alias_of": <str, optional>}, ...]}``

    ``"enum"`` iff the field is in GRaSP's CHOICES registry (respecting
    per-combo restrictions), or the annotation is ``Literal[-1, 1] | None``
    (``sgn``). The ``value_type`` key tells the web whether to render/emit the
    value as a quoted string or a bare number when writing TOML back out —
    required to keep the round-trip type-correct for numeric enums.
    """
    inner = _strip_optional(annotation)

    # sgn is Literal[-1, 1] — numeric enum. Emit ints (not strings) so a
    # re-serialised job writes ``sgn = -1`` rather than ``sgn = "-1"``,
    # matching the int-typed dataclass field.
    if get_origin(inner) is Literal:
        values = sorted(get_args(inner))
        return {
            "type":       "enum",
            "value_type": "int",
            "choices":    [{"value": v, "ui": True} for v in values],
        }

    entries = choice_entries(stage_attr, field_name, instrument, product_type)
    if entries is not None:
        return {
            "type":       "enum",
            "value_type": "str",
            "choices":    entries,
        }

    # Path-like fields get "str".
    if inner in (str, Path) or _is_str_or_path(inner):
        return {"type": "str"}
    if inner is bool:
        return {"type": "bool"}
    if inner is int:
        return {"type": "int"}
    if inner is float:
        return {"type": "float"}
    return {"type": "str"}


def _strip_optional(annotation: Any) -> Any:
    """Strip ``| None`` / ``Optional[T]`` down to ``T``."""
    if get_origin(annotation) is typing.Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    # Support the PEP 604 form (``X | None``) which shows up in get_origin
    # as types.UnionType at runtime.
    if hasattr(annotation, "__args__") and type(None) in getattr(annotation, "__args__", ()):
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) == 1:
            return args[0]
    if isinstance(annotation, str):
        # String forward-refs (from `from __future__ import annotations`).
        # Best-effort match on the common shapes.
        stripped = annotation.replace(" ", "")
        for base in ("bool", "int", "float", "str", "Path"):
            if stripped == f"{base}|None" or stripped == base:
                return {"bool": bool, "int": int, "float": float,
                        "str": str, "Path": Path}[base]
    return annotation


def _is_str_or_path(annotation: Any) -> bool:
    if annotation in (str, Path):
        return True
    if isinstance(annotation, str):
        return annotation.strip() in ("str", "Path")
    return False


def _build_inputs(instrument: str, product_type: str) -> dict[str, dict[str, Any]]:
    files = INPUT_FILES.get((instrument, product_type), ())
    return {
        f"{kind}_file": {"required": True, "help": INPUT_HELP[kind]}
        for kind in files
    }


def _grasp_version() -> str:
    import grasp
    return grasp.__version__
