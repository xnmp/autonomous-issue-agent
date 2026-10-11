"""Operator scripts must never discover the legacy single-repo config."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "script,args",
    [
        ("doctor.sh", []),
        ("master.sh", []),
        ("install-systemd.sh", []),
        ("install-launchd.sh", []),
        ("install-launchd.sh", ["uninstall", "demo"]),
    ],
)
@pytest.mark.parametrize("agent_env", [None, ""])
def test_scripts_refuse_missing_agent_env_without_reading_legacy_file(
    tmp_path, script, args, agent_env
):
    (tmp_path / "bin").mkdir()
    (tmp_path / "config").mkdir()
    shutil.copy2(ROOT / "bin" / script, tmp_path / "bin" / script)
    marker = tmp_path / "legacy-read"
    legacy = tmp_path / "config" / "agent.env"
    legacy.write_text(f'touch "{marker}"\nexit 0\n')
    env = {**os.environ, "HARNESS_ROOT": str(tmp_path), "HOME": str(tmp_path)}
    env.pop("AGENT_ENV", None)
    if agent_env is not None:
        env["AGENT_ENV"] = agent_env

    result = subprocess.run(
        ["bash", str(tmp_path / "bin" / script), *args],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode != 0
    assert "AGENT_ENV" in result.stderr
    assert "compose-env.sh" in result.stderr
    assert not marker.exists()
    assert legacy.read_text() == f'touch "{marker}"\nexit 0\n'


def test_master_uses_explicit_snapshot_for_the_query(tmp_path):
    snapshot = tmp_path / ".active.demo.env"
    snapshot.write_text('REPO=acme/demo\nPERMISSION_MODE=plan\n')
    claude = tmp_path / "claude"
    claude.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    claude.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "bin" / "master.sh"), "what is stuck?"],
        env={**os.environ, "AGENT_ENV": str(snapshot), "REPO": "wrong/repo",
             "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Repo: acme/demo" in result.stdout
    assert "what is stuck?" in result.stdout
    assert "--permission-mode\nplan\n" in result.stdout


@pytest.mark.parametrize("script", ["install-systemd.sh", "install-launchd.sh"])
def test_uninstall_all_does_not_need_config(tmp_path, script):
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("AGENT_ENV", None)
    result = subprocess.run(
        ["bash", str(ROOT / "bin" / script), "uninstall"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target", ["doctor", "install-systemd", "install-launchd", "demo-gate"])
@pytest.mark.parametrize("explicit_snapshot", [True, False])
def test_make_requires_and_passes_explicit_snapshot(tmp_path, target, explicit_snapshot):
    shutil.copy2(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "bin").mkdir()
    (tmp_path / "config").mkdir()
    active = tmp_path / "config" / ".active.demo.env"
    active.write_text('REPO=acme/demo\n')
    (tmp_path / "config" / "agent.env").write_text('REPO=wrong/repo\n')
    called = tmp_path / "called"
    (tmp_path / "bin" / f"{target}.sh").write_text(
        f'printf "%s\\n" "$AGENT_ENV" > "{called}"\n'
        '. "$AGENT_ENV"\nprintf "%s\\n" "$REPO"\n'
    )
    env = dict(os.environ)
    env.pop("AGENT_ENV", None)
    snapshot_arg = [f"AGENT_ENV={active}"] if explicit_snapshot else []
    result = subprocess.run(
        ["make", target, "DEMO_REPO=acme/demo", *snapshot_arg],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )

    if explicit_snapshot:
        assert result.returncode == 0, result.stderr
        assert called.read_text().strip() == str(active)
        assert "acme/demo" in result.stdout
    else:
        assert result.returncode != 0
        assert "AGENT_ENV" in result.stderr
        assert "compose-env.sh" in result.stderr
        assert not called.exists()


@pytest.mark.parametrize("agent_env", [None, "", "missing.env"])
def test_demo_gate_refuses_before_touching_the_demo_repo(tmp_path, agent_env):
    # Run directly, not via make: the gate creates and pushes a repository and
    # launches a daemon, so a missing snapshot must stop it before step 1.
    (tmp_path / "bin").mkdir()
    shutil.copy2(ROOT / "bin" / "demo-gate.sh", tmp_path / "bin" / "demo-gate.sh")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    touched = tmp_path / "touched"
    for tool in ("uv", "gh"):
        stub = stubs / tool
        stub.write_text(f'#!/bin/sh\necho {tool} >> "{touched}"\nprintf "1\\n"\n')
        stub.chmod(0o755)
    env = {**os.environ, "DEMO_REPO": "acme/demo",
           "PATH": f"{stubs}:{os.environ['PATH']}"}
    env.pop("AGENT_ENV", None)
    if agent_env is not None:
        env["AGENT_ENV"] = str(tmp_path / agent_env) if agent_env else ""

    result = subprocess.run(
        ["bash", str(tmp_path / "bin" / "demo-gate.sh")],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode != 0
    assert "AGENT_ENV" in result.stderr
    assert "GATE PASS" not in result.stdout
    assert "init demo target" not in result.stdout
    assert not touched.exists()
