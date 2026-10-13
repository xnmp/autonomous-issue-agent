"""#270: the operator stop command logs each SIGTERM it sends."""

from __future__ import annotations

import subprocess
import threading

from issue_agent.app import cli


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


def _lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line.startswith("signal-send:")]
