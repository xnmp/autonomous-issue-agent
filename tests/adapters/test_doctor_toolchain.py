"""doctor.sh toolchain-pin drift check (#33).

TEST_CMD runs on the operator's local toolchain while CI resolves its own.
Observed on the xnmp/keifu preflight (2026-07-28): the local rustc was six
months stale (1.93.0) against CI's 1.97.1, and four clippy lints CI's
`-D warnings` caught did not exist locally. doctor warns when an ecosystem is
in use without a pin file, or when the pin differs from the tool on PATH.
These drive the REAL bin/doctor.sh against a scratch TARGET_DIR with fake
rustc/node/python3 binaries reporting a controlled version.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

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


# Realistic REPO/FIX_BOT/REVIEW_BOT keep the unrelated identity checks quiet.
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


_CARGO = {"Cargo.toml": '[package]\nname = "x"\n'}
_PIN = {"rust-toolchain.toml": '[toolchain]\nchannel = "1.97.1"\n'}
_PY = {"pyproject.toml": '[project]\nname = "x"\n'}


@pytest.mark.parametrize("files,tools,present,absent", [
    (_CARGO, {}, ["Cargo.toml present but no rust-toolchain.toml/rust-toolchain"], []),
    # The keifu incident: both versions are named.
    ({**_CARGO, **_PIN}, {"rustc": "1.93.0"}, ["rustc drift", "1.97.1", "1.93.0"], []),
    ({**_CARGO, **_PIN}, {"rustc": "1.97.1"}, ["rustc matches rust-toolchain.toml (1.97.1)"], ["rustc drift"]),
    # An unparseable pin stays silent rather than comparing nonsense.
    ({**_CARGO, "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n'}, {"rustc": "1.97.1"}, [],
     ["rustc drift", "rustc matches", "no rust-toolchain.toml/rust-toolchain"]),
    ({"package.json": "{}\n"}, {}, ["package.json present but no .nvmrc/.node-version"], []),
    ({"package.json": "{}\n", ".nvmrc": "18.17.0\n"}, {"node": "20.11.1"}, ["node drift", "18.17.0", "20.11.1"], []),
    ({"package.json": "{}\n", ".nvmrc": "20.11.1\n"}, {"node": "20.11.1"}, ["node matches .nvmrc (20.11.1)"],
     ["node drift"]),
    # A loose .nvmrc must not shadow a parseable .node-version beside it.
    ({"package.json": "{}\n", ".nvmrc": "lts/*\n", ".node-version": "20.11.1\n"}, {"node": "18.17.0"},
     ["node drift", "20.11.1", "18.17.0"], []),
    (_PY, {}, ["pyproject.toml/requirements.txt present but no .python-version"], []),
    ({"requirements.txt": "flask\n", ".python-version": "3.11.4\n"}, {"python3": "3.12.2"},
     ["python drift", "3.11.4", "3.12.2"], []),
    ({**_PY, ".python-version": "3.12.2\n"}, {"python3": "3.12.2"}, ["python3 matches .python-version (3.12.2)"],
     ["python drift"]),
    ({"README.md": "hello\n"}, {}, [], ["rust-toolchain", ".nvmrc", ".python-version"]),
])
def test_doctor_toolchain_pins(tmp_path, files, tools, present, absent):
    out = _toolchain_doctor(tmp_path, files, **tools).stdout
    assert all(text in out for text in present), out
    assert not any(text in out for text in absent), out
