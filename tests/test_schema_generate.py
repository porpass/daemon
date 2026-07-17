"""Regression: the daemon's schema generator must match GRaSP's exporter.

The generator was extracted from ``grasp.schema.export`` into this repo. Until
GRaSP drops its ``export-schema`` CLI, we can pin the extraction by diffing the
two outputs byte-for-byte (modulo the ``generated_at`` timestamp, which differs
by construction).

Both sides are skipped when GRaSP isn't importable / on PATH, so the suite still
runs in a bare environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

grasp = pytest.importorskip("grasp", reason="GRaSP not importable")

from porpass_daemon.schema.generate import build_schema, export_schema  # noqa: E402

# Every combo GRaSP's support matrix declares.
EXPECTED_COMBOS = {
    "SHARAD_EDR", "SHARAD_RDR", "SHARAD_US_RDR",
    "MARSIS_EDR", "MARSIS_RDR",
    "LRS_EDR", "LRS_RDR",
}


def _mask_generated_at(doc: dict) -> dict:
    """Blank the one field that legitimately differs between two runs."""
    return {**doc, "generated_at": "<masked>"}


def _grasp_cli() -> str | None:
    return shutil.which("grasp")


# --------------------------------------------------------------------------- #
# The generator on its own
# --------------------------------------------------------------------------- #

def test_export_writes_every_combo(tmp_path: Path) -> None:
    written = export_schema(tmp_path)
    assert {p.stem.replace(".schema", "") for p in written} == EXPECTED_COMBOS
    for path in written:
        doc = json.loads(path.read_text())
        assert doc["schema_version"] == "1.1"
        assert doc["grasp_version"] == grasp.__version__
        assert len(doc["stages"]) == 7 and len(doc["globals"]) == 4


def test_curation_is_applied(tmp_path: Path) -> None:
    """Spot-check that daemon-owned curation reaches the artifact."""
    doc = build_schema("SHARAD", "EDR")
    sections = {s["key"]: s for s in [*doc["stages"], *doc["globals"]]}

    # STAGE_TITLES + ATTR_TO_LONG (long canonical keys, not short attrs)
    assert sections["ionospheric_compensation"]["title"] == "Ionospheric Compensation"
    # STAGE_DISABLES
    assert sections["sar_processing"]["disables"] == [
        {"when": {"field": "method", "equals": "BACKSCATTER"},
         "stages": ["multilooking"]}
    ]
    fields = {f["name"]: f for f in sections["ionospheric_compensation"]["fields"]}
    # VISIBLE_WHEN
    assert fields["campbell_b"]["visible_when"] == {"field": "method",
                                                    "equals": "CAMPBELL"}
    # DEFAULT_NOTES (only on null defaults)
    assert fields["metric"]["default_note"] == "PEAK_SNR (Campbell) / L4 (Contrast)"
    # numeric enum stays int-typed
    assert fields["sgn"]["value_type"] == "int"
    assert [c["value"] for c in fields["sgn"]["choices"]] == [-1, 1]
    # WINDOW_DISPLAY_ORDER first (ui:true), aliases after (ui:false + alias_of)
    win = fields["window"]["choices"]
    assert win[0]["value"] == "RECTANGLE" and win[0]["ui"] is True
    aliases = {c["value"]: c.get("alias_of") for c in win if not c["ui"]}
    assert aliases.get("HANNING") == "HANN"
    assert aliases.get("BH") == "BLACKMAN-HARRIS"


def test_restrictions_narrow_choices() -> None:
    """MARSIS has no calibrated chirps — RESTRICTIONS must narrow chirp_type."""
    marsis = build_schema("MARSIS", "EDR")
    rc = next(s for s in marsis["stages"] if s["key"] == "range_compression")
    chirp = next(f for f in rc["fields"] if f["name"] == "chirp_type")
    assert [c["value"] for c in chirp["choices"]] == ["IDEAL"]

    sharad = build_schema("SHARAD", "EDR")
    rc = next(s for s in sharad["stages"] if s["key"] == "range_compression")
    chirp = next(f for f in rc["fields"] if f["name"] == "chirp_type")
    assert set(c["value"] for c in chirp["choices"]) == {"IDEAL", "CALIBRATED"}


def test_lrs_has_no_auxiliary_input() -> None:
    assert set(build_schema("LRS", "EDR")["inputs"]) == {"label_file", "science_file"}
    assert "auxiliary_file" in build_schema("SHARAD", "EDR")["inputs"]


def test_cmap_still_owned_by_grasp_tripwire() -> None:
    """Tripwire for GRaSP Phase 3, when ``("plots","cmap")`` leaves CHOICES.

    Today GRaSP's CHOICES still carries the curated cmap short-list, so
    ``get_choices`` supplies it and our ``curation.CMAP_NAMES`` copy is a
    deliberately unused stand-by. In Phase 3 (~grasp 0.6.0a2) GRaSP drops the
    entry, ``get_choices`` starts returning None, and ``plots.cmap`` would
    silently degrade from an enum to a plain ``{"type": "str"}`` field.

    This test fails the moment that happens, so the hand-off is loud. When it
    does: make the generator fall back to ``curation.CMAP_NAMES`` for
    ``("plots", "cmap")``, then update this test to assert the fallback.
    """
    from grasp.introspection import get_choices as grasp_get_choices

    from porpass_daemon.schema.curation import CMAP_NAMES

    grasp_cmaps = grasp_get_choices("plots", "cmap", "SHARAD", "EDR")
    assert grasp_cmaps is not None, (
        "GRaSP dropped ('plots','cmap') from CHOICES — Phase 3 has landed. "
        "Activate curation.CMAP_NAMES as the source for cmap choices in "
        "schema/generate.py, then update this test."
    )
    # While GRaSP still owns it, our stand-by copy must not drift from it.
    assert set(grasp_cmaps) == set(CMAP_NAMES), (
        "curation.CMAP_NAMES has drifted from GRaSP's CHOICES entry; "
        "reconcile before Phase 3 makes our copy authoritative."
    )
    # ...and the artifact still renders cmap as an enum.
    plots = next(s for s in build_schema("SHARAD", "EDR")["globals"]
                 if s["key"] == "plot_parameters")
    cmap = next(f for f in plots["fields"] if f["name"] == "cmap")
    assert cmap["type"] == "enum" and cmap["value_type"] == "str"


# --------------------------------------------------------------------------- #
# Byte-identity against GRaSP's exporter (the extraction's safety net)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(_grasp_cli() is None, reason="grasp CLI not on PATH")
def test_byte_identical_to_grasp_export_schema(tmp_path: Path) -> None:
    """The extracted generator must reproduce `grasp export-schema` exactly.

    Compares raw file bytes with only `generated_at` masked. Any divergence in
    ordering, indentation, curation, or choice sets fails here.
    """
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"

    export_schema(ours)
    proc = subprocess.run(
        ["grasp", "export-schema", "--out", str(theirs)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"grasp export-schema failed: {proc.stderr}"

    ours_files = sorted(p.name for p in ours.glob("*.schema.json"))
    theirs_files = sorted(p.name for p in theirs.glob("*.schema.json"))
    assert ours_files == theirs_files, "different set of artifacts emitted"

    for name in theirs_files:
        a = json.loads((ours / name).read_text())
        b = json.loads((theirs / name).read_text())
        assert _mask_generated_at(a) == _mask_generated_at(b), f"{name}: content differs"

        # Byte-level too: same key order, indentation, and trailing newline.
        a_txt = json.dumps(_mask_generated_at(a), indent=2) + "\n"
        b_txt = json.dumps(_mask_generated_at(b), indent=2) + "\n"
        assert a_txt == b_txt, f"{name}: serialised bytes differ"
