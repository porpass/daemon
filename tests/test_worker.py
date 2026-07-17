"""End-to-end tests for per-job processing and the completion transitions.

_handle_job is exercised fully offline: a fake DB connection answers the
observation/file lookups and records the terminal status, inputs download from a
local HTTP server, and a fake `grasp` writes a product. The completion SQL guards
are unit-tested separately.
"""

from __future__ import annotations

import shutil
import stat
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from porpass_daemon import db
from porpass_daemon.config import Config
from porpass_daemon.models import Job
from porpass_daemon.worker import WorkerLoop

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
WEB_OWNED = ("output_dir", "results_deleted", "results_deleted_at", "rerun_of")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._result: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=()) -> int:
        self._conn.executed.append((sql, params))
        if "FROM observations" in sql:
            self._result = [self._conn.obs_row] if self._conn.obs_row else []
            return len(self._result)
        if "_files" in sql:
            url = self._conn.file_urls.get(params[1])
            self._result = [{"file_url": url}] if url else []
            return len(self._result)
        if "SELECT cancel_requested" in sql:
            self._result = [{"cancel_requested": 1 if self._conn.cancel_flag else 0}]
            return 1
        if "SET claimed_at = NOW()" in sql:          # heartbeat
            return 1
        if "SET status = 'succeeded'" in sql:
            self._conn.final_status = "succeeded"
            return 1
        if "SET status = 'failed'" in sql:
            self._conn.final_status = "failed"
            self._conn.error_message = params[0]
            return 1
        if "SET status = 'cancelled'" in sql:
            self._conn.final_status = "cancelled"
            self._conn.error_message = params[0]
            return 1
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, obs_row, file_urls, cancel_flag=False):
        self.obs_row = obs_row
        self.file_urls = file_urls
        self.cancel_flag = cancel_flag
        self.executed: list[tuple] = []
        self.final_status: str | None = None
        self.error_message: str | None = None

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def ping(self, reconnect=True):
        pass

    def close(self):
        pass


@pytest.fixture
def http_root(tmp_path: Path):
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


def _fake_grasp(tmp_path: Path, *, exit_code: int = 0, write_product: bool = True) -> Path:
    """A fake `grasp run` that writes a product into cwd (the job dir)."""
    script = tmp_path / "fake_grasp"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os\n"
        f"EXIT = {exit_code}\nWRITE = {write_product}\n"
        "assert sys.argv[1] == 'run' and sys.argv[3] == '-v'\n"
        "print('fake GRaSP: running', flush=True)\n"
        "if WRITE:\n"
        "    open(os.path.join(os.getcwd(), 's_00169802_edr.img'), 'wb').write(b'DATA'*256)\n"
        "    open(os.path.join(os.getcwd(), 's_00169802_edr_browse.png'), 'wb').write(b'PNG'*10)\n"
        "print('fake GRaSP: done', flush=True)\n"
        "sys.exit(EXIT)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _config(tmp_path: Path, grasp_bin: str, cancel_interval: float = 999) -> Config:
    # storage layout: schemas/ holds the artifact the daemon loads
    schemas = tmp_path / "storage" / "schemas"
    schemas.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "SHARAD_EDR.schema.json", schemas / "SHARAD_EDR.schema.json")
    return Config(
        db_host="localhost", db_port=3306, db_database="d", db_username="u",
        db_password="p", storage_path=tmp_path / "storage", grasp_bin=grasp_bin,
        poll_interval=5, heartbeat_interval=999, reaper_stale_seconds=300,
        cancel_poll_interval=cancel_interval, worker_id="test-worker",
        publish_schemas_on_start=False,
    )


def _sleeping_fake_grasp(tmp_path: Path) -> Path:
    """A fake `grasp run` that writes a partial product, then runs 'forever'."""
    script = tmp_path / "fake_grasp"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, time\n"
        "assert sys.argv[1] == 'run'\n"
        "open(os.path.join(os.getcwd(), 's_00169802_edr.img'), 'wb').write(b'PARTIAL'*10)\n"
        "print('fake GRaSP: partial written, running long', flush=True)\n"
        "time.sleep(30)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _job(out_dir: Path) -> Job:
    return Job(
        job_id=6, user_id=1, observation_id=381,
        config={
            "instrument": "SHARAD", "product": "EDR", "grasp_version": "0.5.1",
            "overrides": {"plot_parameters": {"cmap": "gray"}},
        },
        output_dir=str(out_dir),
    )


