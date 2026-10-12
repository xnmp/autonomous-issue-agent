"""The composed fix role (slice 5b): production wiring, real SQLite, fake GitHub.

Each test ticks `compose_fix_daemon` against an in-memory GitHub that applies
writes the way GitHub does, and asserts on what GitHub ends up holding (PRs,
labels, comments, merges) and on what the agent was asked to do.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.config import from_env
from issue_agent.app.roles import compose_fix_daemon
from issue_agent.domain.actions import AgentKind, MergePullRequest, OpenPullRequest, RunAgent
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.state import StopReason, stop_tag
from issue_agent.domain.world import Branch, Comment, IssueView, PRView, Review, WorldState

FIX_BOT, REVIEW_BOT = "fix-bot", "review-bot"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ci(head: str, bucket: str) -> Observed:
    state = "IN_PROGRESS" if bucket == "pending" else "COMPLETED"
    return Observed(head, head, frozenset({RequiredCheck("test")}),
                    (Check("test", state=state, bucket=bucket, required=True),))


@dataclass
class FakeGitHub:
    """Just enough GitHub for the fix role: issues, harness PRs, pushed
    branches, labels and comments. Writes land the way GitHub applies them."""

    issues: dict[int, IssueView] = field(default_factory=dict)
    prs: dict[int, PRView] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)  # name -> pushed head
    merged: list[tuple[int, str]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    lose_reply: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ── observation ──
    def build_fix_world(self) -> WorldState:
        with self.lock:
            with_pr = {p.head_ref_name for p in self.prs.values()}
            return WorldState(
                issues=tuple(i for i in self.issues.values() if i.state == "open"),
                prs=tuple(self.prs.values()),
                branches=tuple(Branch(b, _now()) for b in self.branches if b not in with_pr),
            )

    def get_issue(self, number: int) -> IssueView:
        return self.issues[number]

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        return self.prs[number]

    def pull_request_for_branch(self, branch: str) -> int | None:
        return next((n for n, p in self.prs.items() if p.head_ref_name == branch), None)

    def has_label(self, kind: str, number: int, label: str) -> bool:
        subject = self.issues[number] if kind == "issue" else self.prs[number]
        return any(lab.value == label for lab in subject.labels)

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        subject = self.issues[number] if kind == "issue" else self.prs[number]
        return any(c.body == body for c in subject.comments)

    def is_issue_closed(self, issue: int) -> bool:
        return self.issues[issue].state != "open"

    def failed_run_log(self, head: str, run_ids) -> str:
        return "AssertionError: expected 2, got 3"

    def review_comments(self, pr: int) -> str:
        return ""

    # ── writes ──
    def _lossy(self, name: str) -> None:
        if name in self.lose_reply:
            self.lose_reply.discard(name)
            raise RuntimeError(f"{name}: reply lost after the write landed")

    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        store = self.issues if kind == "issue" else self.prs
        labels = {lab for lab in store[number].labels if lab.value != remove}
        if add is not None:
            labels.add(Label(add))
        store[number] = replace(store[number], labels=frozenset(labels))

    def comment(self, kind: str, number: int, body: str, *, author: str = FIX_BOT) -> None:
        store = self.issues if kind == "issue" else self.prs
        store[number] = replace(store[number], comments=(*store[number].comments, Comment(author, body, _now())))
        self._lossy("comment")

    def open_pull_request(self, branch: str, base: str, title: str, body: str) -> None:
        with self.lock:
            if self.pull_request_for_branch(branch) is not None:
                raise RuntimeError("a pull request already exists for this branch")
            number = 100 + len(self.prs)
            head = self.branches[branch]
            self.prs[number] = PRView(
                number=number, head_ref_name=branch, head_ref_oid=head, state="OPEN", title=title, body=body,
                author=FIX_BOT, ci=_ci(head, "pending"), mergeable="MERGEABLE", merge_state="CLEAN",
            )
        self._lossy("open_pull_request")

    def enable_auto_merge(self, pr: int, method: str, expected_head: str) -> bool:
        if self.prs[pr].head_ref_oid != expected_head:
            return False
        self.merged.append((pr, expected_head))
        self.prs[pr] = replace(self.prs[pr], state="MERGED")
        return True

    def update_branch(self, pr: int, expected_head: str) -> bool:
        return self.prs[pr].head_ref_oid == expected_head

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self.closed.append(number)
        self.issues[number] = replace(self.issues[number], state="closed")

    # ── test helpers ──
    def push(self, branch: str, head: str, ci: str = "pending") -> None:
        """An agent's `git push`: moves the branch, and the PR head if one exists."""
        with self.lock:
            self.branches[branch] = head
            for number, p in self.prs.items():
                if p.head_ref_name == branch and p.state == "OPEN":
                    self.prs[number] = replace(p, head_ref_oid=head, ci=_ci(head, ci), reviews=(),
                                               mergeable="MERGEABLE", merge_state="CLEAN")

    def comments_with(self, kind: str, number: int, text: str) -> list[Comment]:
        subject = self.issues[number] if kind == "issue" else self.prs[number]
        return [c for c in subject.comments if text in c.body]


