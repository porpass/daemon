"""Tests for schema artifact loading, validation, and publishing.

Publishing now generates the artifacts in-process (see
``porpass_daemon.schema.generate``) rather than shelling out to
``grasp export-schema``; these cover the publish wrapper's validation and
failure handling. The generator itself — including byte-identity with GRaSP's
exporter — is covered by ``test_schema_generate.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porpass_daemon import schema
from porpass_daemon.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
ALL = ["SHARAD_EDR", "MARSIS_EDR", "LRS_EDR"]


# --------------------------------------------------------------------------- #
# Loading / validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ALL)
def test_load_and_shape(name: str) -> None:
    art = schema.load_artifact(FIXTURES / f"{name}.schema.json")
    assert art.schema_version == "1.1"
    assert art.instrument == name.split("_")[0]
    assert art.product == "EDR"
    # 7 stages + 4 globals = 11 sections, general present.
    assert len(art.sections()) == 11
    assert art.section("general") is not None
    assert art.section("sar_processing") is not None
    assert art.section("does_not_exist") is None


def test_lrs_has_two_inputs_others_three() -> None:
    lrs = schema.load_artifact(FIXTURES / "LRS_EDR.schema.json")
    sharad = schema.load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    assert set(lrs.inputs) == {"label_file", "science_file"}
    assert "auxiliary_file" in sharad.inputs


def test_reject_wrong_version() -> None:
    with pytest.raises(schema.SchemaError, match="schema_version"):
        schema.parse_artifact({"schema_version": "2.0"}, "x")


def test_reject_missing_keys() -> None:
    with pytest.raises(schema.SchemaError, match="missing required key"):
        schema.parse_artifact({"schema_version": "1.1", "instrument": "SHARAD"}, "x")


def test_reject_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.schema.json"
    bad.write_text("{not json")
    with pytest.raises(schema.SchemaError, match="invalid JSON"):
        schema.load_artifact(bad)


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(schema.SchemaError, match="not found"):
        schema.load_artifact(tmp_path / "nope.schema.json")


# --------------------------------------------------------------------------- #
# Publishing (fake grasp)
# --------------------------------------------------------------------------- #

def _make_config(tmp_path: Path, grasp_bin: str) -> Config:
    return Config(
        db_host="localhost", db_port=3306, db_database="d",
        db_username="u", db_password="p",
        storage_path=tmp_path / "storage",
        grasp_bin=grasp_bin,
        poll_interval=5, heartbeat_interval=30, reaper_stale_seconds=300,
        cancel_poll_interval=5, worker_id="test-1", publish_schemas_on_start=True,
    )


def test_publish_generates_and_validates(tmp_path: Path) -> None:
    """publish_schemas now generates in-process and validates what it wrote."""
    pytest.importorskip("grasp", reason="GRaSP not importable")
    cfg = _make_config(tmp_path, grasp_bin="grasp")

    written = schema.publish_schemas(cfg)

    assert written, "expected at least one artifact"
    for path in written:
        assert path.parent == cfg.schemas_dir
        assert path.exists()
        schema.load_artifact(path)  # each published artifact is valid


def test_publish_does_not_shell_out(tmp_path: Path, monkeypatch) -> None:
    """GRASP_BIN is irrelevant to publishing now — no subprocess is spawned."""
    pytest.importorskip("grasp", reason="GRaSP not importable")

    def _boom(*a, **k):  # pragma: no cover - fails the test if reached
        raise AssertionError("publish_schemas must not spawn a subprocess")

    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)

    # A nonsense GRASP_BIN must not matter: generation is in-process.
    cfg = _make_config(tmp_path, grasp_bin=str(tmp_path / "no_such_grasp"))
    assert schema.publish_schemas(cfg)


def test_publish_wraps_generator_failure(tmp_path: Path, monkeypatch) -> None:
    """A generator blow-up surfaces as SchemaError for the best-effort caller."""
    pytest.importorskip("grasp", reason="GRaSP not importable")

    def _explode(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("porpass_daemon.schema.generate.export_schema", _explode)
    cfg = _make_config(tmp_path, grasp_bin="grasp")
    with pytest.raises(schema.SchemaError, match="schema generation failed"):
        schema.publish_schemas(cfg)