def _worker(config: Config) -> WorkerLoop:
    return WorkerLoop(config, threading.Event())


# --------------------------------------------------------------------------- #
# End-to-end _handle_job
# --------------------------------------------------------------------------- #

def test_handle_job_success(tmp_path: Path, http_root) -> None:
    base, root = http_root
    for name in ("E.LBL", "E_S.DAT", "E_A.DAT"):
        (root / name).write_bytes(b"x" * 64)
    out_dir = tmp_path / "job6"; out_dir.mkdir()

    cfg = _config(tmp_path, str(_fake_grasp(tmp_path)))
    conn = _FakeConn(
        obs_row={"native_id": "E_0169802", "instrument_abbr": "SHARAD"},
        file_urls={"LBL": f"{base}/E.LBL", "SCI": f"{base}/E_S.DAT", "AUX": f"{base}/E_A.DAT"},
    )

    _worker(cfg)._handle_job(conn, _job(out_dir))

    assert conn.final_status == "succeeded"
    # provenance + products + manifest all present in the job dir
    assert (out_dir / "job.toml").exists()
    assert (out_dir / "run.log").exists()
    import json
    mani = json.loads((out_dir / "manifest.json").read_text())
    names = {f["path"]: f["kind"] for f in mani["files"]}
    assert names["run.log"] == "log"
    assert names["s_00169802_edr.img"] == "data"
    assert names["s_00169802_edr_browse.png"] == "image"
    # inputs were downloaded to a temp dir and cleaned up (not in the job dir)
    assert not (out_dir / "E.LBL").exists()


def test_handle_job_grasp_failure(tmp_path: Path, http_root) -> None:
    base, root = http_root
    for name in ("E.LBL", "E_S.DAT", "E_A.DAT"):
        (root / name).write_bytes(b"x" * 64)
    out_dir = tmp_path / "job6"; out_dir.mkdir()

    cfg = _config(tmp_path, str(_fake_grasp(tmp_path, exit_code=2, write_product=False)))
    conn = _FakeConn(
        obs_row={"native_id": "E", "instrument_abbr": "SHARAD"},
        file_urls={"LBL": f"{base}/E.LBL", "SCI": f"{base}/E_S.DAT", "AUX": f"{base}/E_A.DAT"},
    )

    _worker(cfg)._handle_job(conn, _job(out_dir))

    assert conn.final_status == "failed"
    assert "exited 2" in conn.error_message
    assert (out_dir / "run.log").exists()          # log preserved for the web
    assert (out_dir / "manifest.json").exists()


def test_handle_job_input_failure(tmp_path: Path) -> None:
    out_dir = tmp_path / "job6"; out_dir.mkdir()
    cfg = _config(tmp_path, str(_fake_grasp(tmp_path)))
    conn = _FakeConn(
        obs_row={"native_id": "E", "instrument_abbr": "SHARAD"},
        file_urls={},                               # no files -> resolution fails
    )

    _worker(cfg)._handle_job(conn, _job(out_dir))

    assert conn.final_status == "failed"
    assert "no SCI file" in conn.error_message or "no LBL file" in conn.error_message
    # a run.log documenting the pre-GRaSP failure is still written
    assert (out_dir / "run.log").exists()


def test_handle_job_no_output_dir(tmp_path: Path) -> None:
    cfg = _config(tmp_path, str(_fake_grasp(tmp_path)))
    conn = _FakeConn(obs_row=None, file_urls={})
    job = Job(job_id=6, user_id=1, observation_id=381,
              config={"instrument": "SHARAD", "product": "EDR"}, output_dir=None)
    _worker(cfg)._handle_job(conn, job)
    assert conn.final_status == "failed"
    assert "output_dir" in conn.error_message


