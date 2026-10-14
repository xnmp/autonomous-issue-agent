"""Executable registry of accepted residuals.

Every test states the desired closed behavior and is a strict expected failure
while the matching entry remains accepted in ``docs/open/residuals.md``. A fix
therefore becomes an XPASS and fails the suite until code and registry move
together.

CRED-HOME (a leg reading the operator's HOME) closed with the leg sandbox
(#413); `tests/adapters/test_leg_sandbox.py` asserts it on a real sandbox.
GIT-OBJECTS (a fix leg writing the object store review checkouts read) closed
with per-leg fix clones; `tests/domain/test_sandbox.py` asserts it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from issue_agent.adapters.worktree import Worktrees

_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **_IDENTITY}).stdout.strip()


@pytest.mark.xfail(strict=True, reason="BORROWED-OBJECTS: a leg's own fetch can adopt an object the repository "
                                       "holds under no ref, which the repository's gc then deletes")
def test_a_fix_clone_survives_the_repositorys_gc_after_its_leg_fetched(tmp_path: Path) -> None:
    upstream, target, other = tmp_path / "upstream.git", tmp_path / "target", tmp_path / "other"
    _git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(upstream))
    _git(tmp_path, "init", "-q", "-b", "dev", str(target))
    _git(target, "commit", "--allow-empty", "-m", "base")
    _git(target, "remote", "add", "origin", f"file://{upstream}")
    _git(target, "push", "-q", "origin", "dev")
    clone = Worktrees(str(target)).ensure(5, "fix/5-x")
    _git(clone, "push", "-q", "origin", "fix/5-x")
    # Someone else pushes to the PR branch; a review fetches that head under no ref.
    _git(tmp_path, "clone", "-q", "-b", "fix/5-x", f"file://{upstream}", str(other))
    _git(other, "commit", "--allow-empty", "-m", "pushed by another")
    _git(other, "push", "-q", "origin", "fix/5-x")
    _git(target, "fetch", "-q", "origin", _git(other, "rev-parse", "HEAD"))
    (target / ".git/FETCH_HEAD").unlink()

    _git(clone, "pull", "-q", "--ff-only", "origin", "fix/5-x")  # the leg's own fetch, mid-leg
    _git(target, "gc", "-q", "--prune=now")

    assert _git(clone, "log", "-1", "--format=%s") == "pushed by another"
