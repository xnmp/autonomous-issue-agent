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


def _bash(env, script, *args):
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _environment(tmp_path: Path, *, systemd: bool, fail_pattern: str = "") -> tuple[dict[str, str], Path, Path]:
    home, fake_bin, target, config = (tmp_path / name for name in ("home", "bin", "target-repo", "agent.env"))
    for directory in (home, fake_bin, target):
        directory.mkdir()
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


def _script(systemd: bool) -> str:
    return str(ROOT / "bin" / ("install-systemd.sh" if systemd else "install-launchd.sh"))


def _install(env, *args, systemd: bool = True):
    return _bash(env, _script(systemd), *args)


def _artifacts(home: Path, systemd: bool) -> Path:
    return home / ".config/systemd/user" if systemd else home / "Library/LaunchAgents"


def _calls(env) -> list[str]:
    return Path(env["SERVICE_LOG"]).read_text().splitlines()


def _checksum(env, tmp_path: Path) -> str:
    """The launchd job suffix the installer derives from (config, target)."""
    return subprocess.run(
        ["cksum"], input=f"{env['AGENT_ENV']}|{tmp_path / 'target-repo'}", capture_output=True, text=True, check=True
    ).stdout.split()[0]


def _launchd_snapshot(home: Path, checksum: str, target: Path) -> Path:
    snapshot = home / "Library/Application Support/issue-agent" / f"{checksum}.env"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(f'REPO=""\nTARGET_DIR="{target}"\nKNOWN_GOOD=1\n')
    return snapshot


@pytest.mark.parametrize("systemd", (True, False))
def test_service_installers_reject_noncanonical_deployment_identity(tmp_path, systemd):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "Acme App"

    result = _install(env, systemd=systemd)

    assert result.returncode == 2
    assert "non-canonical deployment identity" in result.stderr
    assert not tuple(_artifacts(home, systemd).glob("*"))


@pytest.mark.parametrize("systemd", (True, False))
def test_service_installers_refuse_an_existing_identity_alias(tmp_path, systemd):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    if systemd:
        artifact = _artifacts(home, systemd) / "issue-agent-fix-watcher-acme-app.service"
        text = "Environment=ISSUE_AGENT_DEPLOYMENT_ID=Acme App\n"
    else:
        artifact = _artifacts(home, systemd) / "dev.issue-agent.fix-watcher.123456.plist"
        text = "<key>ISSUE_AGENT_DEPLOYMENT_ID</key><string>Acme App</string>\n"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(text)

    result = _install(env, systemd=systemd)

    assert result.returncode == 2
    assert "deployment identity alias" in result.stderr
    assert artifact.exists()


@pytest.mark.parametrize("systemd", (True, False))
def test_service_uninstall_preserves_every_definition_when_stop_fails(tmp_path, systemd):
    env, home, _uv = _environment(tmp_path, systemd=systemd)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    Path(env["AGENT_ENV"]).write_text(f'REPO="acme/app"\nTARGET_DIR="{tmp_path / "target-repo"}"\n')
    install = _install(env, systemd=systemd)
    assert install.returncode == 0, install.stderr
    artifacts = tuple(_artifacts(home, systemd).glob("*"))
    assert len(artifacts) == (3 if systemd else 2)

    env["FAIL_UNINSTALL_PATTERN"] = "review-watcher"
    uninstall = _install(env, "uninstall", "acme-app", systemd=systemd)

    assert uninstall.returncode != 0
    assert "REFUSING uninstall" in uninstall.stderr
    assert all(path.exists() for path in artifacts)
    assert "removed 3" not in uninstall.stdout


def _role_units(units) -> tuple[Path, ...]:
    return tuple(unit for unit in units if unit.name != "issue-agent-dashboard.service")


def test_systemd_installer_renders_validated_units_with_absolute_uv(tmp_path):
    env, home, uv = _environment(tmp_path, systemd=True)

    result = _install(env)

    assert result.returncode == 0, result.stderr
    units = sorted(_artifacts(home, True).glob("*.service"))
    assert len(units) == 3
    assert all(f"ExecStart={uv} " in unit.read_text() for unit in units)
    terminal_hook = ROOT / "bin/service-terminal-evidence.sh"
    assert terminal_hook.is_file()
    for unit in _role_units(units):
        text = unit.read_text()
        for directive in (
            "NotifyAccess=all",
            "WatchdogSec=300",
            "KillMode=mixed",
            "TimeoutStopSec=90",
            "FinalKillSignal=SIGKILL",
            f'ExecStopPost=-/bin/sh "{terminal_hook}"\n',
            "Environment=ISSUE_AGENT_DEPLOYMENT_ID=target-repo",
        ):
            assert directive in text, (unit.name, directive)
    dashboard = _artifacts(home, True) / "issue-agent-dashboard.service"
    assert "Environment=AGENT_ENV=" not in dashboard.read_text()
    assert "Environment=TARGET_DIR=" not in dashboard.read_text()
    assert result.stdout.count("active (running)") == 3


