"""The type gate must not disappear inside an ignored linked-worktree path."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def _boolean_setting(config: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*=\s*(true|false)\s*$", config, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_explicit_pyrefly_inputs_survive_worktree_exclusions(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    config = (root / "pyproject.toml").read_text()
    use_ignore_files = _boolean_setting(config, "use-ignore-files")
    disable_exclude_heuristics = _boolean_setting(config, "disable-project-excludes-heuristics")
    nested = tmp_path / ".claude" / "worktrees" / "issue-1"
    source = nested / "src"
    source.mkdir(parents=True)
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    (git_info / "exclude").write_text(".claude/worktrees/\n")
    (nested / "pyproject.toml").write_text(
        "[tool.pyrefly]\n"
        'project-includes = ["src"]\n'
        f"disable-project-excludes-heuristics = {disable_exclude_heuristics}\n"
        f"use-ignore-files = {use_ignore_files}\n"
    )
    (source / "broken.py").write_text('def answer() -> int:\n    return "not an integer"\n')
    pyrefly = shutil.which("pyrefly")
    assert pyrefly is not None

    checked = subprocess.run([pyrefly, "check"], cwd=nested, capture_output=True, text=True, timeout=30)

    diagnostic = checked.stdout + checked.stderr
    assert checked.returncode != 0
    assert "broken.py" in diagnostic
    assert "bad-return" in diagnostic
