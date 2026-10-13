"""doctor.sh toolchain-pin drift check (#33).

TEST_CMD (the harness's gate) runs on the operator's local toolchain; CI
resolves its own; nothing compares them, so a locally-green gate can be
systematically weaker than CI's. Observed hard on the xnmp/keifu preflight
(2026-07-28): the operator's rustc was six months stale (1.93.0) while CI's
resolved 1.97.1, and four clippy lints CI's `-D warnings` caught did not exist
in the stale local toolchain — `cargo clippy` was clean locally and red in CI
on identical source.

`bin/doctor.sh` gains a `== toolchain pin (drift vs CI) ==` section: for
Rust/Node/Python, warn when the ecosystem is in use but has no toolchain-pin
file at all (guarantees drift over time), and warn when a pin file's exact
version differs from the tool actually on PATH (the keifu incident class).
Entirely local — no network — so these drive the REAL bin/doctor.sh via
subprocess against a scratch TARGET_DIR with a stubbed PATH carrying fake
rustc/node/python3 binaries that report a controlled version, mirroring the
`_ci_doctor`/`_fake_ci_tools` pattern in test_compose_env.py.

A NEW file rather than an addition to test_compose_env.py: the PR gate blocks
any diff to a test file that already exists at the base ref (only new test
files may be added), so `_run_doctor` et al. are duplicated here in minimal
form rather than modifying that file.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SH = ROOT / "bin" / "doctor.sh"


def _run_doctor(env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    if "AGENT_ENV" not in env:
        empty = Path(tempfile.mkdtemp(prefix="doctor-cfg-")) / "empty.env"
        empty.write_text("")
        full_env["AGENT_ENV"] = str(empty)
    return subprocess.run(
        ["bash", str(DOCTOR_SH)],
        env=full_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── #33: toolchain-pin drift ────────────────────────────────────────────────
# `REPO`/`FIX_BOT`/`REVIEW_BOT` are given realistic values so the unrelated
# identity/CI-access checks stay quiet (warnings only) and don't add noise to
# the specific substrings asserted here.
def _fake_toolchain_bin(
    tmp_path: Path,
    *,
    rustc: str | None = None,
    node: str | None = None,
    python3: str | None = None,
) -> Path:
    fake_bin = tmp_path / "fake_toolchain_bin"
    fake_bin.mkdir()
    scripts = {
        "gh": (
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "--version" ]; then echo "gh version 0.0.0-fake"; exit 0; fi\n'
            'if [ "$1" = "api" ] && [ "$2" = "user" ]; then echo "fake-user"; exit 0; fi\n'
            'if [ "$1" = "repo" ] && [ "$2" = "view" ]; then exit 0; fi\n'
            'exit 0\n'
        ),
        "claude": '#!/usr/bin/env bash\necho "claude 0.0.0-fake"\n',
        "jq": '#!/usr/bin/env bash\necho "jq-0.0.0-fake"\n',
        "uv": (
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "--version" ]; then echo "uv 0.0.0-fake"; exit 0; fi\n'
            'exit 2\n'
        ),
    }
    if rustc is not None:
        scripts["rustc"] = f'#!/usr/bin/env bash\necho "rustc {rustc} (abcdef123 2026-01-01)"\n'
    if node is not None:
        scripts["node"] = f'#!/usr/bin/env bash\necho "v{node}"\n'
    if python3 is not None:
        scripts["python3"] = f'#!/usr/bin/env bash\necho "Python {python3}"\n'
    for name, body in scripts.items():
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _target_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    for rel, content in files.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return target


def _toolchain_doctor(
    tmp_path: Path, files: dict[str, str], **tool_versions
) -> subprocess.CompletedProcess:
    fake_bin = _fake_toolchain_bin(tmp_path, **tool_versions)
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True, exist_ok=True)
    target = _target_dir(tmp_path, files)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
        "REPO": "acme/widget",
        "FIX_BOT": "acme-fixer",
        "REVIEW_BOT": "acme-reviewer",
        "TARGET_DIR": str(target),
    }
    return _run_doctor(env)


def test_doctor_warns_when_rust_toolchain_pin_is_missing(tmp_path):
    result = _toolchain_doctor(tmp_path, {"Cargo.toml": "[package]\nname = \"x\"\n"})
    assert "Cargo.toml present but no rust-toolchain.toml/rust-toolchain" in result.stdout


def test_doctor_warns_on_rustc_version_drift(tmp_path):
    """The exact keifu incident: a stale local rustc missed clippy lints CI's
    pinned/resolved (newer) toolchain caught. Must name BOTH versions."""
    result = _toolchain_doctor(
        tmp_path,
        {
            "Cargo.toml": "[package]\nname = \"x\"\n",
            "rust-toolchain.toml": '[toolchain]\nchannel = "1.97.1"\n',
        },
        rustc="1.93.0",
    )
    assert "rustc drift" in result.stdout
    assert "1.97.1" in result.stdout
    assert "1.93.0" in result.stdout


def test_doctor_quiet_when_rustc_matches_pin(tmp_path):
    """Anti-vacuity twin: the healthy case must not warn, and should say so."""
    result = _toolchain_doctor(
        tmp_path,
        {
            "Cargo.toml": "[package]\nname = \"x\"\n",
            "rust-toolchain.toml": '[toolchain]\nchannel = "1.97.1"\n',
        },
        rustc="1.97.1",
    )
    assert "rustc drift" not in result.stdout
    assert "rustc matches rust-toolchain.toml (1.97.1)" in result.stdout


def test_doctor_quiet_on_unparseable_rust_pin(tmp_path):
    """A heuristic that guessed at "channel = \"stable\"" would train operators
    to ignore the line — it must stay silent rather than compare nonsense."""
    result = _toolchain_doctor(
        tmp_path,
        {
            "Cargo.toml": "[package]\nname = \"x\"\n",
            "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n',
        },
        rustc="1.97.1",
    )
    assert "rustc drift" not in result.stdout
    assert "rustc matches" not in result.stdout
    assert "no rust-toolchain.toml/rust-toolchain" not in result.stdout


def test_doctor_warns_when_node_pin_is_missing(tmp_path):
    result = _toolchain_doctor(tmp_path, {"package.json": "{}\n"})
    assert "package.json present but no .nvmrc/.node-version" in result.stdout


def test_doctor_warns_on_node_version_drift(tmp_path):
    result = _toolchain_doctor(
        tmp_path,
        {"package.json": "{}\n", ".nvmrc": "18.17.0\n"},
        node="20.11.1",
    )
    assert "node drift" in result.stdout
    assert "18.17.0" in result.stdout
    assert "20.11.1" in result.stdout


def test_doctor_quiet_when_node_matches_pin(tmp_path):
    result = _toolchain_doctor(
        tmp_path,
        {"package.json": "{}\n", ".nvmrc": "20.11.1\n"},
        node="20.11.1",
    )
    assert "node drift" not in result.stdout
    assert "node matches .nvmrc (20.11.1)" in result.stdout


def test_doctor_falls_back_to_node_version_when_nvmrc_is_unparseable(tmp_path):
    """A loose .nvmrc ("lts/*") must not shadow a parseable .node-version
    sitting right next to it — the check should still find something to
    compare against instead of going silent just because the FIRST candidate
    file it looked at happened to be unparseable."""
    result = _toolchain_doctor(
        tmp_path,
        {"package.json": "{}\n", ".nvmrc": "lts/*\n", ".node-version": "20.11.1\n"},
        node="18.17.0",
    )
    assert "node drift" in result.stdout
    assert "20.11.1" in result.stdout
    assert "18.17.0" in result.stdout


def test_doctor_warns_when_python_pin_is_missing(tmp_path):
    result = _toolchain_doctor(tmp_path, {"pyproject.toml": "[project]\nname = \"x\"\n"})
    assert "pyproject.toml/requirements.txt present but no .python-version" in result.stdout


def test_doctor_warns_on_python_version_drift(tmp_path):
    result = _toolchain_doctor(
        tmp_path,
        {"requirements.txt": "flask\n", ".python-version": "3.11.4\n"},
        python3="3.12.2",
    )
    assert "python drift" in result.stdout
    assert "3.11.4" in result.stdout
    assert "3.12.2" in result.stdout


def test_doctor_quiet_when_python_matches_pin(tmp_path):
    result = _toolchain_doctor(
        tmp_path,
        {"pyproject.toml": "[project]\nname = \"x\"\n", ".python-version": "3.12.2\n"},
        python3="3.12.2",
    )
    assert "python drift" not in result.stdout
    assert "python3 matches .python-version (3.12.2)" in result.stdout


def test_doctor_silent_on_toolchain_checks_when_no_ecosystem_marker_present(tmp_path):
    """No Cargo.toml/package.json/pyproject.toml/requirements.txt at all — none
    of the three checks should fire anything, missing-pin or otherwise."""
    result = _toolchain_doctor(tmp_path, {"README.md": "hello\n"})
    assert "rust-toolchain" not in result.stdout
    assert ".nvmrc" not in result.stdout
    assert ".python-version" not in result.stdout
