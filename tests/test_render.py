"""Tests for job.toml rendering.

Every rendered document is parsed back with tomllib (a strong check that it is
valid TOML), and section headers are checked against the set GRaSP's loader
accepts. The real GRaSP loader is exercised separately as a live check.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from porpass_daemon import render
from porpass_daemon.schema import load_artifact

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"

# The long section names GRaSP's loader accepts (grasp.section_aliases.LONG_TO_ATTR).
VALID_SECTIONS = {
    "general", "input", "preprocessing", "range_compression", "emi_suppression",
    "ionospheric_compensation", "sar_processing", "multilooking",
    "clutter_simulation", "output_parameters", "plot_parameters", "final_output",
}

OUT_DIR = "/Users/Shared/porpass-storage/processing/1/6"
INPUTS = {
    "label_file": "/tmp/job-6/E_0169802_002_SS19_700_A.LBL",
    "science_file": "/tmp/job-6/E_0169802_002_SS19_700_A_S.DAT",
    "auxiliary_file": "/tmp/job-6/E_0169802_002_SS19_700_A_A.DAT",
}
# job 6's real Contract B overrides
JOB6_OVERRIDES = {
    "plot_parameters": {"cmap": "gray"},
    "ionospheric_compensation": {"metric": "PEAK_SNR"},
}


def _render(name: str, overrides, inputs=INPUTS) -> str:
    art = load_artifact(FIXTURES / f"{name}.schema.json")
    return render.render_job_toml(art, overrides, OUT_DIR, inputs)


def test_valid_toml_and_sections() -> None:
    text = _render("SHARAD_EDR", JOB6_OVERRIDES)
    doc = tomllib.loads(text)                       # must be valid TOML
    assert set(doc).issubset(VALID_SECTIONS)        # every header GRaSP-valid
    assert "general" in doc and "input" in doc      # required sections present


def test_injected_general_and_input() -> None:
    doc = tomllib.loads(_render("SHARAD_EDR", JOB6_OVERRIDES))
    assert doc["general"]["out_dir"] == OUT_DIR
    assert doc["general"]["verbose"] is True         # schema default, lowercase bool
    assert doc["input"]["label_file"] == INPUTS["label_file"]
    assert doc["input"]["science_file"] == INPUTS["science_file"]
    assert doc["input"]["auxiliary_file"] == INPUTS["auxiliary_file"]


def test_overrides_applied_field_level() -> None:
    doc = tomllib.loads(_render("SHARAD_EDR", JOB6_OVERRIDES))
    assert doc["plot_parameters"]["cmap"] == "gray"
    assert doc["ionospheric_compensation"]["metric"] == "PEAK_SNR"
    # a sibling default in an overridden section still renders (field-level merge)
    assert len(doc["plot_parameters"]) > 1


def test_int_enum_emitted_unquoted() -> None:
    # sar_processing.sgn is a value_type:int enum (default null); override it.
    text = _render("SHARAD_EDR", {"sar_processing": {"sgn": -1}})
    assert "sgn = -1" in text                        # unquoted in the raw text
    doc = tomllib.loads(text)
    assert doc["sar_processing"]["sgn"] == -1
    assert isinstance(doc["sar_processing"]["sgn"], int)


def test_null_default_fields_omitted() -> None:
    # With no override, ionospheric_compensation.sgn (default null) must not appear.
    doc = tomllib.loads(_render("SHARAD_EDR", JOB6_OVERRIDES))
    assert "sgn" not in doc.get("ionospheric_compensation", {})


def test_unsupported_sections_skipped_marsis() -> None:
    # MARSIS EDR: sar_processing and multilooking are supported:false -> omitted.
    doc = tomllib.loads(_render("MARSIS_EDR", {}))
    assert "sar_processing" not in doc
    assert "multilooking" not in doc


def test_lrs_two_inputs_only() -> None:
    inputs = {k: INPUTS[k] for k in ("label_file", "science_file")}
    doc = tomllib.loads(_render("LRS_EDR", {}, inputs=inputs))
    assert set(doc["input"]) == {"label_file", "science_file"}


def test_deterministic_render() -> None:
    a = _render("SHARAD_EDR", JOB6_OVERRIDES)
    b = _render("SHARAD_EDR", JOB6_OVERRIDES)
    assert a == b                                    # byte-for-byte reproducible


def test_string_escaping_in_out_dir() -> None:
    art = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    weird = '/Users/Shared/porpass storage/"quoted"/1'
    doc = tomllib.loads(render.render_job_toml(art, {}, weird, INPUTS))
    assert doc["general"]["out_dir"] == weird        # round-trips through escaping


def test_floats_have_decimal_point() -> None:
    # Every float-typed field that renders should read back as a float.
    art = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    doc = tomllib.loads(render.render_job_toml(art, {}, OUT_DIR, INPUTS))
    float_fields = {
        (sec["key"], f["name"])
        for sec in art.sections()
        for f in sec["fields"]
        if f.get("type") == "float" and f.get("default") is not None
    }
    for section, name in float_fields:
        assert isinstance(doc[section][name], float), f"{section}.{name} not float"
