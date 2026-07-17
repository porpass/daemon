"""Tests for the GRaSP runner, driven by a fake `grasp` executable."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

import pytest

from porpass_daemon import grasp_runner
from porpass_daemon.grasp_runner import GraspError


def _write_fake_grasp(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """A fake `grasp` that emulates `run <toml> -v`, writing to stdout+stderr."""
    script = tmp_path / "fake_grasp"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"EXIT = {exit_code}\n"
        "assert sys.argv[1] == 'run' and sys.argv[3] == '-v'\n"
        "print('GRaSP banner', flush=True)\n"
        "print('warning: something', file=sys.stderr, flush=True)\n"
        "print('processing done', flush=True)\n"
        "sys.exit(EXIT)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _job_toml(tmp_path: Path) -> Path:
    p = tmp_path / "job.toml"
    p.write_text("[general]\nout_dir = \".\"\n[input]\nlabel_file=\"x\"\nscience_file=\"y\"\n")
    return p


def test_run_success(tmp_path: Path) -> None:
    grasp = _write_fake_grasp(tmp_path)
    run_log = tmp_path / "run.log"
    res = grasp_runner.run_grasp(str(grasp), _job_toml(tmp_path), run_log)

    assert res.ok and res.returncode == 0
    text = run_log.read_text()
    assert "GRaSP banner" in text
    assert "processing done" in text
    assert "warning: something" in text          # stderr merged into run.log
    assert "=== porpass-daemon GRaSP run ===" in text
    assert "=== GRaSP exited 0 at" in text


def test_run_nonzero_exit(tmp_path: Path) -> None:
    grasp = _write_fake_grasp(tmp_path, exit_code=3)
    run_log = tmp_path / "run.log"
    res = grasp_runner.run_grasp(str(grasp), _job_toml(tmp_path), run_log)

    assert not res.ok and res.returncode == 3
    assert "=== GRaSP exited 3 at" in run_log.read_text()


def test_run_grasp_missing_raises_and_logs(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    with pytest.raises(GraspError, match="not found"):
        grasp_runner.run_grasp(str(tmp_path / "no_such_grasp"), _job_toml(tmp_path), run_log)
    # run.log still records the failure
    assert "ERROR" in run_log.read_text()


def test_run_command_shape(tmp_path: Path) -> None:
    grasp = _write_fake_grasp(tmp_path)
    run_log = tmp_path / "run.log"
    job = _job_toml(tmp_path)
    grasp_runner.run_grasp(str(grasp), job, run_log)
    # the fake asserts argv is ['run', <toml>, '-v'] — reaching here proves shape
    assert f"run {job} -v" in run_log.read_text()


def _write_sleeping_grasp(tmp_path: Path) -> Path:
    script = tmp_path / "fake_grasp"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "print('running long', flush=True)\n"
        "time.sleep(60)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_run_cancelled_terminates_grasp(tmp_path: Path) -> None:
    grasp = _write_sleeping_grasp(tmp_path)
    run_log = tmp_path / "run.log"
    cancel = threading.Event()
    result: dict = {}

    def go() -> None:
        result["r"] = grasp_runner.run_grasp(
            str(grasp), _job_toml(tmp_path), run_log, cancel_event=cancel
        )

    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.5)          # let the fake GRaSP start
    cancel.set()             # request cancel
    t.join(timeout=20)

    assert not t.is_alive(), "run_grasp did not return after cancel"
    res = result["r"]
    assert res.cancelled is True
    assert res.ok is False
    assert "cancelled" in run_log.read_text()
