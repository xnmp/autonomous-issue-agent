"""Reply recovery must return to the branch that contains blocked work."""

import asyncio
import subprocess
from pathlib import Path

from issue_agent.adapters.store import Store
from issue_agent.adapters.worktree import GitResult, Worktrees, _subprocess_git
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Comment, IssueView

from test_dispatch import FakeGateway, FakeRunner
from test_fix_workflow import _workflow
from test_worktree import FakeGit


def test_resume_recreates_removed_worktree_on_original_branch_with_blocked_commit(tmp_path):
    """A retitled blocked issue resumes its original branch, not `fix/N-issue`."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (repo / "base.txt").write_text("base\n")
    git("add", "base.txt")
    git("commit", "-qm", "base")
    git("branch", "-M", "dev")

    def worktree_git(args: list[str], cwd: str) -> GitResult:
        if args[:2] == ["fetch", "origin"]:
            return GitResult(0, "")
        return _subprocess_git(args, cwd)

    issue = 5
    original_branch = "fix/5-preserve-blocked-work"
    worktrees = Worktrees(str(repo), worktree_root="worktrees", run=worktree_git)
    original_cwd = Path(worktrees.ensure(issue, original_branch))
    (original_cwd / "blocked.txt").write_text("blocked work\n")
    git("add", "blocked.txt", cwd=original_cwd)
    git("commit", "-qm", "blocked work", cwd=original_cwd)
    blocked_commit = git("rev-parse", "HEAD", cwd=original_cwd)
    git("worktree", "remove", str(original_cwd), "--force")

    iss = IssueView(issue, title="Retitled after blocked work", body="body", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("alice", "continue", "t2"),
    ))
    gw = FakeGateway(issues={issue: iss}, pr_for_issue={})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s5"))
    # This represents a blocked leg created before branch persistence shipped.
    # Recovery must discover the retained branch rather than fall back to the
    # title-derived name, which is now different after the issue was retitled.
    store = Store(str(tmp_path / "state"))

    asyncio.run(_workflow(gw, store, runner, worktrees, fix_bot="fix-bot").resume(issue))

    recovered_cwd = Path(runner.tasks[0].cwd)
    assert git("branch", "--show-current", cwd=recovered_cwd) == original_branch
    assert git("rev-parse", "HEAD", cwd=recovered_cwd) == blocked_commit
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/fix/5-issue"], cwd=repo,
    ).returncode == 1


def test_resume_ignores_stale_synthesized_branch_when_recovering_blocked_work(tmp_path):
    """A stale fallback branch must not hide the original blocked-work branch."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (repo / "base.txt").write_text("base\n")
    git("add", "base.txt")
    git("commit", "-qm", "base")
    git("branch", "-M", "dev")

    def worktree_git(args: list[str], cwd: str) -> GitResult:
        if args[:2] == ["fetch", "origin"]:
            return GitResult(0, "")
        return _subprocess_git(args, cwd)

    issue = 5
    original_branch = "fix/5-preserve-blocked-work"
    stale_branch = "fix/5-retitled-after-blocked-work"
    worktrees = Worktrees(str(repo), worktree_root="worktrees", run=worktree_git)
    original_cwd = Path(worktrees.ensure(issue, original_branch))
    (original_cwd / "blocked.txt").write_text("blocked work\n")
    git("add", "blocked.txt", cwd=original_cwd)
    git("commit", "-qm", "blocked work", cwd=original_cwd)
    blocked_commit = git("rev-parse", "HEAD", cwd=original_cwd)
    git("worktree", "remove", str(original_cwd), "--force")
    git("branch", stale_branch, "dev")

    iss = IssueView(issue, title="Retitled after blocked work", body="body", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("alice", "continue", "t2"),
    ))
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s5"))
    workflow = _workflow(
        FakeGateway(issues={issue: iss}, pr_for_issue={}),
        Store(str(tmp_path / "state")),
        runner,
        worktrees,
        fix_bot="fix-bot",
    )

    asyncio.run(workflow.resume(issue))

    recovered_cwd = Path(runner.tasks[0].cwd)
    assert git("branch", "--show-current", cwd=recovered_cwd) == original_branch
    assert git("rev-parse", "HEAD", cwd=recovered_cwd) == blocked_commit


def test_resolve_persists_branch_for_future_reply_recovery(tmp_path):
    issue = 5
    title = "Preserve blocked work"
    gw = FakeGateway(issues={issue: IssueView(issue, title=title, body="body")}, pr_for_issue={})
    store = Store(str(tmp_path / "state"))
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner).resolve(issue))

    assert store.get_branch(issue) == "fix/5-preserve-blocked-work"


def test_branch_for_issue_recovers_a_unique_retained_branch():
    refs = GitResult(0, "\n".join(["dev", "fix/5-original-title", "fix/50-other-issue"]))

    assert Worktrees("/repo", run=FakeGit({"for-each-ref": refs})).branch_for_issue(5) == (
        "fix/5-original-title"
    )
