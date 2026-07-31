"""The type gate must not disappear inside an ignored linked-worktree path."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def test_explicit_pyrefly_inputs_survive_parent_git_excludes(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    match = re.search(
        r"^use-ignore-files\s*=\s*(true|false)\s*$",
        (root / "pyproject.toml").read_text(),
        re.MULTILINE,
    )
    assert match is not None
    nested = tmp_path / "nested" / "issue-1"
    source = nested / "src"
    source.mkdir(parents=True)
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    (git_info / "exclude").write_text("nested/\n")
    (nested / "pyproject.toml").write_text(
        "[tool.pyrefly]\n"
        'project-includes = ["src"]\n'
        f"use-ignore-files = {match.group(1)}\n"
    )
    (source / "broken.py").write_text(
        "def answer() -> int:\n"
        '    return "not an integer"\n'
    )
    pyrefly = shutil.which("pyrefly")
    assert pyrefly is not None

    checked = subprocess.run(
        [pyrefly, "check"],
        cwd=nested,
        capture_output=True,
        text=True,
        timeout=30,
    )

    diagnostic = checked.stdout + checked.stderr
    assert checked.returncode != 0
    assert "broken.py" in diagnostic
    assert "bad-return" in diagnostic
