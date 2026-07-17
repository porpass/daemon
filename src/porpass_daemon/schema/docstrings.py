"""Tiny Google-style docstring parser for the schema generator.

Extracts the two things the generator needs from a GRaSP parameter dataclass:

- :func:`stage_help`: the summary paragraph at the top of the class docstring,
  used as stage-level help.
- :func:`field_help`: a ``{field_name: help_text}`` map extracted from the
  ``Attributes:`` block.

Deliberately narrow: not a general Google-style parser. If a docstring doesn't
have an ``Attributes:`` block, :func:`field_help` returns an empty dict.

Copied verbatim from GRaSP's former ``grasp.schema.docstrings`` when the schema
generator moved into the daemon. The parsing behaviour must not drift — GRaSP's
dataclass docstrings are the input, and the byte-identity regression test in
``tests/test_schema_generate.py`` pins the output.
"""

from __future__ import annotations

import inspect
import re

_FIELD_START = re.compile(r"^(?P<name>\w+):\s*(?P<rest>.*)$")


def stage_help(cls: type) -> str:
    """Return the first paragraph of ``cls.__doc__``.

    Args:
        cls: A dataclass with a Google-style docstring.

    Returns:
        The first paragraph (up to the first blank line) with whitespace
        collapsed. Empty string if the docstring is missing or empty.
    """
    doc = inspect.getdoc(cls)
    if not doc:
        return ""
    paragraph = []
    for line in doc.splitlines():
        if not line.strip():
            break
        paragraph.append(line.strip())
    return " ".join(paragraph)


def field_help(cls: type) -> dict[str, str]:
    """Return ``{field_name: help_text}`` parsed from the ``Attributes:`` block.

    Continuation lines (indented deeper than the field-name line) are joined
    with a single space. Field-name lines that don't match the ``name: text``
    shape are skipped.

    Args:
        cls: A dataclass with a Google-style docstring.

    Returns:
        Mapping from field name to a single-line help string. Empty when the
        docstring has no ``Attributes:`` block or is missing.
    """
    doc = inspect.getdoc(cls)
    if not doc:
        return {}
    lines = doc.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.rstrip() == "Attributes:"
        )
    except StopIteration:
        return {}

    body = lines[start + 1:]

    # Establish the field-name indent from the first non-blank line.
    field_indent = None
    for line in body:
        if line.strip():
            field_indent = len(line) - len(line.lstrip())
            break
    if field_indent is None:
        return {}

    out: dict[str, str] = {}
    current: str | None = None
    parts: list[str] = []

    def _flush():
        if current is not None:
            out[current] = " ".join(p for p in parts if p).strip()

    for line in body:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not stripped:
            # Blank line inside a block just breaks the previous field's
            # tail — keep going in case the block resumes.
            continue
        if indent < field_indent:
            # Dedent below the Attributes-block indent -> block ended.
            break
        if indent == field_indent:
            match = _FIELD_START.match(stripped)
            if match:
                _flush()
                current = match.group("name")
                parts = [match.group("rest")]
            else:
                # Not a field-name line at the block indent — skip it.
                _flush()
                current = None
                parts = []
        else:
            # Continuation of the current field.
            if current is not None:
                parts.append(stripped)

    _flush()
    return out
