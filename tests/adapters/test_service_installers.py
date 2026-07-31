from __future__ import annotations

import os
import plistlib
import subprocess
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
        "FAIL_ONCE_FILE": str(tmp_path / "failed-once"),
        "SERVICE_LOG": str(tmp_path / "service.log"),
        "LOADED_LABELS": "",
        "ACTIVE_UNITS": "",
        "ENABLED_UNITS": "",
        "REQUIRE_OLD_SNAPSHOT_DURING_UNLOAD": "0",
        "CUTOVER_SNAPSHOT": "",
    }
    return env, home, fake_bin / "uv"


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
    assert all(
        "Environment=ISSUE_AGENT_DEPLOYMENT_ID=target-repo"
        in unit.read_text()
        for unit in units
    )


def test_systemd_scoped_uninstall_preserves_other_deployments(tmp_path):
    env, home, _uv = _environment(tmp_path, systemd=True)
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    for deployment in ("my-app", "other-app"):
        for role in ("fix-watcher", "review-watcher", "steward"):
            (unit_dir / f"issue-agent-{role}-{deployment}.service").write_text(
                deployment
            )

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
    assert all(path.name.endswith("-other-app.service") for path in remaining)


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


@pytest.mark.parametrize("state_var", ("ACTIVE_UNITS", "ENABLED_UNITS"))
def test_systemd_refuses_live_legacy_unit_without_rollback_definition(
    tmp_path, state_var
):
    env, home, _uv = _environment(tmp_path, systemd=True)
    env[state_var] = "issue-agent-fix-watcher.service"

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "active/enabled legacy unit" in result.stderr
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
        "issue-agent-steward",
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
    assert sorted(path.name for path in unit_dir.glob("*.service")) == [
        f"{service}-stable-name.service" for service in services
    ]
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
    assert len(units) == 6
    assert sum(path.name.endswith("-acme-app.service") for path in units) == 3
    assert sum(path.name.endswith("-other-app.service") for path in units) == 3

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
    assert len(units) == 6
    assert sum(path.name.endswith("-acme-app.service") for path in units) == 3
    assert sum(
        path.name.endswith("-acme-app-wt.service") for path in units
    ) == 3
    calls = Path(env["SERVICE_LOG"]).read_text().splitlines()
    assert not any(
        call == f"stop issue-agent-{role}-acme-app.service"
        for call in calls[len(calls) // 2 :]
        for role in ("fix-watcher", "review-watcher", "steward")
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
        "issue-agent-steward",
    )
    for name in names:
        (unit_dir / f"{name}-target-repo.service").write_text("known-good")
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
    assert snapshot.read_text() == "KNOWN_GOOD=1\n"


def test_systemd_failed_cutover_restores_legacy_units(tmp_path):
    env, home, _uv = _environment(
        tmp_path, systemd=True, fail_pattern="review-watcher"
    )
    env["ISSUE_AGENT_DEPLOYMENT_ID"] = "stable-name"
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    services = (
        "issue-agent-fix-watcher",
        "issue-agent-review-watcher",
        "issue-agent-steward",
    )
    for service in services:
        (unit_dir / f"{service}-target-repo.service").write_text(
            f"legacy-{service}"
        )

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert all(
        (unit_dir / f"{service}-target-repo.service").read_text()
        == f"legacy-{service}"
        for service in services
    )
    assert not any(unit_dir.glob("*-stable-name.service"))


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
    assert len(plists) == 3
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
    for role in ("fix-watcher", "review-watcher", "steward"):
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
    assert len(remaining) == 3
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
        "dev.issue-agent.steward",
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


@pytest.mark.parametrize("selector", ("--fix", "--review", "--steward"))
def test_launchd_refuses_partial_migration_cutover(tmp_path, selector):
    env, home, _uv = _environment(tmp_path, systemd=False)

    result = subprocess.run(
        ["bash", str(ROOT / "bin/install-launchd.sh"), selector],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "every legacy writer stops" in result.stderr
    assert tuple((home / "Library/LaunchAgents").glob("*.plist")) == ()


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
        "dev.issue-agent.steward",
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
        f"dev.issue-agent.steward.{checksum}",
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