def test_systemd_dashboard_install_is_singleton_and_latest_install_wins(tmp_path):
    env, home, _first_uv = _environment(tmp_path, systemd=True)
    first = _install(env)
    assert first.returncode == 0, first.stderr

    latest_bin = tmp_path / "latest-bin"
    latest_bin.mkdir()
    _executable(latest_bin / "uv", "exit 0")
    second = _install({**env, "ISSUE_AGENT_DEPLOYMENT_ID": "repo-b", "PATH": f"{latest_bin}:{env['PATH']}"})
    assert second.returncode == 0, second.stderr

    unit_dir = _artifacts(home, True)
    dashboards = tuple(unit_dir.glob("issue-agent-dashboard.service"))
    assert len(dashboards) == 1
    assert f"ExecStart={latest_bin / 'uv'} " in dashboards[0].read_text()
    assert len(tuple(unit_dir.glob("*.service"))) == 5
    assert _calls(env).count("restart issue-agent-dashboard.service") == 2


def test_systemd_dashboard_port_conflict_preserves_installed_roles(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    port_check = tmp_path / "bin" / "dashboard-port-check"
    _executable(port_check, "printf '4242\\tpython issue-agent dashboard\\n'; exit 0")
    env["ISSUE_AGENT_DASHBOARD_PORT_CHECK"] = str(port_check)

    result = _install(env)

    assert result.returncode != 0
    assert "TCP 8321 is already bound" in result.stderr
    assert "PID 4242 (python issue-agent dashboard)" in result.stderr
    assert "Stop the manual `issue-agent dashboard`" in result.stderr
    units = tuple(_artifacts(home, True).glob("*.service"))
    assert len(units) == 2
    assert all(path.name != "issue-agent-dashboard.service" for path in units)
    assert result.stdout.count("active (running)") == 2


def test_systemd_installer_rolls_back_if_unit_is_not_running(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["SUB_STATE"] = "failed"

    result = _install(env)

    assert result.returncode != 0
    assert "activation check failed" in result.stderr
    assert not tuple(_artifacts(home, True).glob("*.service"))


def test_systemd_scoped_uninstall_preserves_other_deployments(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    unit_dir = _artifacts(home, True)
    unit_dir.mkdir(parents=True)
    for deployment in ("my-app", "other-app"):
        for role in ("fix-watcher", "review-watcher"):
            (unit_dir / f"issue-agent-{role}-{deployment}.service").write_text(deployment)
    dashboard = unit_dir / "issue-agent-dashboard.service"
    dashboard.write_text("singleton")

    result = _install(env, "uninstall", "My App")

    assert result.returncode == 0, result.stderr
    remaining = tuple(unit_dir.glob("*.service"))
    assert len(remaining) == 3
    assert dashboard in remaining
    assert sum(path.name.endswith("-other-app.service") for path in remaining) == 2


@pytest.mark.parametrize("state_var", ("ACTIVE_UNITS", "ENABLED_UNITS"))
@pytest.mark.parametrize(
    "unit,kind",
    [("issue-agent-fix-watcher-target-repo.service", "current"), ("issue-agent-fix-watcher.service", "legacy")],
)
def test_systemd_refuses_a_live_unit_without_rollback_definition(tmp_path, state_var, unit, kind):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env[state_var] = unit

    result = _install(env)

    assert result.returncode == 2
    assert f"active/enabled {kind} unit" in result.stderr
    assert "no recoverable definition" in result.stderr
    assert not any(call.startswith(("stop ", "disable ", "restart ", "enable ")) for call in _calls(env))
    assert not tuple((home / ".config/issue-agent").glob("*.env"))


_SERVICES = ("issue-agent-fix-watcher", "issue-agent-review-watcher")


def test_systemd_upgrade_stops_and_removes_every_legacy_unit_name(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "stable-name"
    unit_dir = _artifacts(home, True)
    unit_dir.mkdir(parents=True)
    for service in _SERVICES:
        (unit_dir / f"{service}.service").write_text("legacy-unsuffixed")
        (unit_dir / f"{service}-target-repo.service").write_text("legacy-target-name")

    result = _install(env)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in unit_dir.glob("*.service")) == sorted(
        [*(f"{service}-stable-name.service" for service in _SERVICES), "issue-agent-dashboard.service"]
    )
    calls = _calls(env)
    for service in _SERVICES:
        for name in (service, f"{service}-target-repo", f"{service}-stable-name"):
            assert f"stop {name}.service" in calls


def test_systemd_failed_cutover_restores_legacy_units(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True, fail_pattern="review-watcher")
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "stable-name"
    unit_dir = _artifacts(home, True)
    unit_dir.mkdir(parents=True)
    for service in _SERVICES:
        (unit_dir / f"{service}-target-repo.service").write_text(f"legacy-{service}")

    result = _install(env)

    assert result.returncode != 0
    assert all((unit_dir / f"{s}-target-repo.service").read_text() == f"legacy-{s}" for s in _SERVICES)
    assert not any(unit_dir.glob("*-stable-name.service"))


def test_systemd_deployment_ids_do_not_collide_and_reject_repointing(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    config = Path(env["AGENT_ENV"])
    for repo in ("acme/app", "other/app"):
        target = tmp_path / repo
        target.mkdir(parents=True)
        config.write_text(f'REPO="{repo}"\nTARGET_DIR="{target}"\n')
        installed = _install({**env, "ISSUE_AGENT_DEPLOYMENT_ID": repo.replace("/", "-")})
        assert installed.returncode == 0, installed.stderr
    units = tuple(_artifacts(home, True).glob("*.service"))
    assert len(units) == 5
    assert sum(path.name.endswith("-acme-app.service") for path in units) == 2
    assert sum(path.name.endswith("-other-app.service") for path in units) == 2

    collision = _install({**env, "ISSUE_AGENT_DEPLOYMENT_ID": "acme-app"})
    assert collision.returncode == 2
    assert "REFUSING deployment 'acme-app'" in collision.stderr
    assert 'REPO="acme/app"' in (home / ".config/issue-agent/acme-app.env").read_text()


def test_systemd_same_name_repoint_replaces_all_units_atomically(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    config = Path(env["AGENT_ENV"])
    first_target, second_target = tmp_path / "checkout-a", tmp_path / "checkout-b"
    first_target.mkdir()
    second_target.mkdir()
    config.write_text(f'REPO="acme/app"\nTARGET_DIR="{first_target}"\nKNOWN_GOOD=1\n')

    first = _install(env)
    assert first.returncode == 0, first.stderr
    first_call_count = len(_calls(env))
    snapshot = home / ".config/issue-agent/acme-app.env"

    config.write_text(f'REPO="acme/app"\nTARGET_DIR="{second_target}"\n')
    env["REQUIRE_OLD_SNAPSHOT_DURING_STOP"] = "1"
    env["CUTOVER_SNAPSHOT"] = str(snapshot)
    second = _install(env)

    assert second.returncode == 0, second.stderr
    units = tuple(_artifacts(home, True).glob("*.service"))
    assert len(units) == 3
    role_units = _role_units(units)
    assert all(path.name.endswith("-acme-app.service") for path in role_units)
    assert all(f"Environment=TARGET_DIR={second_target}" in path.read_text() for path in role_units)
    assert str(second_target) in snapshot.read_text()
    second_calls = _calls(env)[first_call_count:]
    stops = [second_calls.index(f"stop {path.name}") for path in role_units]
    restarts = [second_calls.index(f"restart {path.name}") for path in role_units]
    assert max(stops) < min(restarts)


def test_systemd_failed_activation_rolls_back_units_and_config(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True, fail_pattern="review-watcher")
    unit_dir, config_dir = _artifacts(home, True), home / ".config/issue-agent"
    unit_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    for name in _SERVICES:
        (unit_dir / f"{name}-target-repo.service").write_text("known-good")
    dashboard = unit_dir / "issue-agent-dashboard.service"
    dashboard.write_text("known-good-dashboard")
    snapshot = config_dir / "target-repo.env"
    snapshot.write_text("KNOWN_GOOD=1\n")

    result = _install(env)

    assert result.returncode != 0
    assert all((unit_dir / f"{name}-target-repo.service").read_text() == "known-good" for name in _SERVICES)
    assert dashboard.read_text() == "known-good-dashboard"
    assert snapshot.read_text() == "KNOWN_GOOD=1\n"


def test_launchd_installer_renders_validated_plists_with_absolute_uv(tmp_path):
    env, home, uv = _environment(tmp_path, systemd=False)

    result = _install(env, systemd=False)

    assert result.returncode == 0, result.stderr
    plists = [plistlib.loads(plist.read_bytes()) for plist in sorted(_artifacts(home, False).glob("*.plist"))]
    assert len(plists) == 2
    assert all(plist["ProgramArguments"][0] == str(uv) for plist in plists)
    assert all(plist["EnvironmentVariables"]["ISSUE_AGENT_DEPLOYMENT_ID"] for plist in plists)


@pytest.mark.parametrize("selector", ("--fix", "--review"))
def test_launchd_refuses_partial_migration_cutover(tmp_path, selector):
    env, home, _uv = _environment(tmp_path, systemd=False)

    result = _install(env, selector, systemd=False)

    assert result.returncode == 2
    assert "every legacy writer stops" in result.stderr
    assert tuple(_artifacts(home, False).glob("*.plist")) == ()


@pytest.mark.parametrize("fails", [False, True], ids=["migrates", "rolls-back"])
def test_launchd_stable_identity_cutover_of_checksum_predecessors(tmp_path, fails):
    """Checksum-named jobs of this deployment are the old writers: a stable
    install unloads and removes them (and only them); a failed one restores them."""
    env, home, _uv = _environment(tmp_path, systemd=False, fail_pattern="review-watcher" if fails else "")
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "acme-app"
    Path(env["AGENT_ENV"]).write_text(f'REPO="acme/app"\nTARGET_DIR="{tmp_path / "target-repo"}"\n')
    agents = _artifacts(home, False)
    agents.mkdir(parents=True)
    units = ("fix-watcher", "review-watcher")
    mine = tuple(agents / f"dev.issue-agent.{unit}.123456.plist" for unit in units)
    other = tuple(agents / f"dev.issue-agent.{unit}.999999.plist" for unit in units)
    for paths, owner in ((mine, "acme-app"), (other, "other-app")):
        for path in paths:
            path.write_text(f"<key>ISSUE_AGENT_DEPLOYMENT_ID</key><string>{owner}</string>\n")

    result = _install(env, systemd=False)

    calls = _calls(env)
    assert all(path.exists() for path in other)
    current = tuple(agents.glob("dev.issue-agent.*.acme-app.plist"))
    if fails:
        assert result.returncode != 0
        assert all(path.exists() and calls.count(f"load {path}") == 1 for path in mine)
        assert current == ()
        assert not (home / "Library/Application Support/issue-agent/acme-app.env").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert len(current) == 2 and not any(path.exists() for path in mine)
        assert all(f"unload {path}" in calls and f"remove {path.stem}" in calls for path in mine)


def test_launchd_scoped_uninstall_preserves_other_deployments(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=False)
    checksum = _checksum(env, tmp_path)
    launch_agents = _artifacts(home, False)
    launch_agents.mkdir(parents=True)
    for role in ("fix-watcher", "review-watcher"):
        (launch_agents / f"dev.issue-agent.{role}.{checksum}.plist").write_text("current")
        (launch_agents / f"dev.issue-agent.{role}.999999.plist").write_text("other")

    result = _install(env, "uninstall", "target-repo", systemd=False)

    assert result.returncode == 0, result.stderr
    remaining = tuple(launch_agents.glob("*.plist"))
    assert len(remaining) == 2
    assert all(path.name.endswith(".999999.plist") for path in remaining)


def test_launchd_failed_load_restores_every_known_good_plist(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=False, fail_pattern="review-watcher")
    checksum = _checksum(env, tmp_path)
    launch_agents = _artifacts(home, False)
    launch_agents.mkdir(parents=True)
    labels = ("dev.issue-agent.fix-watcher", "dev.issue-agent.review-watcher")
    for label in labels:
        (launch_agents / f"{label}.{checksum}.plist").write_text("known-good")
        (launch_agents / f"{label}.plist").write_text("legacy-known-good")
    snapshot = _launchd_snapshot(home, checksum, tmp_path / "target-repo")

    result = _install(env, systemd=False)

    assert result.returncode != 0
    assert all((launch_agents / f"{label}.{checksum}.plist").read_text() == "known-good" for label in labels)
    assert all((launch_agents / f"{label}.plist").read_text() == "legacy-known-good" for label in labels)
    assert "KNOWN_GOOD=1" in snapshot.read_text()


def test_launchd_stops_every_current_writer_before_swapping_config(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=False)
    checksum = _checksum(env, tmp_path)
    launch_agents = _artifacts(home, False)
    launch_agents.mkdir(parents=True)
    plists = [launch_agents / f"dev.issue-agent.{role}.{checksum}.plist" for role in ("fix-watcher", "review-watcher")]
    for plist in plists:
        plist.write_text("known-good")
    snapshot = _launchd_snapshot(home, checksum, tmp_path / "target-repo")
    env["REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD"] = "1"
    env["CUTOVER_SNAPSHOT"] = str(snapshot)

    result = _install(env, systemd=False)

    assert result.returncode == 0, result.stderr
    calls = _calls(env)
    unloads, loads = ([calls.index(f"{verb} {plist}") for plist in plists] for verb in ("unload", "load"))
    assert max(unloads) < min(loads)
    assert "KNOWN_GOOD=1" not in snapshot.read_text()


@contextlib.contextmanager
def _process_tree():
    """A real `bash → bash → sleep` tree; yields (root pid, grandchild pid)."""
    root = subprocess.Popen(["bash", "-c", "bash -c 'sleep 60 & wait' & wait"], start_new_session=True)

    def children(pid) -> list[str]:
        return subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()

    try:
        leaf = None
        for _ in range(100):
            middle = children(root.pid)
            found = children(middle[0]) if middle else []
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
    unit = _artifacts(home, True) / "issue-agent-dashboard.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=/old\n")
    port_check = tmp_path / "bin" / "dashboard-port-check"
    _executable(port_check, f"printf '{owner_pid}\\tissue-agent\\tdashboard\\n'")
    env["ISSUE_AGENT_DASHBOARD_PORT_CHECK"] = str(port_check)
    env["MAIN_PID"] = str(main_pid)
    return _install(env), unit


@pytest.mark.parametrize("holder", ("descendant", "main-pid"))
def test_systemd_dashboard_port_held_by_its_own_unit_reinstalls(tmp_path, holder):
    # Incident 2026-10-12: the unit's MainPID is `uv`; the listener is its
    # `issue-agent` child, so a PID comparison refused the unit's own socket.
    with _process_tree() as (main_pid, listener):
        result, unit = _install_with_port_owner(tmp_path, listener if holder == "descendant" else main_pid, main_pid)

    assert result.returncode == 0, result.stderr
    assert "ExecStart=/old" not in unit.read_text()


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


def _install_with_credentials(tmp_path, present):
    env, home, _ = _environment(tmp_path, systemd=True)
    creds = home / ".config" / "issue-agent" / "credentials"
    creds.mkdir(parents=True)
    for name in present:
        (creds / f"{name}.cred").write_text("opaque")
    assert _install(env).returncode == 0
    fix, review = _role_units(sorted(_artifacts(home, True).glob("*.service")))
    lines = lambda unit: [x for x in unit.read_text().splitlines() if x.startswith("LoadCredentialEncrypted=")]  # noqa: E731
    return creds, fix, review, lines


def test_systemd_units_load_each_role_its_own_github_token_and_both_the_claude_token(tmp_path):
    creds, fix, review, lines = _install_with_credentials(
        tmp_path, ("FIX_BOT_TOKEN", "REVIEW_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"))

    assert lines(fix) == [f"LoadCredentialEncrypted=FIX_BOT_TOKEN:{creds}/FIX_BOT_TOKEN.cred",
                          f"LoadCredentialEncrypted=CLAUDE_CODE_OAUTH_TOKEN:{creds}/CLAUDE_CODE_OAUTH_TOKEN.cred"]
    assert lines(review) == [f"LoadCredentialEncrypted=REVIEW_BOT_TOKEN:{creds}/REVIEW_BOT_TOKEN.cred",
                             f"LoadCredentialEncrypted=CLAUDE_CODE_OAUTH_TOKEN:{creds}/CLAUDE_CODE_OAUTH_TOKEN.cred"]


def test_systemd_units_emit_no_line_for_an_absent_credential_file(tmp_path):
    # A line for a missing file fails the unit at start.
    _creds, fix, review, lines = _install_with_credentials(tmp_path, ("REVIEW_BOT_TOKEN",))

    assert lines(fix) == []
    assert len(lines(review)) == 1
    for unit in (fix, review):
        assert "@CREDENTIALS@" not in unit.read_text()
        assert "TasksMax=4096" in unit.read_text()
