from __future__ import annotations

import contextlib
import os
import plistlib
import signal
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _environment(
    tmp_path: Path, *, systemd: bool, fail_pattern: str = ""
) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    target = tmp_path / "target-repo"
    config = tmp_path / "agent.env"
    home.mkdir()
    fake_bin.mkdir()
    target.mkdir()
    config.write_text(f'TARGET_DIR="{target}"\n')
    _executable(fake_bin / "uv", "exit 0")
    if systemd:
        _executable(fake_bin / "systemd-analyze", 'exit "${VERIFY_STATUS:-0}"')
        _executable(
            fake_bin / "systemctl",
            """
[ "${1:-}" != "--user" ] || shift
printf '%s\n' "$*" >> "$SERVICE_LOG"
if [ "${1:-}" = "disable" ] && [ -n "${FAIL_UNINSTALL_PATTERN:-}" ]; then
  case "$*" in
    *"$FAIL_UNINSTALL_PATTERN"*) exit 9 ;;
  esac
fi
if [ "${1:-}" = "stop" ] \
  && [ "${2:-}" != "issue-agent-dashboard.service" ] \
  && [ "${REQUIRE_OLD_SNAPSHOT_DURING_STOP:-0}" = "1" ]; then
  grep -q 'KNOWN_GOOD=1' "$CUTOVER_SNAPSHOT" || exit 9
fi
if [ "${1:-}" = "is-active" ] || [ "${1:-}" = "is-enabled" ]; then
  selector="ACTIVE_UNITS"
  [ "${1:-}" != "is-enabled" ] || selector="ENABLED_UNITS"
  eval "selected=\\${$selector:-}"
  case " $selected " in
    *" ${3:-${2:-}} "*) exit 0 ;;
  esac
  [ -f "$HOME/.config/systemd/user/${3:-${2:-}}" ]
  exit $?
fi
if [ "${1:-}" = "show" ]; then
  case "$*" in
    *"property=ActiveState"*) printf '%s\n' "${ACTIVE_STATE:-active}" ;;
    *"property=SubState"*) printf '%s\n' "${SUB_STATE:-running}" ;;
    *"property=MainPID"*) printf '%s\n' "${MAIN_PID:-0}" ;;
  esac
  exit 0
fi
if [ "${1:-}" = "restart" ] && [ -n "${FAIL_PATTERN:-}" ]; then
  case "${2:-}" in
    *"$FAIL_PATTERN"*)
      if [ ! -e "$FAIL_ONCE_FILE" ]; then
        touch "$FAIL_ONCE_FILE"
        exit 1
      fi
      ;;
  esac
fi
exit 0
""".strip(),
        )
    else:
        _executable(fake_bin / "plutil", 'exit "${VERIFY_STATUS:-0}"')
        _executable(
            fake_bin / "launchctl",
            """
printf '%s\n' "$*" >> "$SERVICE_LOG"
if [ "${1:-}" = "unload" ] && [ -n "${FAIL_UNINSTALL_PATTERN:-}" ]; then
  case "${2:-}" in
    *"$FAIL_UNINSTALL_PATTERN"*) exit 9 ;;
  esac
fi
if [ "${1:-}" = "unload" ] \
  && [ "${REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD:-0}" = "1" ]; then
  grep -q 'KNOWN_GOOD=1' "$CUTOVER_SNAPSHOT" || exit 9
fi
if [ "${1:-}" = "list" ]; then
  case " ${LOADED_LABELS:-} " in
    *" ${2:-} "*) exit 0 ;;
  esac
  [ -f "$HOME/Library/LaunchAgents/${2:-}.plist" ]
  exit $?
fi
if [ "${1:-}" = "load" ] && [ -n "${FAIL_PATTERN:-}" ]; then
  case "${2:-}" in
    *"$FAIL_PATTERN"*)
      if [ ! -e "$FAIL_ONCE_FILE" ]; then
        touch "$FAIL_ONCE_FILE"
        exit 1
      fi
      ;;
  esac
fi
exit 0
""".strip(),
        )
    env = {
        **os.environ,
        "HOME": str(home),
        "AGENT_ENV": str(config),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAIL_PATTERN": fail_pattern,
        "FAIL_UNINSTALL_PATTERN": "",
        "FAIL_ONCE_FILE": str(tmp_path / "failed-once"),
        "SERVICE_LOG": str(tmp_path / "service.log"),
        "LOADED_LABELS": "",
        "ACTIVE_UNITS": "",
        "ENABLED_UNITS": "",
        "ACTIVE_STATE": "active",
        "SUB_STATE": "running",
        "REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD": "0",
        "REQUIRE_OLD_SNAPSHOT_DURING_STOP": "0",
        "CUTOVER_SNAPSHOT": "",
        "ISSUE_AGENT_DASHBOARD_PORT_CHECK": "",
    }
    return env, home, fake_bin / "uv"


