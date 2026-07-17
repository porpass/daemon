"""Smoke tests for the scaffold: config loading, worker identity, fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porpass_daemon import config as config_mod

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


def test_load_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORPASS_STORAGE_PATH", "/tmp/porpass-storage")
    monkeypatch.setenv("DB_DATABASE", "porpass_test")
    monkeypatch.setenv("DAEMON_WORKER_ID", "")
    cfg = config_mod.load_config()

    assert cfg.db_database == "porpass_test"
    assert cfg.storage_path == Path("/tmp/porpass-storage")
    assert cfg.schemas_dir == Path("/tmp/porpass-storage/schemas")
    assert cfg.processing_dir == Path("/tmp/porpass-storage/processing")
    # Derived worker id is {hostname}-{pid}, so it must contain the pid.
    assert cfg.worker_id.endswith(f"-{__import__('os').getpid()}")


def test_missing_storage_path_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORPASS_STORAGE_PATH", raising=False)
    with pytest.raises(RuntimeError, match="PORPASS_STORAGE_PATH"):
        config_mod.load_config()


def test_explicit_worker_id_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORPASS_STORAGE_PATH", "/tmp/porpass-storage")
    monkeypatch.setenv("DAEMON_WORKER_ID", "custom-worker-1")
    assert config_mod.load_config().worker_id == "custom-worker-1"


@pytest.mark.parametrize(
    "name", ["SHARAD_EDR", "MARSIS_EDR", "LRS_EDR"]
)
def test_schema_fixtures_present_and_valid(name: str) -> None:
    doc = json.loads((FIXTURES / f"{name}.schema.json").read_text())
    assert doc["schema_version"] == "1.1"
    assert isinstance(doc["stages"], list) and doc["stages"]
    assert isinstance(doc["globals"], list) and doc["globals"]
    assert isinstance(doc["inputs"], dict) and doc["inputs"]
