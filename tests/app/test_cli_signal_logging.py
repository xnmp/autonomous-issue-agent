"""#270: the operator stop command logs each SIGTERM it sends."""

from __future__ import annotations

import signal

from issue_agent.app import cli


def test_operator_stop_logs_each_sigterm_target(monkeypatch, capfd) -> None:
    calls = []

    def kill(pid, signum):
        calls.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(cli.os, "kill", kill)
    monkeypatch.setattr(cli, "_launcher_pids", lambda _n: [123])
    monkeypatch.setattr(cli, "_role_pids", lambda _n: {})
    assert cli._stop_deployment("dep") == 0
    assert calls == [(123, signal.SIGTERM), (123, 0)]
    (line,) = _lines(capfd.readouterr().err)
    assert "SIGTERM pid=123 reason=operator_stop:dep" in line


def _lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line.startswith("signal-send:")]