@pytest.mark.parametrize("systemd", (True, False))
def test_service_installers_reject_noncanonical_deployment_identity(
    tmp_path,
    systemd,
):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "Acme App"
    script = "install-systemd.sh" if systemd else "install-launchd.sh"

    result = subprocess.run(
        ["bash", str(ROOT / "bin" / script)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "non-canonical deployment identity" in result.stderr
    assert not tuple(
        (
            home / ".config/systemd/user"
            if systemd
            else home / "Library/LaunchAgents"
        ).glob("*")
    )


@pytest.mark.parametrize("systemd", (True, False))
def test_service_installers_refuse_an_existing_identity_alias(
    tmp_path,
    systemd,
):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    if systemd:
        artifact = (
            home
            / ".config/systemd/user"
            / "issue-agent-fix-watcher-acme-app.service"
        )
        artifact.parent.mkdir(parents=True)
        artifact.write_text(
            "Environment=ISSUE_AGENT_DEPLOYMENT_ID=Acme App\n"
        )
        script = "install-systemd.sh"
    else:
        artifact = (
            home
            / "Library/LaunchAgents"
            / "dev.issue-agent.fix-watcher.123456.plist"
        )
        artifact.parent.mkdir(parents=True)
        artifact.write_text(
            "<key>ISSUE_AGENT_DEPLOYMENT_ID</key>"
            "<string>Acme App</string>\n"
        )
        script = "install-launchd.sh"

    result = subprocess.run(
        ["bash", str(ROOT / "bin" / script)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "deployment identity alias" in result.stderr
    assert artifact.exists()


@pytest.mark.parametrize("systemd", (True, False))
def test_service_uninstall_preserves_every_definition_when_stop_fails(
    tmp_path,
    systemd,
):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    config = Path(env["AGENT_ENV"])
    target = tmp_path / "target-repo"
    config.write_text(f'REPO="acme/app"\nTARGET_DIR="{target}"\n')
    script = "install-systemd.sh" if systemd else "install-launchd.sh"
    install = subprocess.run(
        ["bash", str(ROOT / "bin" / script)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    artifact_dir = (
        home / ".config/systemd/user"
        if systemd
        else home / "Library/LaunchAgents"
    )
    artifacts = tuple(artifact_dir.glob("*"))
    assert len(artifacts) == (3 if systemd else 2)

    env["FAIL_UNINSTALL_PATTERN"] = "review-watcher"
    uninstall = subprocess.run(
        [
            "bash",
            str(ROOT / "bin" / script),
            "uninstall",
            "acme-app",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert uninstall.returncode != 0
    assert "REFUSING uninstall" in uninstall.stderr
    assert all(path.exists() for path in artifacts)
    assert "removed 3" not in uninstall.stdout


def test_systemd_installer_renders_validated_units_with_absolute_uv(tmp_path):
    env, home, uv = _environment(tmp_path, systemd=True)

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    units = sorted((home / ".config/systemd/user").glob("*.service"))
    assert len(units) == 3
    assert all(f"ExecStart={uv} " in unit.read_text() for unit in units)
    role_units = tuple(
        unit for unit in units
        if unit.name != "issue-agent-dashboard.service"
    )
    assert all("NotifyAccess=all" in unit.read_text() for unit in role_units)
    assert all("WatchdogSec=300" in unit.read_text() for unit in role_units)
    assert all("KillMode=mixed" in unit.read_text() for unit in role_units)
    assert all("TimeoutStopSec=90" in unit.read_text() for unit in role_units)
    assert all("FinalKillSignal=SIGKILL" in unit.read_text() for unit in role_units)
    terminal_hook = ROOT / "bin/service-terminal-evidence.sh"
    assert terminal_hook.is_file()
    assert all(
        f'ExecStopPost=-/bin/sh "{terminal_hook}"\n' in unit.read_text()
        for unit in role_units
    )
    assert all(
        "Environment=ISSUE_AGENT_DEPLOYMENT_ID=target-repo"
        in unit.read_text()
        for unit in role_units
    )
    dashboard = home / ".config/systemd/user/issue-agent-dashboard.service"
    assert "Environment=AGENT_ENV=" not in dashboard.read_text()
    assert "Environment=TARGET_DIR=" not in dashboard.read_text()
    assert result.stdout.count("active (running)") == 3


def test_systemd_dashboard_install_is_singleton_and_latest_install_wins(tmp_path):
    env, home, _first_uv = _environment(tmp_path, systemd=True)
    script = str(ROOT / "bin/install-systemd.sh")

    first = subprocess.run(
        ["bash", script], env=env, capture_output=True, text=True
    )
    assert first.returncode == 0, first.stderr

    latest_bin = tmp_path / "latest-bin"
    latest_bin.mkdir()
    latest_uv = latest_bin / "uv"
    _executable(latest_uv, "exit 0")
    second_env = {
        **env,
        "ISSUE_AGENT_DEPLOYMENT_ID": "repo-b",
        "PATH": f"{latest_bin}:{env['PATH']}",
    }
    second = subprocess.run(
        ["bash", script], env=second_env, capture_output=True, text=True
    )
    assert second.returncode == 0, second.stderr

    unit_dir = home / ".config/systemd/user"
    dashboards = tuple(unit_dir.glob("issue-agent-dashboard.service"))
    assert len(dashboards) == 1
    assert f"ExecStart={latest_uv} " in dashboards[0].read_text()
    assert len(tuple(unit_dir.glob("*.service"))) == 5
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    assert calls.count("restart issue-agent-dashboard.service") == 2


def test_systemd_dashboard_port_conflict_preserves_installed_roles(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    port_check = tmp_path / "bin" / "dashboard-port-check"
    _executable(
        port_check,
        "printf '4242\\tpython issue-agent dashboard\\n'; exit 0",
    )
    env["ISSUE_AGENT_DASHBOARD_PORT_CHECK"] = str(port_check)

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "TCP 8321 is already bound" in result.stderr
    assert "PID 4242 (python issue-agent dashboard)" in result.stderr
    assert "Stop the manual `issue-agent dashboard`" in result.stderr
    units = tuple((home / ".config/systemd/user").glob("*.service"))
    assert len(units) == 2
    assert all(path.name != "issue-agent-dashboard.service" for path in units)
    assert result.stdout.count("active (running)") == 2


def test_systemd_installer_rolls_back_if_unit_is_not_running(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["SUB_STATE"] = "failed"

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "activation check failed" in result.stderr
    assert not tuple(
        (home / ".config/systemd/user").glob("*.service")
    )


def test_systemd_scoped_uninstall_preserves_other_deployments(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    for deployment in ("my-app", "other-app"):
        for role in ("fix-watcher", "review-watcher"):
            (unit_dir / f"issue-agent-{role}-{deployment}.service").write_text(
                deployment
            )
    dashboard = unit_dir / "issue-agent-dashboard.service"
    dashboard.write_text("singleton")

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "bin/install-systemd.sh"),
            "uninstall",
            "My App",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    remaining = tuple(unit_dir.glob("*.service"))
    assert len(remaining) == 3
    assert dashboard in remaining
    assert sum(
        path.name.endswith("-other-app.service") for path in remaining
    ) == 2


@pytest.mark.parametrize("state_var", ("ACTIVE_UNITS", "ENABLED_UNITS"))
def test_systemd_refuses_live_current_unit_without_rollback_definition(
    tmp_path, state_var
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    unit = "issue-agent-fix-watcher-target-repo.service"
    env[state_var] = unit

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "active/enabled current unit" in result.stderr
    assert "no recoverable definition" in result.stderr
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    assert not any(
        call.startswith(("stop ", "disable ", "restart ", "enable "))
        for call in calls
    )
    assert not tuple(
        (home / ".config/issue-agent").glob("*.env")
    )


def test_systemd_upgrade_stops_and_removes_every_legacy_unit_name(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "stable-name"
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    services = (
        "issue-agent-fix-watcher",
        "issue-agent-review-watcher",
    )
    for service in services:
        (unit_dir / f"{service}.service").write_text("legacy-unsuffixed")
        (unit_dir / f"{service}-target-repo.service").write_text(
            "legacy-target-name"
        )

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in unit_dir.glob("*.service")) == sorted([
        *(f"{service}-stable-name.service" for service in services),
        "issue-agent-dashboard.service",
    ])
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    for service in services:
        assert f"stop {service}.service" in calls
        assert f"stop {service}-target-repo.service" in calls
        assert f"stop {service}-stable-name.service" in calls


def test_systemd_deployment_ids_do_not_collide_and_reject_repointing(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    config = Path(env["AGENT_ENV"])
    first_target = tmp_path / "acme" / "app"
    second_target = tmp_path / "other" / "app"
    first_target.mkdir(parents=True)
    second_target.mkdir(parents=True)
    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{first_target}"\n'
    )
    first_env = {
        **env,
        "ISSUE_AGENT_DEPLOYMENT_ID": "acme-app",
    }

    first = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=first_env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    config.write_text(
        f'REPO="other/app"\nTARGET_DIR="{second_target}"\n'
    )
    second_env = {
        **env,
        "ISSUE_AGENT_DEPLOYMENT_ID": "other-app",
    }
    second = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=second_env,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    units = tuple((home / ".config/systemd/user").glob("*.service"))
    assert len(units) == 5
    assert sum(path.name.endswith("-acme-app.service") for path in units) == 2
    assert sum(path.name.endswith("-other-app.service") for path in units) == 2

    collision = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env={
            **env,
            "ISSUE_AGENT_DEPLOYMENT_ID": "acme-app",
        },
        capture_output=True,
        text=True,
    )
    assert collision.returncode == 2
    assert "REFUSING deployment 'acme-app'" in collision.stderr
    snapshot = home / ".config/issue-agent/acme-app.env"
    assert 'REPO="acme/app"' in snapshot.read_text()


def test_systemd_same_name_repoint_replaces_all_units_atomically(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    config = Path(env["AGENT_ENV"])
    first_target = tmp_path / "checkout-a"
    second_target = tmp_path / "checkout-b"
    first_target.mkdir()
    second_target.mkdir()
    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{first_target}"\nKNOWN_GOOD=1\n'
    )

    first = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    service_log = Path(env["SERVICE_LOG"])
    first_call_count = len(service_log.read_text().splitlines())
    snapshot = home / ".config/issue-agent/acme-app.env"

    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{second_target}"\n'
    )
    env["REQUIRE_OLD_SNAPSHOT_DURING_STOP"] = "1"
    env["CUTOVER_SNAPSHOT"] = str(snapshot)
    second = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert second.returncode == 0, second.stderr
    unit_dir = home / ".config/systemd/user"
    units = tuple(unit_dir.glob("*.service"))
    assert len(units) == 3
    role_units = tuple(
        path for path in units
        if path.name != "issue-agent-dashboard.service"
    )
    assert all(path.name.endswith("-acme-app.service") for path in role_units)
    assert all(
        f"Environment=TARGET_DIR={second_target}" in path.read_text()
        for path in role_units
    )
    assert str(second_target) in snapshot.read_text()
    second_calls = service_log.read_text().splitlines()[first_call_count:]
    stops = [
        second_calls.index(f"stop {path.name}")
        for path in role_units
    ]
    restarts = [
        second_calls.index(f"restart {path.name}")
        for path in role_units
    ]
    assert max(stops) < min(restarts)


def test_systemd_second_checkout_preserves_same_repo_deployment(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    config = Path(env["AGENT_ENV"])
    first_target = tmp_path / "checkout-primary"
    second_target = tmp_path / "checkout-secondary"
    first_target.mkdir()
    second_target.mkdir()
    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{first_target}"\n'
    )

    first = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env={**env, "ISSUE_AGENT_DEPLOYMENT_ID": "acme-app"},
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{second_target}"\n'
    )
    second = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env={**env, "ISSUE_AGENT_DEPLOYMENT_ID": "acme-app-wt"},
        capture_output=True,
        text=True,
    )

    assert second.returncode == 0, second.stderr
    units = tuple((home / ".config/systemd/user").glob("*.service"))
    assert len(units) == 5
    assert sum(path.name.endswith("-acme-app.service") for path in units) == 2
    assert sum(
        path.name.endswith("-acme-app-wt.service") for path in units
    ) == 2
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    assert not any(
        call == f"stop issue-agent-{role}-acme-app.service"
        for call in calls[len(calls) // 2 :]
        for role in ("fix-watcher", "review-watcher")
    )


def test_systemd_failed_activation_rolls_back_units_and_config(tmp_path):
    env, home, _uv = _environment(
        tmp_path, systemd=True, fail_pattern="review-watcher"
    )
    unit_dir = home / ".config/systemd/user"
    config_dir = home / ".config/issue-agent"
    unit_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    names = (
        "issue-agent-fix-watcher",
        "issue-agent-review-watcher",
    )
    for name in names:
        (unit_dir / f"{name}-target-repo.service").write_text("known-good")
    dashboard = unit_dir / "issue-agent-dashboard.service"
    dashboard.write_text("known-good-dashboard")
    snapshot = config_dir / "target-repo.env"
    snapshot.write_text("KNOWN_GOOD=1\n")

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert all(
        (unit_dir / f"{name}-target-repo.service").read_text() == "known-good"
        for name in names
    )
    assert dashboard.read_text() == "known-good-dashboard"
    assert snapshot.read_text() == "KNOWN_GOOD=1\n"


def test_launchd_installer_renders_validated_plists_with_absolute_uv(tmp_path):
    env, home, uv = _environment(tmp_path, systemd=False)

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    plists = sorted((home / "Library/LaunchAgents").glob("*.plist"))
    assert len(plists) == 2
    assert all(
        plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(uv)
        for plist in plists
    )
    assert all(
        plistlib.loads(plist.read_bytes())["EnvironmentVariables"][
            "ISSUE_AGENT_DEPLOYMENT_ID"
        ]
        for plist in plists
    )


def test_launchd_same_name_repoint_replaces_all_loaded_jobs_atomically(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=False)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    config = Path(env["AGENT_ENV"])
    first_target = tmp_path / "checkout-a"
    second_target = tmp_path / "checkout-b"
    first_target.mkdir()
    second_target.mkdir()
    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{first_target}"\nKNOWN_GOOD=1\n'
    )

    first = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    service_log = Path(env["SERVICE_LOG"])
    first_call_count = len(service_log.read_text().splitlines())
    snapshot = (
        home
        / "Library/Application Support/issue-agent"
        / "acme-app.env"
    )

    config.write_text(
        f'REPO="acme/app"\nTARGET_DIR="{second_target}"\n'
    )
    env["REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD"] = "1"
    env["CUTOVER_SNAPSHOT"] = str(snapshot)
    second = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert second.returncode == 0, second.stderr
    launch_agents = home / "Library/LaunchAgents"
    plists = tuple(launch_agents.glob("*.plist"))
    assert len(plists) == 2
    assert all(path.name.endswith(".acme-app.plist") for path in plists)
    assert all(
        plistlib.loads(path.read_bytes())["EnvironmentVariables"]["TARGET_DIR"]
        == str(second_target)
        for path in plists
    )
    assert str(second_target) in snapshot.read_text()
    second_calls = service_log.read_text().splitlines()[first_call_count:]
    unloads = [
        second_calls.index(f"unload {path}")
        for path in plists
    ]
    loads = [
        second_calls.index(f"load {path}")
        for path in plists
    ]
    assert max(unloads) < min(loads)


def test_launchd_scoped_uninstall_preserves_other_deployments(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=False)
    config = Path(env["AGENT_ENV"])
    target = tmp_path / "target-repo"
    checksum = subprocess.run(
        ["cksum"],
        input=f"{config}|{target}",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    for role in ("fix-watcher", "review-watcher"):
        (launch_agents / f"dev.issue-agent.{role}.{checksum}.plist").write_text(
            "current"
        )
        (launch_agents / f"dev.issue-agent.{role}.999999.plist").write_text(
            "other"
        )

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "bin/install-launchd.sh"),
            "uninstall",
            "target-repo",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    remaining = tuple(launch_agents.glob("*.plist"))
    assert len(remaining) == 2
    assert all(path.name.endswith(".999999.plist") for path in remaining)


def test_launchd_upgrade_unloads_and_removes_unsuffixed_legacy_jobs(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=False)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "stable-name"
    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    labels = (
        "dev.issue-agent.fix-watcher",
        "dev.issue-agent.review-watcher",
    )
    for label in labels:
        (launch_agents / f"{label}.plist").write_text("legacy")

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        (launch_agents / f"{label}.plist").exists() for label in labels
    )
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    for label in labels:
        legacy = launch_agents / f"{label}.plist"
        assert f"unload {legacy}" in calls
        assert f"remove {label}" in calls


def test_launchd_refuses_loaded_current_job_without_rollback_plist(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=False)
    config = Path(env["AGENT_ENV"])
    target = tmp_path / "target-repo"
    checksum = subprocess.run(
        ["cksum"],
        input=f"{config}|{target}",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    label = f"dev.issue-agent.fix-watcher.{checksum}"
    env["LOADED_LABELS"] = label

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "loaded current job" in result.stderr
    assert "no recoverable plist" in result.stderr
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    assert not any(call.startswith(("unload ", "remove ", "load ")) for call in calls)
    assert not tuple(
        (
            home / "Library/Application Support/issue-agent"
        ).glob("*.env")
    )


def test_launchd_failed_load_restores_every_known_good_plist(tmp_path):
    env, home, _uv = _environment(
        tmp_path, systemd=False, fail_pattern="review-watcher"
    )
    config = Path(env["AGENT_ENV"])
    target = tmp_path / "target-repo"
    checksum = subprocess.run(
        ["cksum"],
        input=f"{config}|{target}",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    labels = (
        "dev.issue-agent.fix-watcher",
        "dev.issue-agent.review-watcher",
    )
    for label in labels:
        (launch_agents / f"{label}.{checksum}.plist").write_text("known-good")
        (launch_agents / f"{label}.plist").write_text("legacy-known-good")
    snapshot = (
        home
        / "Library/Application Support/issue-agent"
        / f"{checksum}.env"
    )
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        f'REPO=""\nTARGET_DIR="{target}"\nKNOWN_GOOD=1\n'
    )

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert all(
        (launch_agents / f"{label}.{checksum}.plist").read_text()
        == "known-good"
        for label in labels
    )
    assert all(
        (launch_agents / f"{label}.plist").read_text()
        == "legacy-known-good"
        for label in labels
    )
    assert "KNOWN_GOOD=1" in snapshot.read_text()


def test_launchd_stops_every_current_writer_before_swapping_config(
    tmp_path,
):
    env, home, _uv = _environment(tmp_path, systemd=False)
    config = Path(env["AGENT_ENV"])
    target = tmp_path / "target-repo"
    checksum = subprocess.run(
        ["cksum"],
        input=f"{config}|{target}",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    labels = (
        f"dev.issue-agent.fix-watcher.{checksum}",
        f"dev.issue-agent.review-watcher.{checksum}",
    )
    for label in labels:
        (launch_agents / f"{label}.plist").write_text("known-good")
    snapshot = (
        home
        / "Library/Application Support/issue-agent"
        / f"{checksum}.env"
    )
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        f'REPO=""\nTARGET_DIR="{target}"\nKNOWN_GOOD=1\n'
    )
    env["REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD"] = "1"
    env["CUTOVER_SNAPSHOT"] = str(snapshot)

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    unloads = [
        calls.index(f"unload {launch_agents / f'{label}.plist'}")
        for label in labels
    ]
    loads = [
        calls.index(f"load {launch_agents / f'{label}.plist'}")
        for label in labels
    ]
    assert max(unloads) < min(loads)
    assert "KNOWN_GOOD=1" not in snapshot.read_text()


def test_launchd_success_uses_stable_private_config_snapshot(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=False)
    config = Path(env["AGENT_ENV"])
    config.write_text(
        f'REPO="o/r"\nTARGET_DIR="{tmp_path / "target-repo"}"\n'
        "TOKEN=secret\n"
    )

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    snapshots = tuple(
        (
            home / "Library/Application Support/issue-agent"
        ).glob("*.env")
    )
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == config.read_text()
    assert snapshots[0].stat().st_mode & 0o777 == 0o600
    for plist in (home / "Library/LaunchAgents").glob("*.plist"):
        rendered = plistlib.loads(plist.read_bytes())
        assert (
            rendered["EnvironmentVariables"]["AGENT_ENV"]
            == str(snapshots[0])
        )


@contextlib.contextmanager
def _process_tree():
    """A real `bash → bash → sleep` tree; yields (root pid, grandchild pid)."""
    root = subprocess.Popen(
        ["bash", "-c", "bash -c 'sleep 60 & wait' & wait"], start_new_session=True
    )
    try:
        leaf = None
        for _ in range(100):
            middle = subprocess.run(
                ["pgrep", "-P", str(root.pid)], capture_output=True, text=True
            ).stdout.split()
            if middle:
                found = subprocess.run(
                    ["pgrep", "-P", middle[0]], capture_output=True, text=True
                ).stdout.split()
                if found:
                    leaf = int(found[0])
                    break
            time.sleep(0.05)
        assert leaf is not None
        yield root.pid, leaf
    finally:
        os.killpg(root.pid, signal.SIGKILL)
        root.wait()


def _install_with_port_owner(tmp_path, owner_pid, main_pid):
    env, home, _uv = _environment(tmp_path, systemd=True)
    unit = home / ".config/systemd/user/issue-agent-dashboard.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=/old\n")
    port_check = tmp_path / "bin" / "dashboard-port-check"
    _executable(port_check, f"printf '{owner_pid}\\tissue-agent\\tdashboard\\n'")
    env["ISSUE_AGENT_DASHBOARD_PORT_CHECK"] = str(port_check)
    env["MAIN_PID"] = str(main_pid)
    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    return result, unit


def test_systemd_dashboard_port_held_by_its_own_unit_descendant_reinstalls(tmp_path):
    # Incident 2026-10-12: the unit's MainPID is `uv`; the listener is its
    # `issue-agent` child, so a PID comparison refused the unit's own socket.
    with _process_tree() as (main_pid, listener):
        result, unit = _install_with_port_owner(tmp_path, listener, main_pid)

    assert result.returncode == 0, result.stderr
    assert "ExecStart=/old" not in unit.read_text()


def test_systemd_dashboard_port_held_by_its_main_pid_reinstalls(tmp_path):
    with _process_tree() as (main_pid, _listener):
        result, _unit = _install_with_port_owner(tmp_path, main_pid, main_pid)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("main_pid", ("unrelated", "0", "", "not-a-pid"))
def test_systemd_dashboard_port_held_outside_the_unit_is_refused(tmp_path, main_pid):
    with _process_tree() as (_root, listener), _process_tree() as (other, _leaf):
        unit_pid = other if main_pid == "unrelated" else main_pid
        result, unit = _install_with_port_owner(tmp_path, listener, unit_pid)

    assert result.returncode != 0
    assert "TCP 8321 is already bound" in result.stderr
    assert "ExecStart=/old" in unit.read_text()


def test_systemd_dashboard_port_owner_unknown_is_refused(tmp_path):
    with _process_tree() as (main_pid, _listener):
        result, unit = _install_with_port_owner(tmp_path, "unknown", main_pid)

    assert result.returncode != 0
    assert "PID unknown" in result.stderr