class FakeWorktrees:
    def __init__(self, root: Path):
        self.root = root
        self.removed: list[int] = []

    def ensure(self, issue: int, branch: str) -> str:
        path = self.root / f"issue-{issue}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def sync(self, issue: int, branch: str, head: str) -> str:
        return self.ensure(issue, branch)

    def branch_for_issue(self, issue: int, fallback: str, *, preferred: str = "") -> str:
        return preferred or fallback

    def remove(self, issue: int) -> None:
        self.removed.append(issue)

    def prune_validate(self) -> int:
        return 0


class Agent:
    """A scripted agent: each call runs the next step with the task, and the
    step's return value is the leg's result. A step may raise (a transport
    death) or block on an event."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        step = self.steps.pop(0)
        return await asyncio.to_thread(step, task)


def pushed(gh: FakeGitHub, branch: str, head: str, ci: str = "pending", session: str = "s1"):
    def step(_task):
        gh.push(branch, head, ci)
        return AgentResult(False, status="pushed", summary="Fixed the null token.", cost_usd=0.1, session_id=session)
    return step


def did_nothing(session: str = "s1"):
    return lambda _task: AgentResult(False, status="pushed", cost_usd=0.1, session_id=session)


def died(_task):
    raise ConnectionError("stream reset by peer")


def asked(needs: str, session: str = "s1"):
    return lambda _task: AgentResult(False, status="blocked", needs=needs, cost_usd=0.1, session_id=session)


@pytest.fixture
def role(tmp_path):
    def compose(gh: FakeGitHub, agent: Agent, **env):
        cfg = replace(
            from_env({
                "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEW_BOT": REVIEW_BOT, "BASE_BRANCH": "dev",
                "STATE_DIR": str(tmp_path / "state"), "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
                **env,
            }),
            control_authority_root=str(tmp_path / "machine-state"),
        )
        worktrees = FakeWorktrees(tmp_path / "worktrees")
        composed = compose_fix_daemon(cfg, gh, Store(cfg.state_dir), worktrees, agent, log=lambda _line: None)
        return composed, worktrees
    return compose


def tick(composed) -> list:
    taken = composed.daemon.tick()
    composed.daemon.wait_for_dispatches()
    return taken


def dispatched(taken, kind: AgentKind) -> list[RunAgent]:
    return [a for a in taken if isinstance(a, RunAgent) and a.kind is kind]


def ready(number: int = 5, title: str = "Null token crashes") -> IssueView:
    return IssueView(number=number, title=title, body="Steps: ...", labels=frozenset({Label.AI_READY}))


def open_pr(gh: FakeGitHub, issue: int = 5, *, head: str = "h1", ci: str = "pass", **kw) -> int:
    branch = f"fix/{issue}-null-token-crashes"
    gh.branches[branch] = head
    gh.prs[100] = PRView(number=100, head_ref_name=branch, head_ref_oid=head, state="OPEN", author=FIX_BOT,
                         ci=_ci(head, ci), mergeable=kw.pop("mergeable", "MERGEABLE"),
                         merge_state=kw.pop("merge_state", "CLEAN"), **kw)
    return 100


# ── RESOLVE → push → PR (Q11) ────────────────────────────────────────────────

def test_resolve_runs_once_under_its_lease_then_the_harness_opens_one_pr(role):
    gh = FakeGitHub(issues={5: ready()})
    release = threading.Event()
    started = threading.Event()

    def resolve(task):
        started.set()
        assert release.wait(timeout=10)
        gh.push("fix/5-null-token-crashes", "h1")
        return AgentResult(False, status="pushed", summary="Fixed the null token.", cost_usd=0.1, session_id="s1")

    agent = Agent(resolve)
    composed, _ = role(gh, agent)
    first = composed.daemon.tick()
    assert dispatched(first, AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, head="")]
    assert started.wait(timeout=10)
    assert composed.daemon.tick() == []  # the leg holds its item: no second RESOLVE
    release.set()
    composed.daemon.wait_for_dispatches()

    assert OpenPullRequest(5, "fix/5-null-token-crashes") in tick(composed)
    assert tick(composed) == []  # CI pending on the new PR: wait
    assert [p.head_ref_name for p in gh.prs.values()] == ["fix/5-null-token-crashes"]
    pr = gh.prs[100]
    assert "Fixed the null token." in pr.body and "#5" in pr.body
    task = agent.tasks[0]
    assert "git push -u origin fix/5-null-token-crashes" in task.prompt
    assert task.env["GIT_AUTHOR_EMAIL"] == "fix-bot@users.noreply.github.com"


def test_a_crash_between_push_and_pr_open_yields_exactly_one_pr(role):
    gh = FakeGitHub(issues={5: ready()})
    composed, _ = role(gh, Agent(pushed(gh, "fix/5-null-token-crashes", "h1")))
    tick(composed)  # RESOLVE pushes, then the daemon "crashes" before the next tick

    gh.lose_reply.add("open_pull_request")  # and the restarted daemon's open loses its reply
    restarted, _ = role(gh, Agent())
    tick(restarted)
    tick(restarted)
    tick(restarted)

    assert len(gh.prs) == 1 and gh.prs[100].head_ref_name == "fix/5-null-token-crashes"


# ── PR-stage legs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,shape", [
    (AgentKind.FIX_CI, dict(ci="fail")),
    (AgentKind.REBASE, dict(mergeable="CONFLICTING", merge_state="DIRTY")),
    (AgentKind.ADDRESS_REVIEW, dict(reviews=(Review("CHANGES_REQUESTED", REVIEW_BOT, body="Handle None.",
                                                    submitted_at="2026-10-01T00:00:00Z", commit_id="h1"),))),
])
def test_each_pr_state_dispatches_its_leg_bound_to_the_head_and_resumes_the_session(role, kind, shape):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(pushed(gh, "fix/5-null-token-crashes", "h1", session="s-resolve"),
                  pushed(gh, "fix/5-null-token-crashes", "h2", session="s-resolve"))
    composed, _ = role(gh, agent)
    tick(composed)
    tick(composed)  # opens the PR
    shape = dict(shape)
    gh.prs[100] = replace(gh.prs[100], ci=_ci("h1", shape.pop("ci", "pass")), **shape)

    assert dispatched(tick(composed), kind) == [RunAgent(kind, 100, head="h1")]
    task = agent.tasks[1]
    assert task.resume == "s-resolve"
    assert gh.prs[100].head_ref_oid == "h2"


def test_three_unproductive_attempts_stop_with_one_label_and_one_comment(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(did_nothing(), did_nothing(), did_nothing())
    composed, _ = role(gh, agent)
    for _ in range(3):
        assert dispatched(tick(composed), AgentKind.FIX_CI)

    tick(composed)
    tick(composed)
    tick(composed)

    assert Label.NEEDS_HUMAN in gh.prs[100].labels
    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1
    assert len(agent.tasks) == 3


def test_a_machine_death_is_no_attempt_and_backs_off(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(died))
    assert dispatched(tick(composed), AgentKind.FIX_CI)

    assert tick(composed) == []  # backing off: no redispatch, no stop
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels
    runs = composed.control.fix_runs("o/r", (5,))
    assert len(runs) == 1 and runs[0].receipt is None


def test_an_approved_green_head_merges_bound_to_that_head_and_closes_the_issue(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=(Review("APPROVED", REVIEW_BOT, submitted_at="2026-10-01T00:00:00Z", commit_id="h1"),))
    composed, worktrees = role(gh, Agent())

    assert MergePullRequest(100, "h1") in tick(composed)
    assert gh.merged == [(100, "h1")]
    assert gh.closed == [5] and worktrees.removed == [5]
    assert tick(composed) == []  # merged: neither the PR nor its issue is an item


def test_auto_merge_off_leaves_an_approved_pr_for_a_human(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=(Review("APPROVED", REVIEW_BOT, submitted_at="2026-10-01T00:00:00Z", commit_id="h1"),))
    composed, _ = role(gh, Agent(), AUTO_MERGE="false")

    assert tick(composed) == []
    assert gh.merged == []


def test_a_lost_stop_comment_reply_is_not_posted_twice(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(did_nothing(), did_nothing(), did_nothing()))
    for _ in range(3):
        tick(composed)
    gh.lose_reply.add("comment")

    tick(composed)  # labels, posts, loses the reply
    restarted, _ = role(gh, Agent())
    tick(restarted)
    tick(restarted)

    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1


# ── Agent questions ──────────────────────────────────────────────────────────

def test_a_question_stops_for_a_human_and_the_reply_resumes_the_same_session(role):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(asked("Should the API return 404 or 410?", session="s-q"),
                  pushed(gh, "fix/5-null-token-crashes", "h1", session="s-q"))
    composed, _ = role(gh, agent)
    tick(composed)  # the leg asks; the question is posted on the issue
    assert gh.comments_with("issue", 5, "Should the API return 404 or 410?")

    tick(composed)  # derive stops the issue
    assert Label.NEEDS_HUMAN in gh.issues[5].labels
    assert len(gh.comments_with("issue", 5, stop_tag(StopReason.QUESTION))) == 1
    assert tick(composed) == []

    gh.comment("issue", 5, "410, it is gone for good.", author="alice")
    tick(composed)  # the reply releases the stop
    assert Label.NEEDS_HUMAN not in gh.issues[5].labels

    assert dispatched(tick(composed), AgentKind.RESOLVE)
    resumed = agent.tasks[1]
    assert resumed.resume == "s-q"
    assert "410, it is gone for good." in resumed.prompt
