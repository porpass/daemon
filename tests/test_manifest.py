"""Tests for the Contract C manifest writer."""

from __future__ import annotations

import json
from pathlib import Path

from porpass_daemon import manifest

VALID_KINDS = {"data", "image", "segy", "state", "cluttergram", "log", "other"}


def _populate(job_dir: Path) -> None:
    """Create a job dir with the full spread of GRaSP outputs + provenance."""
    base = "s_00169802_edr"
    contents = {
        # provenance/config — must be excluded
        "config.json": b"{}",
        "job.toml": b"[general]\n",
        # log — must be kind:log
        "run.log": b"=== run ===\n",
        # data triplet
        f"{base}.img": b"IMGDATA" * 10,
        f"{base}.csv": b"a,b,c\n1,2,3\n",
        f"{base}.grsp": b"key = value\n",
        # image products
        f"{base}.bmp": b"BM" + b"\x00" * 50,
        f"{base}_browse.png": b"\x89PNG\r\n" + b"\x00" * 20,
        # segy + state
        f"{base}.sgy": b"SEGY" * 100,
        f"iono_campbell.h5": b"\x89HDF\r\n" + b"\x00" * 30,
        # cluttergram (csim token) — native data vs rendered image
        f"{base}_csim.img": b"CLUTTER" * 5,
        f"{base}_csim.bmp": b"BM" + b"\x00" * 40,
        # junk to skip
        ".DS_Store": b"junk",
    }
    for name, data in contents.items():
        (job_dir / name).write_bytes(data)


def test_manifest_shape_and_exclusions(tmp_path: Path) -> None:
    _populate(tmp_path)
    doc = manifest.build_manifest(tmp_path, grasp_version="0.5.1")

    assert doc["manifest_version"] == "1.0"
    assert doc["grasp_version"] == "0.5.1"
    assert "created_at" in doc

    names = {f["path"] for f in doc["files"]}
    # provenance/config + dotfiles excluded
    assert "config.json" not in names
    assert "job.toml" not in names
    assert "manifest.json" not in names
    assert ".DS_Store" not in names
    # run.log included
    assert "run.log" in names


def test_kinds_and_content_types(tmp_path: Path) -> None:
    _populate(tmp_path)
    by_path = {f["path"]: f for f in manifest.build_manifest(tmp_path, "0.5.1")["files"]}

    assert all(f["kind"] in VALID_KINDS for f in by_path.values())
    assert by_path["run.log"]["kind"] == "log"
    assert by_path["s_00169802_edr.img"]["kind"] == "data"
    assert by_path["s_00169802_edr.csv"]["kind"] == "data"
    assert by_path["s_00169802_edr.grsp"]["kind"] == "data"
    assert by_path["s_00169802_edr.sgy"]["kind"] == "segy"
    assert by_path["s_00169802_edr.bmp"]["kind"] == "image"
    assert by_path["s_00169802_edr_browse.png"]["kind"] == "image"
    assert by_path["iono_campbell.h5"]["kind"] == "state"
    # cluttergram: native data -> cluttergram, but rendered image stays image
    assert by_path["s_00169802_edr_csim.img"]["kind"] == "cluttergram"
    assert by_path["s_00169802_edr_csim.bmp"]["kind"] == "image"

    assert by_path["s_00169802_edr.csv"]["content_type"] == "text/csv"
    assert by_path["s_00169802_edr_browse.png"]["content_type"] == "image/png"
    assert by_path["iono_campbell.h5"]["content_type"] == "application/x-hdf5"


def test_bytes_deleted_and_relative_paths(tmp_path: Path) -> None:
    _populate(tmp_path)
    files = manifest.build_manifest(tmp_path, "0.5.1")["files"]
    for f in files:
        assert f["deleted"] is False
        assert not f["path"].startswith("/") and ".." not in f["path"]
        assert f["bytes"] == (tmp_path / f["path"]).stat().st_size


def test_nested_files_get_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "extra.img").write_bytes(b"x" * 8)
    names = {f["path"] for f in manifest.build_manifest(tmp_path, "0.5.1")["files"]}
    assert "sub/extra.img" in names


def test_run_log_only_failure_case(tmp_path: Path) -> None:
    # A failed run may produce only run.log; it must still be manifested as log.
    (tmp_path / "run.log").write_bytes(b"boom\n")
    (tmp_path / "config.json").write_bytes(b"{}")
    (tmp_path / "job.toml").write_bytes(b"")
    files = manifest.build_manifest(tmp_path, "0.5.1")["files"]
    assert len(files) == 1 and files[0]["path"] == "run.log" and files[0]["kind"] == "log"


def test_write_manifest_roundtrips(tmp_path: Path) -> None:
    _populate(tmp_path)
    path = manifest.write_manifest(tmp_path, "0.5.1")
    assert path.name == "manifest.json"
    doc = json.loads(path.read_text())          # valid JSON on disk
    assert doc["manifest_version"] == "1.0"
    # written manifest.json is excluded from its own listing
    assert "manifest.json" not in {f["path"] for f in doc["files"]}
