"""Tests for input resolution and archive fetching.

Downloads run against a real local HTTP server (no network, no mocking of
requests). DB resolution runs against a small fake connection so the whole
orchestration is exercised offline.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from porpass_daemon import inputs
from porpass_daemon.inputs import InputResolutionError, ResolvedInputs
from porpass_daemon.models import Job
from porpass_daemon.schema import load_artifact

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_file_type_for_role() -> None:
    assert inputs.file_type_for_role("label_file") == "LBL"
    assert inputs.file_type_for_role("science_file") == "SCI"
    assert inputs.file_type_for_role("auxiliary_file") == "AUX"
    with pytest.raises(InputResolutionError, match="file_type mapping"):
        inputs.file_type_for_role("mystery_file")


def test_files_table_for_instrument() -> None:
    assert inputs.files_table_for_instrument("SHARAD") == "sharad_files"
    assert inputs.files_table_for_instrument("lrs") == "lrs_files"  # case-insensitive
    with pytest.raises(InputResolutionError, match="files table"):
        inputs.files_table_for_instrument("VOYAGER")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pds.example.org/data/s_001.lbl", "s_001.lbl"),
        ("https://pds.example.org/data/s_001.dat?ver=2", "s_001.dat"),
        ("https://darts.example.jp/a/b/LRS%5FSCI.zip", "LRS_SCI.zip"),
    ],
)
def test_local_filename_for_url(url: str, expected: str) -> None:
    assert inputs.local_filename_for_url(url) == expected


def test_local_filename_rejects_empty() -> None:
    with pytest.raises(InputResolutionError, match="derive a filename"):
        inputs.local_filename_for_url("https://example.org/")


# --------------------------------------------------------------------------- #
# Local HTTP server for real downloads
# --------------------------------------------------------------------------- #

@pytest.fixture
def http_root(tmp_path: Path):
    """Serve a temp dir over HTTP; yields (base_url, root_path)."""
    root = tmp_path / "archive"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root
    finally:
        server.shutdown()
        server.server_close()


def test_download_file_happy(tmp_path: Path, http_root) -> None:
    base, root = http_root
    (root / "obs.dat").write_bytes(b"RADARDATA" * 1000)
    dest = tmp_path / "dl"
    dest.mkdir()

    with inputs.build_session() as session:
        out = inputs.download_file(f"{base}/obs.dat", dest, session)

    assert out == dest / "obs.dat"          # original filename preserved
    assert out.read_bytes() == b"RADARDATA" * 1000


def test_download_file_404(tmp_path: Path, http_root) -> None:
    base, _ = http_root
    dest = tmp_path / "dl"
    dest.mkdir()
    with inputs.build_session() as session:
        with pytest.raises(InputResolutionError, match="HTTP 404"):
            inputs.download_file(f"{base}/missing.dat", dest, session)


# --------------------------------------------------------------------------- #
# Local files (copy instead of download)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "ref,remote",
    [
        ("https://pds.example.org/x.lbl", True),
        ("http://darts.example.jp/x.lbl", True),
        ("/Volumes/data/SELENE/lrs/.../lrs_sa_wf_05n_044119e.lbl", False),
        ("relative/path/x.lbl", False),
        ("HTTPS://UP.example/x", True),          # scheme is case-insensitive
    ],
)
def test_is_remote_ref(ref: str, remote: bool) -> None:
    assert inputs.is_remote_ref(ref) is remote


def test_copy_local_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "lrs_sa_wf_05n_044119e.lbl"
    src.parent.mkdir()
    src.write_bytes(b"PDS_VERSION_ID = PDS3\n")
    dest = tmp_path / "dest"; dest.mkdir()

    out = inputs.copy_local_file(str(src), dest)

    assert out == dest / "lrs_sa_wf_05n_044119e.lbl"    # filename preserved
    assert out.read_bytes() == src.read_bytes()


def test_copy_local_file_missing(tmp_path: Path) -> None:
    dest = tmp_path / "dest"; dest.mkdir()
    with pytest.raises(InputResolutionError, match="local input file not found"):
        inputs.copy_local_file(str(tmp_path / "nope.lbl"), dest)


def test_fetch_input_dispatches(tmp_path: Path, http_root) -> None:
    base, root = http_root
    (root / "remote.dat").write_bytes(b"REMOTE")
    local = tmp_path / "local.lbl"; local.write_bytes(b"LOCAL")
    dest = tmp_path / "dest"; dest.mkdir()

    with inputs.build_session() as session:
        r = inputs.fetch_input(f"{base}/remote.dat", dest, session)
        l = inputs.fetch_input(str(local), dest, session)

    assert r.read_bytes() == b"REMOTE"                  # URL -> downloaded
    assert l.read_bytes() == b"LOCAL"                   # path -> copied


# --------------------------------------------------------------------------- #
# Fake DB connection
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, obs_row, file_urls: dict[str, str]):
        self._obs_row = obs_row
        self._file_urls = file_urls
        self._result: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=()):
        if "FROM observations" in sql:
            self._result = [self._obs_row] if self._obs_row else []
        elif "_files" in sql:
            file_type = params[1]
            url = self._file_urls.get(file_type)
            # a list allows testing 0 / 1 / many rows per file_type
            self._result = [{"file_url": u} for u in ([url] if url else [])]
        else:  # pragma: no cover - unexpected query
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, obs_row, file_urls):
        self._obs_row = obs_row
        self._file_urls = file_urls

    def cursor(self):
        return _FakeCursor(self._obs_row, self._file_urls)


def _job(observation_id: int = 42) -> Job:
    return Job(job_id=7, user_id=1, observation_id=observation_id,
               config={}, output_dir=None)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def test_resolve_inputs_end_to_end(tmp_path: Path, http_root) -> None:
    base, root = http_root
    for name in ("s_001.lbl", "s_001.dat", "s_001.aux"):
        (root / name).write_bytes(b"x" * 128)

    artifact = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    conn = _FakeConn(
        obs_row={"native_id": "S_00123401", "instrument_abbr": "SHARAD"},
        file_urls={
            "LBL": f"{base}/s_001.lbl",
            "SCI": f"{base}/s_001.dat",
            "AUX": f"{base}/s_001.aux",
        },
    )

    resolved = inputs.resolve_inputs(conn, _job(), artifact, dest_parent=tmp_path)
    try:
        assert set(resolved.files) == {"label_file", "science_file", "auxiliary_file"}
        assert resolved.files["science_file"].name == "s_001.dat"
        assert all(p.exists() for p in resolved.files.values())
    finally:
        resolved.cleanup()
    assert not resolved.temp_dir.exists()


def test_resolve_inputs_local_lrs_files(tmp_path: Path) -> None:
    # LRS EDR is staged on a local disk: file_urls are absolute paths, not URLs.
    staged = tmp_path / "SELENE" / "lrs"; staged.mkdir(parents=True)
    lbl = staged / "lrs_sa_wf_05n_044119e.lbl"; lbl.write_bytes(b"PDS3 label\n")
    sci = staged / "lrs_sa_wf_05n_044119e.sci"; sci.write_bytes(b"science" * 100)

    artifact = load_artifact(FIXTURES / "LRS_EDR.schema.json")
    conn = _FakeConn(
        obs_row={"native_id": "LRS_1", "instrument_abbr": "LRS"},
        file_urls={"LBL": str(lbl), "SCI": str(sci)},   # local paths
    )

    resolved = inputs.resolve_inputs(conn, _job(), artifact, dest_parent=tmp_path)
    try:
        assert set(resolved.files) == {"label_file", "science_file"}
        # copied into the temp dir under their original names
        assert resolved.files["label_file"].parent == resolved.temp_dir
        assert resolved.files["label_file"].name == "lrs_sa_wf_05n_044119e.lbl"
        assert resolved.files["science_file"].read_bytes() == sci.read_bytes()
    finally:
        resolved.cleanup()


def test_resolve_inputs_instrument_mismatch(tmp_path: Path) -> None:
    artifact = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    conn = _FakeConn(
        obs_row={"native_id": "L_1", "instrument_abbr": "LRS"},  # != SHARAD
        file_urls={},
    )
    with pytest.raises(InputResolutionError, match="does not match"):
        inputs.resolve_inputs(conn, _job(), artifact, dest_parent=tmp_path)
    # no temp dir left behind
    assert list(tmp_path.iterdir()) == []


def test_resolve_inputs_missing_required_cleans_up(tmp_path: Path, http_root) -> None:
    base, root = http_root
    (root / "s_001.lbl").write_bytes(b"x" * 10)

    artifact = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    conn = _FakeConn(
        obs_row={"native_id": "S_1", "instrument_abbr": "SHARAD"},
        file_urls={"LBL": f"{base}/s_001.lbl"},  # SCI + AUX missing -> required error
    )
    with pytest.raises(InputResolutionError, match="no SCI file"):
        inputs.resolve_inputs(conn, _job(), artifact, dest_parent=tmp_path)
    # the job's temp dir is removed on the error path (the http_root fixture's
    # own 'archive' dir also lives under tmp_path, so filter to job dirs)
    assert [p for p in tmp_path.iterdir() if p.name.startswith("porpass-job-")] == []


def test_resolve_inputs_observation_not_found(tmp_path: Path) -> None:
    artifact = load_artifact(FIXTURES / "SHARAD_EDR.schema.json")
    conn = _FakeConn(obs_row=None, file_urls={})
    with pytest.raises(InputResolutionError, match="not found"):
        inputs.resolve_inputs(conn, _job(), artifact, dest_parent=tmp_path)
