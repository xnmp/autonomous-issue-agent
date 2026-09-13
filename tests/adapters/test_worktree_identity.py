"""Real Git contracts for retained work and branch-bound publication."""

import subprocess

import pytest

from issue_agent.adapters.accounting_runtime import _fix_publication_context
from issue_agent.adapters.worktree import Worktrees, WorktreeUnavailable
from issue_agent.domain.refs import BRANCH_PREFIXES, issue_from_branch


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "dev")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "base")
    git(tmp_path, "remote", "add", "origin", str(tmp_path))
    return tmp_path


def test_existing_test_branch_keeps_its_work_and_supplies_publication_authority(repo):
    worktrees = Worktrees(str(repo))
    path = repo / worktrees.path(703)
    branch = "test/703-native-webdriver-session-loss"
    git(repo, "worktree", "add", "-b", branch, str(path))
    (path / "diagnostic.txt").write_text("retained diagnostic work")
    original_head = git(path, "rev-parse", "HEAD")

    recovered = worktrees.branch_for_issue(703, "fix/703-old-title")
    prepared = worktrees.ensure(703, recovered)

    assert recovered == branch
    assert prepared == str(path)
    assert git(path, "rev-parse", "HEAD") == original_head
    assert (path / "diagnostic.txt").read_text() == "retained diagnostic work"
    authority = _fix_publication_context("fix", prepared)
    assert authority is not None and authority.branch == branch


@pytest.mark.parametrize("branch", ["fix/704-other", "dev"])
def test_foreign_checkout_is_not_adopted_or_removed(repo, branch):
    worktrees = Worktrees(str(repo))
    path = repo / worktrees.path(703)
    if branch == "dev":
        git(repo, "worktree", "add", "--detach", str(path))
    else:
        git(repo, "worktree", "add", "-b", branch, str(path))
    original_head = git(path, "rev-parse", "HEAD")

    with pytest.raises(WorktreeUnavailable):
        worktrees.branch_for_issue(703, "fix/703-fallback")
    with pytest.raises(WorktreeUnavailable):
        worktrees.ensure(703, "fix/703-required")
    with pytest.raises(WorktreeUnavailable):
        worktrees.remove(703)

    assert path.exists()
    assert git(path, "rev-parse", "HEAD") == original_head


def test_authoritative_pr_branch_does_not_adopt_another_branch(repo):
    worktrees = Worktrees(str(repo))
    path = repo / worktrees.path(703)
    git(repo, "worktree", "add", "-b", "test/703-retained", str(path))

    with pytest.raises(WorktreeUnavailable, match="requires branch"):
        worktrees.ensure(703, "fix/703-published-pr")
    assert git(path, "branch", "--show-current") == "test/703-retained"


def test_same_directory_basename_outside_configured_root_is_not_reused(repo):
    worktrees = Worktrees(str(repo))
    foreign = repo / "unrelated" / "issue-703"
    git(repo, "worktree", "add", "-b", "fix/704-other", str(foreign))

    prepared = worktrees.ensure(703, "fix/703-new")

    assert prepared == str(repo / worktrees.path(703))
    assert git(prepared, "branch", "--show-current") == "fix/703-new"
    assert git(foreign, "branch", "--show-current") == "fix/704-other"


@pytest.mark.parametrize("prefix", BRANCH_PREFIXES)
def test_retained_branch_recovery_uses_the_shared_issue_policy(repo, prefix):
    branch = f"{prefix}/703-retained"
    git(repo, "branch", branch)
    git(repo, "branch", f"{prefix}/7030-unrelated")

    assert Worktrees(str(repo)).branch_for_issue(703, "fix/703-fallback") == branch
    assert issue_from_branch(branch) == 703


def test_persisted_branch_is_preferred_over_an_unrelated_retained_attempt(repo):
    git(repo, "branch", "test/703-real")
    git(repo, "branch", "fix/703-old")
    assert Worktrees(str(repo)).branch_for_issue(
        703, "fix/703-new-title", preferred="test/703-real"
    ) == "test/703-real"