def test_handle_job_cancelled(tmp_path: Path, http_root, monkeypatch) -> None:
    base, root = http_root
    for name in ("E.LBL", "E_S.DAT", "E_A.DAT"):
        (root / name).write_bytes(b"x" * 64)
    out_dir = tmp_path / "job6"; out_dir.mkdir()

    cfg = _config(tmp_path, str(_sleeping_fake_grasp(tmp_path)), cancel_interval=0.2)
    conn = _FakeConn(
        obs_row={"native_id": "E", "instrument_abbr": "SHARAD"},
        file_urls={"LBL": f"{base}/E.LBL", "SCI": f"{base}/E_S.DAT", "AUX": f"{base}/E_A.DAT"},
    )
    # the Heartbeat/CancelWatcher threads open their own connections — give them
    # a fake that reports the job as cancel-requested.
    monkeypatch.setattr(
        "porpass_daemon.db.connect",
        lambda config: _FakeConn(obs_row=None, file_urls={}, cancel_flag=True),
    )

    _worker(cfg)._handle_job(conn, _job(out_dir))

    assert conn.final_status == "cancelled"
    assert "cancel" in conn.error_message.lower()
    # partial product discarded; run.log + manifest kept
    assert not (out_dir / "s_00169802_edr.img").exists()
    import json
    mani = json.loads((out_dir / "manifest.json").read_text())
    assert {f["path"] for f in mani["files"]} == {"run.log"}


def test_finalize_cancel_deletes_partials(tmp_path: Path) -> None:
    out_dir = tmp_path / "job"; out_dir.mkdir()
    for name in ("config.json", "job.toml", "run.log", "s.img", "s_browse.png"):
        (out_dir / name).write_bytes(b"x" * 8)
    cfg = _config(tmp_path, "grasp")
    conn = _FakeConn(obs_row=None, file_urls={})

    _worker(cfg)._finalize_cancel(conn, _job(out_dir), str(out_dir), "0.5.1")

    assert conn.final_status == "cancelled"
    assert not (out_dir / "s.img").exists()
    assert not (out_dir / "s_browse.png").exists()
    assert (out_dir / "run.log").exists() and (out_dir / "job.toml").exists()
    import json
    mani = json.loads((out_dir / "manifest.json").read_text())
    assert {f["path"] for f in mani["files"]} == {"run.log"}


# --------------------------------------------------------------------------- #
# Completion SQL guards
# --------------------------------------------------------------------------- #

class _RecordingConn:
    def __init__(self, affected=1):
        self.affected = affected
        self.executed: list[tuple] = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *e): return False
            def execute(self, sql, params=()):
                conn.executed.append((sql, params)); return conn.affected
        return _Cur()

    def commit(self):
        pass


def test_mark_succeeded_guarded() -> None:
    conn = _RecordingConn()
    assert db.mark_succeeded(conn, 6, "w1") == 1
    sql, params = conn.executed[-1]
    assert "status = 'succeeded'" in sql
    assert "claimed_by = %s" in sql and "status = 'running'" in sql
    assert "claimed_at = NULL" in sql
    assert params == (6, "w1")
    for col in WEB_OWNED:
        assert col not in sql


def test_mark_failed_guarded_and_message() -> None:
    conn = _RecordingConn()
    assert db.mark_failed(conn, 6, "w1", "boom") == 1
    sql, params = conn.executed[-1]
    assert "status = 'failed'" in sql and "error_message = %s" in sql
    assert params == ("boom", 6, "w1")
    for col in WEB_OWNED:
        assert col not in sql


def test_mark_zero_when_not_owned() -> None:
    conn = _RecordingConn(affected=0)
    assert db.mark_succeeded(conn, 6, "w1") == 0


def test_mark_cancelled_guarded() -> None:
    conn = _RecordingConn()
    assert db.mark_cancelled(conn, 6, "w1") == 1
    sql, params = conn.executed[-1]
    assert "status = 'cancelled'" in sql
    assert "claimed_by = %s" in sql and "status = 'running'" in sql
    assert "claimed_at = NULL" in sql
    assert params[1:] == (6, "w1")               # (error_message, job_id, worker_id)
    for col in WEB_OWNED:
        assert col not in sql


def test_is_cancel_requested_reads_flag() -> None:
    assert db.is_cancel_requested(
        _FakeConn(obs_row=None, file_urls={}, cancel_flag=True), 6, "w1") is True
    assert db.is_cancel_requested(
        _FakeConn(obs_row=None, file_urls={}, cancel_flag=False), 6, "w1") is False
