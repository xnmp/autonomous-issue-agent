"""bin/install.sh must shim EVERY declared console script (#156).

`pyproject.toml` declares two entry points; the installer wrote exactly one
hand-named shim (`$BIN_DIR/issue-agent`). So on any box bootstrapped with
`make install`, `issue-agent-publish` — the executable every fixer is told to publish
through — was simply absent from PATH, and issue #18's
resolve leg blocked at $1.84 asking the operator to install it (keifu,
2026-08-02).

The list is now DERIVED from the keys of pyproject's `[project.scripts]`
table, so a new entry point cannot drift out of the installer. These tests
drive the REAL bin/install.sh from a scratch checkout (its `dirname $0/..`
self-location makes that a copy of the script beside a scratch pyproject) with
a fake `uv` on PATH, so nothing here syncs a venv or touches the operator's
~/.local/bin. The scratch pyproject deliberately declares a THIRD script the
real one does not: an assertion naming only the two current entry points would
pass against a hand-maintained list.

The doctor half drives the real bin/doctor.sh with a stubbed PATH, mirroring
the `_fake_bin`/`_run_doctor` pattern in test_doctor_write_probe.py.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "bin" / "install.sh"
DOCTOR_SH = ROOT / "bin" / "doctor.sh"

SCRATCH_PYPROJECT = """\
[project]
name = "issue-agent"
version = "0.0.0"

[project.scripts]
issue-agent = "issue_agent.app.cli:main"
issue-agent-publish = "issue_agent.adapters.agent_publish:main"
issue-agent-later = "issue_agent.app.later:main"

[tool.something]
not-a-script = "must not be shimmed"
"""


def _base_path() -> str:
    """The ambient PATH minus any directory that already carries an
    `issue-agent*` executable. Hard-coding /usr/bin:/bin is not portable (the
    NixOS deployment box has neither), and inheriting PATH verbatim would let
    the operator's own ~/.local/bin shims decide what these tests observe."""
    kept = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not list(Path(d).glob("issue-agent*"))
    ]
    return os.pathsep.join(kept)


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _scratch_install(tmp_path: Path, *, materialise: tuple[str, ...]) -> Path:
    """A scratch harness root: bin/install.sh + a pyproject, and a fake `uv`
    whose `sync` materialises exactly `materialise` into <root>/.venv/bin."""
    root = tmp_path / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "install.sh").write_text(INSTALL_SH.read_text())
    (root / "bin" / "install.sh").chmod(0o755)
    (root / "pyproject.toml").write_text(SCRATCH_PYPROJECT)

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    names = " ".join(materialise)
    _exe(
        fake_bin / "uv",
        "#!/usr/bin/env bash\n"
        'if [ "$1" != "sync" ]; then exit 0; fi\n'
        'mkdir -p "$PWD/.venv/bin"\n'
        f"for n in {names}; do\n"
        '  printf \'#!/usr/bin/env bash\\necho "venv %s ran: $*"\\n\' "$n" '
        '> "$PWD/.venv/bin/$n"\n'
        '  chmod +x "$PWD/.venv/bin/$n"\n'
        "done\n",
    )
    for stub in ("git", "gh"):
        _exe(fake_bin / stub, "#!/usr/bin/env bash\nexit 0\n")
    return root


def _run_install(root: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{root.parent / 'fake_bin'}{os.pathsep}{_base_path()}"
    env["ISSUE_AGENT_BIN_DIR"] = str(bin_dir)
    return subprocess.run(
        ["bash", str(root / "bin" / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


DECLARED = ("issue-agent", "issue-agent-publish", "issue-agent-later")


def test_install_shims_every_declared_console_script(tmp_path):
    root = _scratch_install(tmp_path, materialise=DECLARED)
    bin_dir = tmp_path / "bin_dir"

    result = _run_install(root, bin_dir)

    assert result.returncode == 0, result.stderr + result.stdout
    for name in DECLARED:
        shim = bin_dir / name
        assert shim.exists(), f"{name} was not shimmed: {result.stdout}"
        assert os.access(shim, os.X_OK)
        # Behaviour, not text: the shim must actually reach that name's venv
        # entry point, with the checkout baked in and arguments passed through.
        ran = subprocess.run(
            [str(shim), "--dry-run"], capture_output=True, text=True, timeout=30
        )
        assert ran.stdout.strip() == f"venv {name} ran: --dry-run"
    assert not (bin_dir / "not-a-script").exists()


def test_install_overwrites_a_hand_written_operator_shim(tmp_path):
    """The #156 stopgap was a hand-written ~/.local/bin/issue-agent-publish.
    Re-running the installer must replace it, not skip or fail on it."""
    root = _scratch_install(tmp_path, materialise=DECLARED)
    bin_dir = tmp_path / "bin_dir"
    bin_dir.mkdir()
    _exe(bin_dir / "issue-agent-publish", "#!/usr/bin/env bash\necho stopgap\n")

    assert _run_install(root, bin_dir).returncode == 0
    # Idempotent: a second run over its own output is still clean.
    assert _run_install(root, bin_dir).returncode == 0

    ran = subprocess.run(
        [str(bin_dir / "issue-agent-publish"), "x"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.stdout.strip() == "venv issue-agent-publish ran: x"


def test_install_fails_loudly_when_a_declared_script_is_missing(tmp_path):
    """Deriving the list is only safe if a declaration the sync did not
    materialise stops the install instead of shipping a dangling shim."""
    root = _scratch_install(
        tmp_path, materialise=("issue-agent", "issue-agent-publish")
    )
    bin_dir = tmp_path / "bin_dir"

    result = _run_install(root, bin_dir)

    assert result.returncode != 0
    assert "issue-agent-later" in result.stdout + result.stderr


# ── doctor: the broken-installer signature ──────────────────────────────────
def _run_doctor(path_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{path_dir}{os.pathsep}{_base_path()}"
    empty = Path(tempfile.mkdtemp(prefix="doctor-cfg-")) / "empty.env"
    empty.write_text("")
    env["AGENT_ENV"] = str(empty)
    return subprocess.run(
        ["bash", str(DOCTOR_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _doctor_path(tmp_path: Path, *, present: tuple[str, ...]) -> Path:
    stub_bin = tmp_path / "doctor_bin"
    stub_bin.mkdir()
    for stub in ("claude", "gh", "jq", "git", *present):
        _exe(stub_bin / stub, "#!/usr/bin/env bash\nexit 0\n")
    return stub_bin


def test_doctor_flags_issue_agent_without_its_publish_sibling(tmp_path):
    out = _run_doctor(_doctor_path(tmp_path, present=("issue-agent",))).stdout

    assert "issue-agent-publish not on PATH" in out
    assert "make install" in out


def test_doctor_is_quiet_when_both_entry_points_resolve(tmp_path):
    out = _run_doctor(
        _doctor_path(tmp_path, present=("issue-agent", "issue-agent-publish"))
    ).stdout

    assert "issue-agent-publish on PATH" in out
    assert "not on PATH" not in out.split("== config ==")[0]


def test_doctor_says_nothing_about_shims_in_a_plain_dev_checkout(tmp_path):
    """A checkout that never ran `make install` has neither executable — that
    is not a broken install and must not be reported as one."""
    out = _run_doctor(_doctor_path(tmp_path, present=())).stdout

    assert "issue-agent-publish" not in out
