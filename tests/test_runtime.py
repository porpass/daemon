"""Tests for the runtime version manifest published to shared storage."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from porpass_daemon import runtime
from porpass_daemon.config import Config


def _make_config(tmp_path: Path) -> Config:
    return Config(
        db_host="localhost", db_port=3306, db_database="d",
        db_username="u", db_password="p",
        storage_path=tmp_path / "storage",
        grasp_bin="grasp",
        poll_interval=5, heartbeat_interval=30, reaper_stale_seconds=300,
        cancel_poll_interval=5, worker_id="test-1", publish_schemas_on_start=False,
    )


def test_publishes_expected_shape(tmp_path: Path) -> None:
    """The manifest lands at {storage}/runtime.json with exactly three fields."""
    cfg = _make_config(tmp_path)

    path = runtime.publish_runtime_manifest(cfg)

    assert path == cfg.storage_path / "runtime.json"
    data = json.loads(path.read_text())
    assert sorted(data) == ["daemon", "grasp", "published_at"]
    assert all(isinstance(v, str) and v for v in data.values())


def test_published_at_is_iso8601_utc(tmp_path: Path) -> None:
    """The web parses this timestamp; it must be ISO-8601 UTC with a Z suffix."""
    cfg = _make_config(tmp_path)

    data = json.loads(runtime.publish_runtime_manifest(cfg).read_text())

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["published_at"])


def test_file_is_world_readable(tmp_path: Path) -> None:
    """The web runs as a different user and reads the file cold on each request."""
    cfg = _make_config(tmp_path)

    path = runtime.publish_runtime_manifest(cfg)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_overwrites_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """Each start replaces the manifest; the temp file never survives."""
    cfg = _make_config(tmp_path)

    runtime.publish_runtime_manifest(cfg)
    runtime.publish_runtime_manifest(cfg)

    assert sorted(p.name for p in cfg.storage_path.iterdir()) == ["runtime.json"]


def test_version_strings_are_verbatim(tmp_path: Path, monkeypatch) -> None:
    """Pre-release suffixes must survive untouched — the web displays them as-is."""
    monkeypatch.setattr(runtime, "DAEMON_VERSION", "0.1.0-alpha.4")
    monkeypatch.setattr(runtime, "_grasp_version", lambda: "1.4.0b2")
    cfg = _make_config(tmp_path)

    data = json.loads(runtime.publish_runtime_manifest(cfg).read_text())

    assert data["daemon"] == "0.1.0-alpha.4"
    assert data["grasp"] == "1.4.0b2"


def test_grasp_version_unknown_when_absent(monkeypatch) -> None:
    """A missing GRaSP degrades to "unknown" rather than blocking startup."""
    import importlib.metadata as md

    def _missing(name: str) -> str:
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(runtime, "_dist_version", _missing)
    monkeypatch.setitem(__import__("sys").modules, "grasp", None)

    assert runtime._grasp_version() == runtime.UNKNOWN_VERSION


def test_partial_write_never_visible(tmp_path: Path, monkeypatch) -> None:
    """A failed write must not clobber a previously published manifest.

    Guards the reason for the temp-file + os.replace dance: the web reads this
    file cold, so a half-written runtime.json would break the footer.
    """
    cfg = _make_config(tmp_path)
    good = json.loads(runtime.publish_runtime_manifest(cfg).read_text())

    def _boom() -> dict[str, str]:
        raise OSError("disk full")

    monkeypatch.setattr(runtime, "build_manifest", _boom)
    with pytest.raises(OSError):
        runtime.publish_runtime_manifest(cfg)

    # the previous manifest is intact and no temp file was left behind
    assert json.loads((cfg.storage_path / "runtime.json").read_text()) == good
    assert sorted(p.name for p in cfg.storage_path.iterdir()) == ["runtime.json"]
