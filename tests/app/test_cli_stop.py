"""Operator stops cancel supervisor restarts and verify process exit (ADR 0005)."""

import signal
import subprocess

import pytest

from issue_agent.app import cli


def _install_units(home, platform, roles):
    if platform == "darwin":
        directory = home / "Library" / "LaunchAgents"
        names = [f"dev.issue-agent.{role}-watcher.myapp.plist" for role in roles]
    else:
        directory = home / ".config" / "systemd" / "user"
        names = [f"issue-agent-{role}-watcher-myapp.service" for role in roles]
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / name for name in names]
    for path in paths:
        path.touch()
    return paths


@pytest.mark.parametrize("platform", ("linux", "darwin"))
@pytest.mark.parametrize("installed", (("fix", "review"), ("fix",), ()))
def test_stop_uses_supervisor_for_installed_roles_and_signals_foreground(
    tmp_path, monkeypatch, platform, installed,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", platform)
    paths = _install_units(tmp_path, platform, installed)
    # Other deployments must remain untouched.
    _install_units(tmp_path, platform, ("other",))
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [103])
    roles = {"fix": [101], "review": [102]}
    monkeypatch.setattr(cli, "_role_pids", lambda _: dict(roles))
    events = []

    def run(command, **kwargs):
        events.append(command)
        if command[:3] == ["systemctl", "--user", "stop"]:
            for role in installed:
                roles.pop(role)
        elif command[:2] == ["launchctl", "unload"]:
            roles.pop("fix" if ".fix-watcher." in command[2] else "review")
        return subprocess.CompletedProcess(command, 0)

    def kill(pid, signum):
        events.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.os, "kill", kill)
    assert cli._stop_deployment("myapp") == 0

    commands = ([command for path in paths for command in (
        ["launchctl", "list", path.stem], ["launchctl", "unload", str(path)],
    )] if platform == "darwin" else
        [["systemctl", "--user", "stop", *(path.name for path in paths)]] if paths else [])
    foreground = [pid for role, pid in (("fix", 101), ("review", 102)) if role not in installed] + [103]
    assert events[:-3] == commands + [(pid, signal.SIGTERM) for pid in foreground]
    assert sorted(events[-3:]) == [(pid, 0) for pid in (101, 102, 103)]
    assert all(path.exists() for path in paths), "stop must preserve service definitions"


@pytest.mark.parametrize("platform", ("linux", "darwin"))
def test_stop_signals_foreground_role_when_service_definitions_remain(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", platform)
    _install_units(tmp_path, platform, ("fix", "review"))
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [])
    monkeypatch.setattr(cli, "_role_pids", lambda _: {"fix": [101]})
    commands, signals = [], []

    def run(command, **kwargs):
        commands.append(command)
        # launchd definitions remain after unload; no loaded jobs to unload now.
        return subprocess.CompletedProcess(command, 1 if command[0] == "launchctl" else 0)

    def kill(pid, signum):
        assert commands, "supervisor must stop before foreground signals"
        signals.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.os, "kill", kill)
    assert cli._stop_deployment("myapp") == 0
    assert signals == [(101, signal.SIGTERM), (101, 0)]


@pytest.mark.parametrize("platform", ("linux", "darwin"))
def test_stop_cancels_installed_services_even_without_visible_pids(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", platform)
    paths = _install_units(tmp_path, platform, ("fix", "review"))
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [])
    monkeypatch.setattr(cli, "_role_pids", lambda _: {})
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._stop_deployment("myapp") == 0
    if platform == "darwin":
        assert commands == [command for path in paths for command in (
            ["launchctl", "list", path.stem], ["launchctl", "unload", str(path)],
        )]
    else:
        assert commands == [["systemctl", "--user", "stop", *(path.name for path in paths)]]


def test_stop_already_unloaded_launchd_services_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    paths = _install_units(tmp_path, "darwin", ("fix", "review"))
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [])
    monkeypatch.setattr(cli, "_role_pids", lambda _: {})
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._stop_deployment("myapp") == 0
    assert commands == [["launchctl", "list", path.stem] for path in paths]


@pytest.mark.parametrize("platform", ("linux", "darwin"))
@pytest.mark.parametrize("failure", (
    OSError("supervisor unavailable"),
    subprocess.CalledProcessError(1, "stop"),
    subprocess.TimeoutExpired("stop", 90),
))
def test_stop_reports_supervisor_failure_without_direct_signal(tmp_path, monkeypatch, platform, failure, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", platform)
    _install_units(tmp_path, platform, ("fix", "review"))
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [])
    monkeypatch.setattr(cli, "_role_pids", lambda _: {"fix": [101]})

    def run(command, **kwargs):
        if command[:2] == ["launchctl", "list"]:
            return subprocess.CompletedProcess(command, 0)
        raise failure

    def kill(*args):
        pytest.fail("failed supervisor stop must not fall back to direct signals")

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.os, "kill", kill)
    assert cli._stop_deployment("myapp") == 1
    assert "failed to stop services" in capsys.readouterr().err


@pytest.mark.parametrize("installed", (False, True))
def test_stop_fails_if_process_survives_shutdown(tmp_path, monkeypatch, installed, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", "linux")
    _install_units(tmp_path, "linux", ("fix",) if installed else ())
    monkeypatch.setattr(cli, "_launcher_pids", lambda _: [])
    observations = iter(({"fix": [101]}, {}))
    monkeypatch.setattr(cli, "_role_pids", lambda _: next(observations))
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0))
    signals = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, signum: signals.append((pid, signum)))
    times = iter((0, 0, 91))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    assert cli._stop_deployment("myapp") == 1
    assert signals == ([] if installed else [(101, signal.SIGTERM)]) + [(101, 0)]
    assert "still alive: 101" in capsys.readouterr().err
