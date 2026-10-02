"""Full-gate scratch has one bounded lifetime without deleting caller data."""

import os
from pathlib import Path
import subprocess
import signal
import sys
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_test_suite.sh"


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_gate_uses_private_scratch_and_cleans_only_its_own_directory(tmp_path, exit_code, explicit):
    runtime = tmp_path / "runtime"
    chosen = tmp_path / "override" if explicit else runtime
    runtime.mkdir()
    if explicit:
        chosen.mkdir()
    sentinel = chosen / "unrelated"
    sentinel.write_text("retain")
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}
    env.pop("TMPDIR", None)
    if explicit:
        env["TMPDIR"] = str(chosen)
    result = subprocess.run(
        ["bash", str(SCRIPT), "bash", "-c",
         'printf "%s\\n" "$TMPDIR"; test -d "$TMPDIR"; touch "$TMPDIR/evidence"; exit "$1"',
         "test", str(exit_code)],
        env=env, capture_output=True, text=True,
    )
    scratch = Path(result.stdout.strip())
    assert result.returncode == exit_code
    assert scratch.parent == chosen and scratch != chosen
    assert not scratch.exists()
    assert sentinel.read_text() == "retain"


def test_invalid_explicit_scratch_parent_fails_without_running_tests(tmp_path):
    marker = tmp_path / "must-not-run"
    result = subprocess.run(
        ["bash", str(SCRIPT), "touch", str(marker)],
        env={**os.environ, "TMPDIR": str(tmp_path / "missing")},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize("cancel_signal", [signal.SIGINT, signal.SIGTERM])
def test_cancelling_wrapper_stops_suite_before_removing_scratch(tmp_path, cancel_signal):
    marker = tmp_path / "started"
    child = (
        "import os,pathlib,time; "
        "pathlib.Path(os.environ['PROBE_MARKER']).write_text("
        "str(os.getpid())+'\\n'+os.environ['TMPDIR']); time.sleep(30)"
    )
    process = subprocess.Popen(
        ["bash", str(SCRIPT), sys.executable, "-c", child],
        env={**os.environ, "TMPDIR": str(tmp_path), "PROBE_MARKER": str(marker)},
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raw_pid, scratch = marker.read_text().splitlines()
        child_pid = int(raw_pid)
        process.send_signal(cancel_signal)
        assert process.wait(timeout=5) == 128 + cancel_signal
        assert not Path(scratch).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
