"""#270: every harness-originated signal send leaves one attributable log line."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading

import pytest

from issue_agent.adapters.dashweb import build
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.signal_log import send_signal
from issue_agent.app import cli
from issue_agent.domain.contracts import RunId


def _lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line.startswith("signal-send:")]


def _sleepers(supervisor, prefix: str, count: int = 1) -> list[subprocess.Popen]:
    processes = [subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(count)]
    for index, process in enumerate(processes):
        supervisor.register(RunId(f"{prefix}-{index}"), os.getpgid(process.pid))
    return processes


def test_send_signal_logs_then_delivers_to_a_pid(capsys) -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        send_signal(signal.SIGTERM, pid=process.pid, reason="unit", run_id="r-1")
        assert process.wait(timeout=3) == -signal.SIGTERM
    finally:
        process.kill()
    (line,) = _lines(capsys.readouterr().err)
    assert f"SIGTERM pid={process.pid} reason=unit run_id=r-1" in line


@pytest.mark.parametrize("kwargs", [{}, {"pid": 1, "pgid": 1}])
def test_send_signal_rejects_ambiguous_targets_without_logging(capsys, kwargs) -> None:
    with pytest.raises(ValueError):
        send_signal(signal.SIGTERM, reason="x", **kwargs)
    assert _lines(capsys.readouterr().err) == []


def test_send_signal_logs_a_failed_attempt_and_propagates(capsys) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    with pytest.raises(ProcessLookupError):
        send_signal(signal.SIGTERM, pid=gone.pid, reason="late")
    assert len(_lines(capsys.readouterr().err)) == 1


def test_orphan_reclaim_logs_group_signal_with_reason_and_run(tmp_path, capsys) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    (process,) = _sleepers(supervisor, "orphan")

    supervisor.recover_orphans()

    process.wait(timeout=3)
    (line,) = [l for l in _lines(capsys.readouterr().err) if "SIGTERM" in l]
    assert f"pgid={process.pid}" in line and "reason=orphan_reclaim" in line and "run_id=orphan-0" in line


def test_force_kill_all_and_kill_tree_name_their_reasons(tmp_path, capsys) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    (forced,) = _sleepers(supervisor, "forced")
    (ended,) = _sleepers(supervisor, "ended")

    supervisor.kill_tree(RunId("ended-0"))
    supervisor.force_kill_all()

    forced.wait(timeout=3)
    ended.wait(timeout=3)
    text = "\n".join(_lines(capsys.readouterr().err))
    assert "reason=leg_end_cleanup run_id=ended-0" in text
    assert "reason=drain_deadline run_id=forced-0" in text


def test_a_group_that_ignores_sigterm_logs_the_escalation_to_sigkill(tmp_path, capsys) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.1)
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert stubborn.stdout.readline().strip() == b"up"
    supervisor.register(RunId("stubborn"), os.getpgid(stubborn.pid))

    supervisor.kill_tree(RunId("stubborn"))

    stubborn.wait(timeout=3)
    lines = _lines(capsys.readouterr().err)
    assert any("SIGTERM" in l and "reason=leg_end_cleanup" in l for l in lines)
    assert any(
        "SIGKILL" in l and f"pid={stubborn.pid}" in l and "reason=leg_end_cleanup:grace_expired" in l
        for l in lines
    )


def test_operator_stop_logs_each_sigterm_target(monkeypatch, capsys) -> None:
    victim = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=victim.wait, daemon=True).start()  # reap so the stop sees it exit
    monkeypatch.setattr(cli, "_launcher_pids", lambda _n: [victim.pid])
    monkeypatch.setattr(cli, "_role_pids", lambda _n: {})
    try:
        assert cli._stop_deployment("dep") == 0
    finally:
        victim.kill()
    (line,) = _lines(capsys.readouterr().err)
    assert f"SIGTERM pid={victim.pid} reason=operator_stop:dep" in line


def test_dashboard_build_timeout_logs_the_group_kill(tmp_path, capsys) -> None:
    code = build.run_process(["sleep", "30"], tmp_path, timeout=0.2)

    assert code == build.TIMEOUT_EXIT
    (line,) = _lines(capsys.readouterr().err)
    assert "SIGTERM pgid=" in line and "reason=build_timeout" in line


def test_supervised_launcher_logs_forwarded_signal_and_relay(tmp_path) -> None:
    run_id = RunId("fwd-1")
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    command = supervisor.supervised_command(run_id, [sys.executable, "-c", "print('up', flush=True); import time; time.sleep(30)"])
    launcher = subprocess.Popen(
        command,
        start_new_session=True,
        env={**os.environ, **supervisor.launch_guard_env()},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert launcher.stdout.readline().strip() == b"up"

    launcher.send_signal(signal.SIGTERM)
    _out, err = launcher.communicate(timeout=10)

    lines = _lines(err.decode())
    assert any(
        l.startswith("signal-send: SIGTERM pgid=") and "reason=forward_received_signal run_id=fwd-1" in l
        for l in lines
    )
    assert any("pid=" in l and "reason=relay_received_signal run_id=fwd-1" in l for l in lines)
    assert all(re.search(r"sender_pid=\d+", l) for l in lines)
