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


