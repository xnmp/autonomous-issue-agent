from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path


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
