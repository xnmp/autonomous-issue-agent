"""The composed fix role (slice 5b): production wiring, real SQLite, fake GitHub.

Each test ticks `compose_fix_daemon` against an in-memory GitHub that applies
writes the way GitHub does, and asserts on what GitHub ends up holding (PRs,
labels, comments, merges) and on what the agent was asked to do.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import BlockerLinkFacts
from issue_agent.app.config import from_env
from issue_agent.app.fix import compose_fix_daemon
from issue_agent.domain.actions import AgentKind, MergePullRequest, OpenPullRequest, RunAgent
from issue_agent.domain.contracts import RunId
from issue_agent.domain.effort import Effort
from issue_agent.domain.checks import BaseCI, BaseIssue, BaseRun, Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import MAX_BUDGET_STATUS, AgentResult, ExitReason
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import StopReason, stop_tag
from issue_agent.domain.world import BlockerRef, Branch, Comment, IssueView, PRView, Review, WorldState

FIX_BOT, REVIEW_BOT = "fix-bot", "review-bot"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ci(head: str, bucket: str, attempt: int = 1) -> Observed:
    """One required Actions job; each head and run attempt mints its own job id."""
    state = {"pending": "IN_PROGRESS", "fail": "FAILURE"}.get(bucket, "SUCCESS")  # a completed run reports its conclusion
    job = sum(map(ord, head)) * 100 + attempt
    check = Check("test", state=state, bucket=bucket, started_at=_now(), run_id=900, required=True, check_id=job,
                  run_attempt=attempt)
    return Observed(head, head, frozenset({RequiredCheck("test")}), (check,))


@dataclass
class FakeGitHub:
    """Just enough GitHub for the fix role: issues, harness PRs, pushed
    branches, labels and comments. Writes land the way GitHub applies them."""

    issues: dict[int, IssueView] = field(default_factory=dict)
    prs: dict[int, PRView] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)  # name -> pushed head
    runs: dict[int, str] = field(default_factory=dict)  # run id -> branch; all in progress
    cancelled: list[int] = field(default_factory=list)
    pushed_at: dict[str, str] = field(default_factory=dict)  # name -> last push
    login: str = FIX_BOT  # who the fix credential acts as

    def viewer_login(self) -> str:
        return self.login
    closed_at: dict[int, str] = field(default_factory=dict)  # PR -> when a human closed it
    merged: list[tuple[int, str]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    lose_reply: set[str] = field(default_factory=set)
    rerun_result: str = "fail"  # how the harness's re-run of the failed jobs comes out
    harness_reruns: list[int] = field(default_factory=list)  # run ids the harness re-ran
    lock: threading.Lock = field(default_factory=threading.Lock)
    # The base branch's completed push runs, when each issue was last closed,
    # and the issues GitHub's search has not indexed yet.
    base_runs: list[BaseRun] = field(default_factory=list)
    unindexed: set[int] = field(default_factory=set)
    closed_times: dict[int, tuple[str, str]] = field(default_factory=dict)  # issue -> (when, by whom)
    observed_by: list[str] = field(default_factory=list)

    # ── observation ──
    def build_world(self, base: str, *, filed_by: str = "") -> WorldState:
        with self.lock:
            now = _now()
            self.observed_by.append(filed_by)
            return WorldState(
                issues=tuple(self._open_blockers(i) for i in self.issues.values() if i.state == "open"),
                prs=tuple(self.prs.values()),
                branches=tuple(Branch(b, self.pushed_at.get(b, now)) for b in self.branches if self._owed(b)),
                now=now,
                base_ci=self._base_ci(filed_by) if filed_by else None,
            )

    def _base_ci(self, filer: str) -> BaseCI:
        """What the gateway reads: the runs and the fixer's issues titled
        "… failing on dev", open and closed."""
        issues = tuple(BaseIssue(n, i.title, *(("", False) if i.state == "open" else
                                               (self.closed_times[n][0], self.closed_times[n][1] == filer)))
                       for n, i in sorted(self.issues.items())
                       if i.author == filer and "failing on dev" in i.title and n not in self.unindexed)
        return BaseCI(tuple(self.base_runs), issues)

    def _open_blockers(self, issue: IssueView) -> IssueView:
        """GitHub lists only a blocker that is still open (a closed one is resolved)."""
        return replace(issue, blocked_by=tuple(
            b for b in issue.blocked_by if b.repository or self.issues.get(b.number, issue).state == "open"))

    def blocker_link(self, issue: int, blocker: int) -> BlockerLinkFacts:
        mine, theirs = self._open_blockers(self.issues[issue]), self._open_blockers(self.issues[blocker])
        return BlockerLinkFacts(f"I{issue}", f"I{blocker}", self.issues[blocker].state == "open",
                                BlockerRef(blocker) in mine.blocked_by,
                                tuple(b.number for b in theirs.blocked_by if not b.repository))

    def add_blocked_by(self, issue_id: str, blocker_id: str) -> None:
        issue, blocker = int(issue_id[1:]), int(blocker_id[1:])
        current = self.issues[issue]
        self.issues[issue] = replace(current, blocked_by=(*current.blocked_by, BlockerRef(blocker)))

    def _owed(self, branch: str) -> bool:
        """GitHub's answer to "is this branch owed a PR": none ever, or pushed
        after its newest PR was closed unmerged."""
        prs = [n for n, p in self.prs.items() if p.head_ref_name == branch]
        if not prs:
            return True
        newest = max(prs)
        return (
            self.prs[newest].state == "CLOSED"
            and newest in self.closed_at
            and self.pushed_at.get(branch, "") > self.closed_at[newest]
        )

    def get_issue(self, number: int) -> IssueView:
        return self.issues[number]

    def issue_index(self, limit: int = 300, author: str = "") -> tuple[tuple[int, str, str, frozenset[Label]], ...]:
        mine = {"": None, "@me": FIX_BOT}.get(author, author)
        return tuple((n, i.state, i.title, i.labels) for n, i in sorted(self.issues.items())
                     if mine is None or i.author == mine)[:limit]

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        return self.prs[number]

    def pull_request_for_branch(self, branch: str) -> int | None:
        return next((n for n, p in self.prs.items() if p.head_ref_name == branch and p.state == "OPEN"), None)

    def _subject(self, kind: str, number: int) -> IssueView | PRView:
        return self.issues[number] if kind == "issue" else self.prs[number]

    def has_label(self, kind: str, number: int, label: str) -> bool:
        return any(lab.value == label for lab in self._subject(kind, number).labels)

    def has_comment(self, kind: str, number: int, body: str, author: str) -> bool:
        return any(c.body == body and c.author == author for c in self._subject(kind, number).comments)

    def is_issue_closed(self, issue: int) -> bool:
        return self.issues[issue].state != "open"

    def run_attempt(self, run_id: int) -> int:
        return max(c.run_attempt or 1 for p in self.prs.values() for c in p.checks if c.run_id == run_id)

    def rerun_failed_checks(self, run_id: int) -> None:
        self.harness_reruns.append(run_id)
        number = next(n for n, p in self.prs.items() if any(c.run_id == run_id for c in p.checks))
        self.rerun(number, self.rerun_result)

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
        url = f"https://github.com/o/r/{'issues' if kind == 'issue' else 'pull'}/{number}#issuecomment-{len(store[number].comments) + 1}"
        store[number] = replace(store[number], comments=(*store[number].comments, Comment(author, body, _now(), url)))
        self._lossy("comment")

    def open_pull_request(self, branch: str, base: str, title: str, body: str) -> None:
        with self.lock:
            if self.pull_request_for_branch(branch) is not None:
                raise RuntimeError("a pull request already exists for this branch")
            number = 100 + len(self.prs)
            head = self.branches[branch]
            self.prs[number] = PRView(
                number=number,
                head_ref_name=branch,
                head_ref_oid=head,
                state="OPEN",
                title=title,
                body=body,
                author=FIX_BOT,
                ci=_ci(head, "pending"),
                mergeable="MERGEABLE",
                merge_state="CLEAN",
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

    def branch_runs(self, branch: str) -> tuple[tuple[int, str, str], ...]:
        return tuple((i, "in_progress", "2020-01-01T00:00:00Z") for i, b in self.runs.items() if b == branch)

    def cancel_run(self, run_id: int) -> None:
        self.cancelled.append(run_id)

    def close_issue(self, number: int, reason: str, comment: str, *, author: str = FIX_BOT) -> None:
        self.closed.append(number)
        self.closed_times[number] = (_now(), author)
        current = self.issues[number]
        comments = (*current.comments, Comment(author, comment, _now())) if comment else current.comments
        self.issues[number] = replace(current, state="closed", comments=comments)

    def reopen_issue(self, number: int, comment: str) -> None:
        current = self.issues[number]
        comments = (*current.comments, Comment(FIX_BOT, comment, _now()))
        self.issues[number] = replace(current, state="open", comments=comments)

    def create_issue(self, title: str, body: str) -> int:
        number = max((*self.issues, *self.prs, 0)) + 1
        self.issues[number] = IssueView(number=number, title=title, body=body, author=FIX_BOT, updated_at=_now())
        self._lossy("create_issue")
        return number

    # ── test helpers ──
    def push(self, branch: str, head: str, ci: str = "pending") -> None:
        """An agent's `git push`: moves the branch, and the PR head if one exists."""
        with self.lock:
            self.branches[branch] = head
            self.pushed_at[branch] = _now()
            for number, p in self.prs.items():
                if p.head_ref_name == branch and p.state == "OPEN":
                    self.prs[number] = replace(
                        p, head_ref_oid=head, ci=_ci(head, ci), reviews=(), mergeable="MERGEABLE", merge_state="CLEAN"
                    )

    def rerun(self, number: int, bucket: str = "fail") -> None:
        """`gh run rerun --failed`: a new attempt of the head's run, new job ids."""
        with self.lock:
            p = self.prs[number]
            attempt = max(c.run_attempt or 1 for c in p.checks) + 1
            self.prs[number] = replace(p, ci=_ci(p.head_ref_oid, bucket, attempt))

    def close_pr(self, number: int) -> None:
        """A human closes the PR unmerged."""
        with self.lock:
            self.prs[number] = replace(self.prs[number], state="CLOSED")
            self.closed_at[number] = _now()

    def comments_with(self, kind: str, number: int, text: str) -> list[Comment]:
        return [c for c in self._subject(kind, number).comments if text in c.body]


class FakeWorktrees:
    def __init__(self, root: Path, gh: FakeGitHub | None = None):
        self.root = root
        self.gh = gh
        self.removed: list[int] = []

    def remote_head(self, branch: str) -> str:
        """`git ls-remote`: the branch as pushed, before GitHub's PR view shows it."""
        return self.gh.branches.get(branch, "") if self.gh is not None else ""

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


def red_pushes(gh: FakeGitHub, count: int):
    """Agent legs that each push a new head of the PR branch, whose CI fails."""
    return [pushed(gh, "fix/5-null-token-crashes", f"h{n + 2}", ci="fail") for n in range(count)]


def unseen_push(gh: FakeGitHub, branch: str, head: str):
    """The agent pushes, but GitHub's PR view does not show it yet."""
    def step(_task):
        gh.branches[branch] = head
        return AgentResult(False, status="pushed", cost_usd=0.1, session_id="s1")

    return step


def reran(gh: FakeGitHub, number: int = 100, bucket: str = "fail", session: str = "s1"):
    """The agent re-runs the failed jobs and ends the leg (inconclusive evidence)."""
    def step(_task):
        gh.rerun(number, bucket)
        return AgentResult(False, status="pushed", cost_usd=0.1, session_id=session)

    return step


def died(_task):
    raise ConnectionError("stream reset by peer")


def asked(needs: str, session: str = "s1"):
    return lambda _task: AgentResult(False, status="blocked", needs=needs, cost_usd=0.1, session_id=session)


def seed_light_legs(composed, number: int, **results) -> None:
    """Record a STEWARD and a TRIAGE agent end for `number`: `results` maps a
    kind to its receipt entries (default: an unusable result)."""
    for kind in ("STEWARD", "TRIAGE"):
        composed.control.record_unpaid_leg(
            RunId(uuid.uuid4().hex), repository="o/r", role="fix", kind=kind, owner_id="seed",
            issue=number, number=number, head="", runtime="claude", leg_class="agent", result=results.get(kind, {}))


@pytest.fixture
def role(tmp_path):
    def compose(gh: FakeGitHub, agent: Agent, log=lambda _line: None, *, triaged: bool = True, **env):
        cfg = replace(
            from_env(
                {
                    "REPO": "o/r",
                    "FIX_BOT": FIX_BOT,
                    "REVIEW_BOT": REVIEW_BOT,
                    "BASE_BRANCH": "dev",
                    "STATE_DIR": str(tmp_path / "state"),
                    "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
                    **env,
                }
            ),
            control_authority_root=str(tmp_path / "machine-state"),
        )
        worktrees = FakeWorktrees(tmp_path / "worktrees", gh)
        composed = compose_fix_daemon(cfg, gh, worktrees, agent, log=log)
        if triaged:
            # Light legs that returned nothing usable: the issue resolves at the
            # default tier with no triage comment (test_triage_routing covers them).
            for number in gh.issues:
                seed_light_legs(composed, number)
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


_APPROVED = (Review("APPROVED", REVIEW_BOT, submitted_at="2026-10-01T00:00:00Z", commit_id="h1"),)


def _capped(_task):
    return AgentResult(True, status="", exit_reason=ExitReason.WALL_TIMEOUT, cost_usd=0.1, session_id="s-wall")


def open_pr(gh: FakeGitHub, issue: int = 5, *, head: str = "h1", ci: str = "pass", **kw) -> int:
    branch = f"fix/{issue}-null-token-crashes"
    gh.branches[branch] = head
    gh.prs[100] = PRView(
        number=100,
        head_ref_name=branch,
        head_ref_oid=head,
        state="OPEN",
        author=FIX_BOT,
        ci=_ci(head, ci),
        mergeable=kw.pop("mergeable", "MERGEABLE"),
        merge_state=kw.pop("merge_state", "CLEAN"),
        **kw,
    )
    return 100


# ── #424: the credential must be FIX_BOT ─────────────────────────────────────


def test_the_fix_role_starts_when_its_credential_is_fix_bot_in_either_bot_spelling(role):
    role(FakeGitHub(login=FIX_BOT + "[bot]"), Agent())


@pytest.mark.parametrize("login", ["someone-else", "", "   "])
def test_the_fix_role_refuses_to_start_when_its_credential_is_not_fix_bot(role, login):
    with pytest.raises(ValueError, match="FIX_BOT"):
        role(FakeGitHub(login=login), Agent())


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
    assert gh.prs[100].body == "Fixed the null token."  # the agent's description, unchanged
    task = agent.tasks[0]
    assert "git push origin fix/5-null-token-crashes" in task.prompt
    # The agent reads the issue itself: a pointer, not the pasted text.
    assert "gh issue view 5 --repo o/r --comments" in task.prompt and "Steps: ..." not in task.prompt
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


@pytest.mark.parametrize(
    "kind,shape",
    [
        (AgentKind.FIX_CI, dict(ci="fail")),
        (AgentKind.REBASE, dict(mergeable="CONFLICTING", merge_state="DIRTY")),
        (
            AgentKind.ADDRESS_REVIEW,
            dict(
                reviews=(
                    Review(
                        "CHANGES_REQUESTED",
                        REVIEW_BOT,
                        body="Handle None.",
                        submitted_at="2026-10-01T00:00:00Z",
                        commit_id="h1",
                    ),
                )
            ),
        ),
    ],
)
def test_each_pr_state_dispatches_its_leg_bound_to_the_head_and_resumes_the_session(role, kind, shape):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(
        pushed(gh, "fix/5-null-token-crashes", "h1", session="s-resolve"),
        pushed(gh, "fix/5-null-token-crashes", "h2", session="s-resolve"),
    )
    composed, _ = role(gh, agent)
    tick(composed)
    tick(composed)  # opens the PR
    shape = dict(shape)
    gh.prs[100] = replace(gh.prs[100], ci=_ci("h1", shape.pop("ci", "pass")), **shape)

    assert [(a.kind, a.number, a.head) for a in dispatched(tick(composed), kind)] == [(kind, 100, "h1")]
    task = agent.tasks[1]
    assert task.resume == "s-resolve"
    assert gh.prs[100].head_ref_oid == "h2"


def test_three_unproductive_attempts_stop_with_one_label_and_one_comment(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(*red_pushes(gh, 3))  # each push fails again: new evidence, no progress
    composed, _ = role(gh, agent)
    for _ in range(3):
        assert dispatched(tick(composed), AgentKind.FIX_CI)

    tick(composed)
    tick(composed)
    tick(composed)

    assert Label.NEEDS_HUMAN in gh.prs[100].labels
    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1
    assert len(agent.tasks) == 3


def test_an_approved_green_head_merges_bound_to_that_head_and_closes_the_issue(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=_APPROVED)
    composed, worktrees = role(gh, Agent())

    assert MergePullRequest(100, "h1") in tick(composed)
    assert gh.merged == [(100, "h1")]
    assert gh.closed == [5] and worktrees.removed == [5]
    assert tick(composed) == []  # merged: neither the PR nor its issue is an item


def test_auto_merge_off_leaves_an_approved_pr_for_a_human(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=_APPROVED)
    composed, _ = role(gh, Agent(), AUTO_MERGE="false")

    assert tick(composed) == []
    assert gh.merged == []


def test_a_lost_stop_comment_reply_is_not_posted_twice(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(*red_pushes(gh, 3)))
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
    agent = Agent(
        asked("Should the API return 404 or 410?", session="s-q"),
        pushed(gh, "fix/5-null-token-crashes", "h1", session="s-q"),
    )
    composed, _ = role(gh, agent)
    tick(composed)  # the leg asks; its receipt records the question
    assert gh.issues[5].comments == ()

    tick(composed)  # derive stops the issue; the one stop comment carries the question
    assert Label.NEEDS_HUMAN in gh.issues[5].labels
    stops = gh.comments_with("issue", 5, stop_tag(StopReason.QUESTION))
    assert len(stops) == 1 and "Should the API return 404 or 410?" in stops[0].body
    assert len(gh.issues[5].comments) == 1
    assert tick(composed) == []

    gh.comment("issue", 5, "410, it is gone for good.", author="alice")
    tick(composed)  # the reply releases the stop
    assert Label.NEEDS_HUMAN not in gh.issues[5].labels

    assert dispatched(tick(composed), AgentKind.RESOLVE)
    resumed = agent.tasks[1]
    assert resumed.resume == "s-q"
    reply = gh.issues[5].comments[-1]
    # A pointer to the answer, which the agent reads (with any edit) itself.
    assert reply.url in resumed.prompt and "gh issue view 5 --repo o/r --comments" in resumed.prompt
    assert "410, it is gone for good." not in resumed.prompt


# ── Fix round (slice 5b review): regressions ─────────────────────────────────


@pytest.fixture
def later(monkeypatch):
    """Moves the roles' tick clock past every machine backoff."""
    import issue_agent.app.tick as tick_module

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=2)

    return lambda: monkeypatch.setattr(tick_module, "datetime", Later)


def test_a_leg_ending_while_github_is_read_is_not_dispatched_again(role):
    # P0-1: the world predates the leg's push; its end is already in SQLite.
    gh = FakeGitHub(issues={5: ready()})
    release = threading.Event()

    def resolve(_task):
        assert release.wait(timeout=10)
        gh.push("fix/5-null-token-crashes", "h1")
        return AgentResult(False, status="pushed", summary="Fixed.", cost_usd=0.1, session_id="s1")

    agent = Agent(resolve)
    composed, _ = role(gh, agent)
    assert dispatched(composed.daemon.tick(), AgentKind.RESOLVE)
    observe = composed.daemon.observe

    def stale_observe():
        world = observe()
        release.set()
        composed.daemon.wait_for_dispatches()  # the leg ends mid-tick
        return world

    composed.daemon.observe = stale_observe
    assert not dispatched(composed.daemon.tick(), AgentKind.RESOLVE)
    composed.daemon.observe = observe
    assert OpenPullRequest(5, "fix/5-null-token-crashes") in tick(composed)
    assert len(agent.tasks) == 1


def test_two_issues_run_their_legs_concurrently(role):
    # P1-1: claims conflict by target only.
    gh = FakeGitHub(issues={5: ready(5), 6: ready(6, "Other bug")})
    gate = threading.Event()
    started = [threading.Event(), threading.Event()]

    def step(i):
        def run(_task):
            started[i].set()
            assert gate.wait(timeout=10)
            return AgentResult(False, status="pushed", cost_usd=0.1, session_id="s")

        return run

    composed, _ = role(gh, Agent(step(0), step(1)), FIX_DISPATCH_CONCURRENCY="2")
    taken = composed.daemon.tick()
    try:
        assert len(dispatched(taken, AgentKind.RESOLVE)) == 2
        assert started[0].wait(timeout=10) and started[1].wait(timeout=10)
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()


def test_a_stop_comment_that_failed_is_posted_later_and_a_reply_then_releases(role):
    # P1-2: the label landed, the comment did not.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(*red_pushes(gh, 3)))
    for _ in range(3):
        tick(composed)
    comment = gh.comment

    def unavailable(*_args, **_kw):
        raise RuntimeError("502 from GitHub; nothing written")

    gh.comment = unavailable
    tick(composed)
    assert Label.NEEDS_HUMAN in gh.prs[100].labels and not gh.comments_with("pr", 100, "needs-human")
    gh.comment = comment
    tick(composed)
    tick(composed)
    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1

    gh.comment("pr", 100, "Try mocking the clock.", author="alice")
    tick(composed)
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels


def test_a_machine_death_after_the_answer_keeps_the_answer(role, later):
    # P1-3: the question is the newest agent leg's, not the newest run's.
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(asked("404 or 410?", session="s-q"), died, did_nothing(session="s-q"))
    composed, _ = role(gh, agent)
    tick(composed)
    tick(composed)  # stopped
    gh.comment("issue", 5, "410.", author="alice")
    tick(composed)  # released
    assert dispatched(tick(composed), AgentKind.RESOLVE)  # the resume dies
    later()
    assert dispatched(tick(composed), AgentKind.RESOLVE)
    retried = agent.tasks[2]
    assert retried.resume == "s-q" and gh.issues[5].comments[-1].url in retried.prompt


def test_342_the_agents_own_mid_leg_comment_is_not_the_reply_and_a_later_one_is(role, later):
    # tauri #1102: FIX_BOT is the operator's login. A PR-stage leg posts
    # progress unmarked, then wall-caps; its continuation must not be handed
    # that comment as the human's answer. A comment after the leg ends is.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")

    def posts_then_caps(task):
        # What supervised_exec does once the provider process exists: the leg
        # is running and its start is stamped (the fake agent spawns nothing).
        env = task.env
        SQLiteControlStore(env["ISSUE_AGENT_CONTROL_DB"]).register_process_lease(
            RunId(env["ISSUE_AGENT_RUN_ID"]), expected_owner_id=env["ISSUE_AGENT_OWNER_ID"],
            pid=999_999, pgid=999_999, process_start_ticks=1,
        )
        gh.comment("pr", 100, "Progress report: still red after the retry.", author=FIX_BOT)
        time.sleep(0.01)  # the leg's end is stamped in whole milliseconds
        return _capped(task)

    agent = Agent(asked("Mock the clock or the network?", session="s-q"), posts_then_caps, did_nothing("s-wall"))
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # asks
    tick(composed)  # stops with the question
    gh.comment("pr", 100, "Mock the clock.", author=FIX_BOT)
    tick(composed)  # released
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # posts, then the wall cap
    gh.comment("pr", 100, "Also pin the timezone.", author=FIX_BOT)
    later()
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    continued = agent.tasks[2].prompt
    urls = {c.body: c.url for c in gh.prs[100].comments}
    assert urls["Mock the clock."] in continued  # the answer it points at
    assert urls["Progress report: still red after the retry."] not in continued


def test_a_human_closed_pr_is_reopened_once_the_agent_pushes_again(role):
    # P1-8: the branch is owed a PR again once pushed after the close.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh)
    gh.close_pr(100)
    composed, _ = role(gh, Agent(pushed(gh, "fix/5-null-token-crashes", "h2")))

    assert dispatched(tick(composed), AgentKind.RESOLVE)
    assert OpenPullRequest(5, "fix/5-null-token-crashes") in tick(composed)
    reopened = [p for p in gh.prs.values() if p.state == "OPEN"]
    assert [(p.number, p.head_ref_oid) for p in reopened] == [(101, "h2")]


def test_a_first_pr_stage_wall_cap_is_a_machine_end_and_its_resume_counts(role, later):
    # P2-1: resume once; a PR-stage leg always resumes the issue's session.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(_capped, _capped))
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    assert tick(composed) == []  # a machine end: backing off, no attempt
    later()
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    legs = [
        r.receipt["leg"]
        for r in sorted(composed.control.leg_runs("o/r", (5,)), key=lambda r: r.completed_at)
        if r.receipt is not None and r.kind == "FIX_CI"
    ]
    assert legs == ["machine", "agent"]


def test_a_wall_cap_after_a_transport_death_still_resumes_once_and_then_counts(role, later):
    # Round 2: a receipt-less death between two wall caps does not reset resume-once.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(_capped, died, _capped)
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    later()
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # dies, no receipt
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # continues the wall-capped session
    runs = sorted((r for r in composed.control.leg_runs("o/r", (5,)) if r.kind == "FIX_CI"),
                  key=lambda r: r.completed_at)
    assert [r.receipt["leg"] if r.receipt else None for r in runs] == ["machine", None, "agent"]
    assert agent.tasks[2].resume == "s-wall"


# ── per-leg caps: a budget hit is a wall timeout (owner, 2026-10-14) ────────

# A cap hit spends at least the leg budget (it stops on the crossing), and a
# resume is only bought while the issue cap still affords a leg at the
# dearer role's budget: $1 legs under the default $25 issue cap.
TIERS = {"FIX_MODEL_MED": "claude:claude-sonnet-5-5", "FIX_MODEL_HIGH": "claude:claude-opus-5-5",
         "MAX_BUDGET_USD": "1", "REVIEW_BUDGET_USD": "1"}


def _cap_hit(cap: str, session: str, cost: float = 1.05):
    """A leg the harness ended at a per-leg cap with its session intact.

    Settlement prices tokens, not the CLI's dollars (#389): `cost` is spent as
    output tokens at claude-sonnet-5-5's $10/MTok."""
    spent = cap == "budget"
    return lambda _task: AgentResult(
        True, status=MAX_BUDGET_STATUS if spent else "", envelope_subtype=MAX_BUDGET_STATUS if spent else "",
        exit_reason=ExitReason.BUDGET_EXHAUSTED if spent else ExitReason.WALL_TIMEOUT, cost_usd=cost,
        usage={"input_tokens": 0, "output_tokens": round(cost * 100_000)}, session_id=session)


def _api_error(session: str):
    """A Claude leg the provider ended (#418), parsed from its real envelope."""
    from types import SimpleNamespace

    from issue_agent.adapters.agent_runner import result_to_agent_result
    return lambda _task: result_to_agent_result(SimpleNamespace(
        is_error=True, subtype="success", terminal_reason="api_error", session_id=session,
        result="Failed to authenticate", total_cost_usd=0.0, usage={"input_tokens": 0, "output_tokens": 100_000}))


def test_a_provider_ended_leg_is_a_machine_end_and_a_second_on_its_resume_counts(role, later):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(_api_error("s-1"), _api_error("s-1"))
    composed, _ = role(gh, agent, **TIERS)
    assert dispatched(tick(composed), AgentKind.RESOLVE)
    later()
    assert dispatched(tick(composed), AgentKind.RESOLVE)
    assert [t.resume for t in agent.tasks] == [None, "s-1"]
    assert _resolve_legs(composed) == ["machine", "agent"]


def _resolve_legs(composed) -> list[str | None]:
    runs = sorted((r for r in composed.control.leg_runs("o/r", (5,)) if r.kind == "RESOLVE"),
                  key=lambda r: r.completed_at)
    return [r.receipt["leg"] if r.receipt else None for r in runs]


def test_a_first_budget_hit_resumes_its_session_at_the_same_tier_and_is_no_attempt(role, later):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(_cap_hit("budget", "s-1"), pushed(gh, "fix/5-null-token-crashes", "h1", session="s-1"))
    composed, _ = role(gh, agent, **TIERS)
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]
    assert tick(composed) == []  # a machine end: backing off, no attempt
    later()
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]

    first, resumed = agent.tasks
    assert first.resume is None and resumed.resume == "s-1"
    assert first.model == resumed.model == "claude-sonnet-5-5"
    assert _resolve_legs(composed) == ["machine", "agent"]
    assert OpenPullRequest(5, "fix/5-null-token-crashes") in tick(composed)


@pytest.mark.parametrize("first", ["wall", "budget"])
@pytest.mark.parametrize("second", ["wall", "budget"])
def test_a_cap_hit_on_the_resume_is_an_attempt_whichever_caps_they_were(role, later, first, second):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(_cap_hit(first, "s-1"), _cap_hit(second, "s-1"),
                  pushed(gh, "fix/5-null-token-crashes", "h1", session="s-2"))
    composed, _ = role(gh, agent, **TIERS)
    assert dispatched(tick(composed), AgentKind.RESOLVE)
    later()
    assert dispatched(tick(composed), AgentKind.RESOLVE)  # resumes, and hits a cap again
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.HIGH)]

    capped, resumed, fresh = agent.tasks
    assert (capped.resume, resumed.resume, fresh.resume) == (None, "s-1", None)
    assert (capped.model, resumed.model, fresh.model) == ("claude-sonnet-5-5",) * 2 + ("claude-opus-5-5",)
    assert _resolve_legs(composed) == ["machine", "agent", "agent"]


@pytest.mark.parametrize("first", ["wall", "budget"])
@pytest.mark.parametrize("second", ["wall", "budget"])
def test_a_repair_capped_on_its_resume_hands_the_same_failure_to_a_leg_one_tier_up(role, later, first, second):
    # A capped leg did not finish its repair: its failure is not handed, so the
    # head is neither re-run unrepaired nor stopped for ATTEMPTS.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(_cap_hit(first, "s-1"), _cap_hit(second, "s-1"), did_nothing("s-2"))
    composed, _ = role(gh, agent, **TIERS)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    later()
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # resumes, and hits a cap again
    fresh = dispatched(tick(composed), AgentKind.FIX_CI)

    assert gh.harness_reruns == [] and Label.NEEDS_HUMAN not in gh.prs[100].labels
    assert fresh and fresh[0].tier is Tier.HIGH
    capped, resumed, retried = agent.tasks
    # A PR-stage leg continues the issue's latest session on its runtime, the tier-up leg too.
    assert resumed.resume == retried.resume == "s-1" and (capped.model, resumed.model, retried.model) == (
        "claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
    assert capped.evidence == resumed.evidence == retried.evidence  # the same unfixed failure


def test_a_budget_hit_never_buys_past_the_issues_lifetime_cap(role, later):
    # The lifetime cap is the human's grant: a machine end does not route around it.
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(_cap_hit("budget", "s-1", cost=0.6))
    composed, _ = role(gh, agent, ISSUE_SPEND_CAP_USD="1", MAX_BUDGET_USD="0.6", REVIEW_BUDGET_USD="0.6",
                       FIX_MODEL="claude-sonnet-5-5")
    assert dispatched(tick(composed), AgentKind.RESOLVE)
    later()
    tick(composed)

    assert Label.NEEDS_HUMAN in gh.issues[5].labels
    assert len(gh.comments_with("issue", 5, stop_tag(StopReason.LIFETIME_CAP))) == 1
    assert len(agent.tasks) == 1 and _resolve_legs(composed) == ["machine"]


def test_a_claude_stream_cut_off_by_the_wall_cap_after_init_resumes_that_session(role, later, monkeypatch):
    # #340: the session id came only from the final ResultMessage, so a wall
    # cap left nothing to resume. The real SDK runner, a scripted stream.
    import sys
    import types

    from issue_agent.adapters.agent_runner import AgentRunner

    class ResultMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    resumes: list[str | None] = []

    async def query(prompt, options):
        resumes.append(options["resume"] if "resume" in options else None)
        yield type("SystemMessage", (), {"subtype": "init", "data": {"session_id": "s-init"}})()
        if len(resumes) == 1:
            await asyncio.Event().wait()  # hangs past the wall cap
        yield ResultMessage(subtype="success", is_error=False, session_id="s-init",
                            structured_output={"status": "pushed", "summary": "Fixed."})

    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeAgentOptions, sdk.ResultMessage, sdk.query = dict, ResultMessage, query
    sdk.HookMatcher = dict
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    gh = FakeGitHub(issues={5: ready()})
    composed, _ = role(gh, AgentRunner(idle_timeout=None, max_runtime=0.3, poll_slice=0.05))

    assert dispatched(tick(composed), AgentKind.RESOLVE)
    assert tick(composed) == []  # a machine end: backing off, no attempt
    later()
    assert dispatched(tick(composed), AgentKind.RESOLVE)

    assert resumes == [None, "s-init"]
    runs = sorted((r for r in composed.control.leg_runs("o/r", (5,)) if r.kind not in ("STEWARD", "TRIAGE")),
                  key=lambda r: r.completed_at)
    assert [(r.receipt["leg"], r.receipt["session"]) for r in runs] == [("machine", "s-init"), ("agent", "s-init")]


def test_an_approved_pr_that_branch_protection_blocks_waits_with_a_log_line(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, merge_state="BLOCKED", reviews=_APPROVED)
    lines: list[str] = []
    composed, _ = role(gh, Agent(), log=lines.append)
    assert tick(composed) == [] and gh.merged == []
    assert any("PR #100 waits to merge" in line and "BLOCKED" in line for line in lines)


# ── STEWARD, TRIAGE and routing (owner, 2026-10-13/14) ──────────────────────


def triaged(tier: str = "high", duplicate_of: int | None = None, session: str = "t1"):
    """One scripted light result for either act: the steward's claim for a
    STEWARD task, the grader's for a TRIAGE task (told apart by their schemas)."""
    steward = {"acceptance_criteria": ["A null token returns 401, not 500"],
               "duplicate_of": duplicate_of, "duplicate_reason": "Same crash" if duplicate_of else ""}
    triage = {"tier": tier, "rationale": "One handler."}

    def run(task):
        claim = triage if "tier" in task.schema["properties"] else steward
        return AgentResult(False, structured=claim, cost_usd=0.01, session_id=session)
    return run


ROUTES = {"TRIAGE_MODEL": "claude:claude-haiku-5-5:high", "STEWARD_MODEL": "claude:claude-sonnet-5-5:high",
          "FIX_MODEL_HIGH": "claude:claude-opus-5-5", "FIX_MODEL_XHIGH": "claude:claude-fable-5-1"}


def test_steward_then_triage_then_one_comment_then_resolve_each_on_its_own_route(role):
    gh = FakeGitHub(issues={5: ready(), 2: IssueView(2, title="Login is slow")})
    light = triaged("high")
    agent = Agent(light, light, pushed(gh, "fix/5-null-token-crashes", "h1", session="s-resolve"))
    composed, _ = role(gh, agent, triaged=False, **ROUTES)

    assert dispatched(tick(composed), AgentKind.STEWARD) == [RunAgent(AgentKind.STEWARD, 5)]
    steward = agent.tasks[0]
    assert (steward.model, steward.effort, steward.max_budget_usd) == ("claude-sonnet-5-5", Effort.HIGH, 2.0)
    assert steward.schema["properties"]["duplicate_of"]["enum"] == [2, None]  # never itself
    assert "Login is slow" in steward.prompt

    assert dispatched(tick(composed), AgentKind.TRIAGE) == [RunAgent(AgentKind.TRIAGE, 5)]
    grader = agent.tasks[1]
    assert (grader.model, grader.effort, grader.max_budget_usd) == ("claude-haiku-5-5", Effort.HIGH, 2.0)
    assert "A null token returns 401, not 500" in grader.prompt  # the steward's criteria reach the grader
    assert "Login is slow" not in grader.prompt  # no duplicate hunt

    tick(composed)  # the one comment
    tick(composed)  # nothing new to say: RESOLVE
    (comment,) = gh.comments_with("issue", 5, "Acceptance criteria")
    assert "A null token returns 401, not 500" in comment.body and "`high`" in comment.body
    resolve = agent.tasks[2]
    assert resolve.model == "claude-opus-5-5" and resolve.resume is None  # never a light session
    assert "A null token returns 401, not 500" in resolve.prompt
    assert tick(composed) == [OpenPullRequest(5, "fix/5-null-token-crashes")]
    assert gh.prs[100].body == "Fixed the null token."  # the agent writes the description
    assert len(gh.comments_with("issue", 5, "Acceptance criteria")) == 1
    assert len(gh.comments_with("issue", 5, "issue-agent:triage")) == 1


def test_an_issue_with_its_own_criteria_is_resolved_against_the_issue(role):
    own = "Crash on null token.\n\n## Acceptance criteria\n- [ ] The API answers 401"
    gh = FakeGitHub(issues={5: replace(ready(), body=own)})
    light = triaged("high")
    agent = Agent(light, light, pushed(gh, "fix/5-null-token-crashes", "h1"))
    composed, _ = role(gh, agent, triaged=False, **ROUTES)
    tick(composed)  # STEWARD
    assert "acceptance_criteria" not in agent.tasks[0].schema["properties"]
    tick(composed)  # TRIAGE
    tick(composed)  # the triage comment: the tier only
    tick(composed)  # RESOLVE
    assert gh.comments_with("issue", 5, "Acceptance criteria") == []
    resolve = agent.tasks[2].prompt
    assert "steward acceptance criteria" not in resolve and "A null token returns 401" not in resolve
    assert "gh issue view 5 --repo o/r --comments" in resolve and "The API answers 401" not in resolve


def test_light_legs_cut_off_by_the_wall_cap_count_once_and_the_issue_resolves_at_the_default_tier(role):
    gh = FakeGitHub(issues={5: ready()})
    agent = Agent(_capped, _capped, pushed(gh, "fix/5-null-token-crashes", "h1"))
    composed, _ = role(gh, agent, triaged=False, **ROUTES, FIX_MODEL_MED="claude:claude-sonnet-5-5")
    assert dispatched(tick(composed), AgentKind.STEWARD)
    # Not a machine end retried (and re-paid) every backoff window.
    assert dispatched(tick(composed), AgentKind.TRIAGE)
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]
    assert agent.tasks[2].model == "claude-sonnet-5-5"
    assert gh.comments_with("issue", 5, "issue-agent:triage") == []


def test_a_suspected_duplicate_stops_before_triage_and_a_reply_lets_it_continue(role):
    gh = FakeGitHub(issues={5: ready(), 2: IssueView(2, title="Null token crash")})
    light = triaged("low", duplicate_of=2)
    agent = Agent(light, light, pushed(gh, "fix/5-null-token-crashes", "h1"))
    composed, _ = role(gh, agent, triaged=False, **ROUTES)
    tick(composed)  # STEWARD flags it
    tick(composed)  # the stop
    assert Label.NEEDS_HUMAN in gh.issues[5].labels
    (stop,) = gh.comments_with("issue", 5, stop_tag(StopReason.DUPLICATE))
    assert "#2" in stop.body
    assert tick(composed) == []  # held
    assert len(agent.tasks) == 1  # no triage leg was bought for a suspected duplicate

    gh.comment("issue", 5, "Not a duplicate: #2 is the web client.", author="alice")
    tick(composed)  # release
    assert dispatched(tick(composed), AgentKind.TRIAGE)
    tick(composed)  # triage comment
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.LOW)]
    assert Label.NEEDS_HUMAN not in gh.issues[5].labels


def test_a_combined_result_recorded_before_the_split_is_not_stewarded_again(role):
    gh = FakeGitHub(issues={5: ready()})
    legacy = {"tier": "high", "acceptance_criteria": ["Legacy criterion"], "duplicate_of": None,
              "duplicate_reason": "", "rationale": "Old combined act."}
    agent = Agent(pushed(gh, "fix/5-null-token-crashes", "h1"))
    composed, _ = role(gh, agent, triaged=False, **ROUTES)
    composed.control.record_unpaid_leg(
        RunId(uuid.uuid4().hex), repository="o/r", role="fix", kind="TRIAGE", owner_id="seed",
        issue=5, number=5, head="", runtime="claude", leg_class="agent", result={"triage": legacy})
    taken = tick(composed)
    assert not dispatched(taken, AgentKind.STEWARD) and not dispatched(taken, AgentKind.TRIAGE)
    (comment,) = gh.comments_with("issue", 5, "Acceptance criteria")
    assert "Legacy criterion" in comment.body
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.HIGH)]
    assert "Legacy criterion" in agent.tasks[0].prompt


# ── The light acts hold no dispatch slot (owner, 2026-10-14) ───────────────────────


class SplitAgent:
    """Routes by act: a STEWARD or TRIAGE task (the only ones with a schema) runs `light`,
    every other leg runs `heavy`. Each call is recorded with its kind."""

    def __init__(self, heavy, light):
        self.heavy, self.light, self.tasks = heavy, light, []

    async def run(self, task):
        self.tasks.append(task)
        return await asyncio.to_thread(self.light if task.schema is not None else self.heavy, task)


def _blocked(gate: threading.Event, result):
    def step(task):
        assert gate.wait(timeout=10)
        return result(task) if callable(result) else result
    return step


_DONE = AgentResult(False, status="pushed", cost_usd=0.1, session_id="s")


def _seed_triage(composed, number: int) -> None:
    seed_light_legs(composed, number)


def test_the_light_acts_dispatch_while_the_only_heavy_slot_is_busy(role):
    gh = FakeGitHub(issues={5: ready(5), 6: ready(6, "Other bug")})
    gate = threading.Event()
    agent = SplitAgent(_blocked(gate, _DONE), triaged())
    composed, _ = role(gh, agent, triaged=False, FIX_DISPATCH_CONCURRENCY="1")
    _seed_triage(composed, 5)
    try:
        taken = composed.daemon.tick()  # RESOLVE 5 holds the slot; STEWARD 6 still starts
        assert dispatched(taken, AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]
        assert dispatched(taken, AgentKind.STEWARD) == [RunAgent(AgentKind.STEWARD, 6)]
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()


def test_a_running_light_leg_does_not_take_the_heavy_slot(role):
    gh = FakeGitHub(issues={6: ready(6, "Other bug")})
    gate = threading.Event()
    agent = SplitAgent(_blocked(gate, _DONE), _blocked(gate, triaged()))
    composed, _ = role(gh, agent, triaged=False, FIX_DISPATCH_CONCURRENCY="1")
    try:
        assert dispatched(composed.daemon.tick(), AgentKind.STEWARD) == [RunAgent(AgentKind.STEWARD, 6)]
        gh.issues[5] = ready(5)
        _seed_triage(composed, 5)
        assert dispatched(composed.daemon.tick(), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()


def test_a_cycling_pr_does_not_starve_the_one_that_has_waited_longer(role):
    """#1125 starved behind lower-numbered PRs that kept cycling through the slot."""
    gh = FakeGitHub(issues={5: ready(5), 6: ready(6, "Other bug")})
    open_pr(gh, issue=5, ci="fail")
    gh.branches["fix/6-other-bug"] = "h2"
    gh.prs[101] = replace(gh.prs[100], number=101, head_ref_name="fix/6-other-bug", head_ref_oid="h2", ci=_ci("h2", "fail"))
    agent = Agent(did_nothing(), did_nothing())
    composed, _ = role(gh, agent, FIX_DISPATCH_CONCURRENCY="1")
    first = dispatched(tick(composed), AgentKind.FIX_CI)
    second = dispatched(tick(composed), AgentKind.FIX_CI)
    assert [a.number for a in first + second] == [100, 101]


def test_heavy_acts_still_cap_at_the_concurrency(role):
    gh = FakeGitHub(issues={5: ready(5), 6: ready(6, "Other bug"), 7: ready(7, "Third")})
    gate = threading.Event()
    agent = SplitAgent(_blocked(gate, _DONE), triaged())
    composed, _ = role(gh, agent, FIX_DISPATCH_CONCURRENCY="2")
    try:
        assert len(dispatched(composed.daemon.tick(), AgentKind.RESOLVE)) == 2
        assert not dispatched(composed.daemon.tick(), AgentKind.RESOLVE)  # both slots busy, 7 waits
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()


def test_the_light_acts_keep_their_per_issue_lease_and_the_spend_headroom(role):
    gh = FakeGitHub(issues={5: ready(5)})
    gate = threading.Event()
    agent = SplitAgent(lambda _t: _DONE, _blocked(gate, triaged()))
    composed, _ = role(gh, agent, triaged=False, FIX_DISPATCH_CONCURRENCY="1")
    try:
        assert dispatched(composed.daemon.tick(), AgentKind.STEWARD)
        assert not dispatched(composed.daemon.tick(), AgentKind.STEWARD)  # one leg per issue
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()
    assert len(agent.tasks) == 1

    broke, _ = role(FakeGitHub(issues={5: ready(5)}), SplitAgent(lambda _t: _DONE, triaged()), triaged=False,
                    DAILY_SPEND_CAP_USD="0.5")
    assert not dispatched(tick(broke), AgentKind.STEWARD)  # a 2.0 budget does not fit under a 0.5 cap


def test_many_unstewarded_issues_run_light_legs_up_to_the_concurrency_at_once_and_all_finish(role):
    numbers = range(5, 11)
    gh = FakeGitHub(issues={n: ready(n, f"Bug {n}") for n in numbers})
    gate = threading.Event()
    agent = SplitAgent(lambda _t: _DONE, _blocked(gate, triaged()))
    lines: list[str] = []
    composed, _ = role(gh, agent, lines.append, triaged=False, FIX_DISPATCH_CONCURRENCY="2")
    try:
        assert len(dispatched(composed.daemon.tick(), AgentKind.STEWARD)) == 2  # the ceiling is the concurrency
        composed.daemon.tick()
        assert not [line for line in lines if "not submitted" in line]  # decide never over-chooses
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()
    for _ in range(40):
        tick(composed)
    graded = [r for r in composed.control.leg_runs("o/r", tuple(numbers)) if r.kind == "TRIAGE"]
    assert {r.number for r in graded} == set(numbers)


def test_a_repeated_fix_act_escalates_its_model_one_tier_per_attempt(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(*red_pushes(gh, 2))
    composed, _ = role(gh, agent, **ROUTES, FIX_MODEL_MED="claude:claude-sonnet-5-5")
    tick(composed)
    tick(composed)
    # No triage data (a PR from before routing): MED, then one rung up.
    assert [t.model for t in agent.tasks] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    tiers = [r.receipt["tier"] for r in sorted(composed.control.leg_runs("o/r", (5,)), key=lambda r: r.completed_at)
             if r.kind == "FIX_CI"]
    assert tiers == ["med", "high"]


def test_unset_tier_specs_keep_the_single_configured_fixer(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(did_nothing())
    composed, _ = role(gh, agent, FIX_MODEL="claude-sonnet-5-5")
    tick(composed)
    assert agent.tasks[0].model == "claude-sonnet-5-5"


def test_a_routed_legs_task_carries_the_routes_effort_and_an_omitted_one_uses_the_global_knob(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(*red_pushes(gh, 2))
    composed, _ = role(gh, agent, FIX_EFFORT="low", FIX_MODEL_MED="claude:claude-sonnet-5-5:high",
                       FIX_MODEL_HIGH="claude:claude-opus-5-5")
    tick(composed)
    tick(composed)  # MED (route effort), then escalated to HIGH (no route effort)
    assert [t.effort for t in agent.tasks] == [Effort.HIGH, Effort.LOW]


def test_each_light_act_carries_its_own_routes_effort(role):
    gh = FakeGitHub(issues={5: ready()})
    light = triaged("high")
    agent = Agent(light, light)
    composed, _ = role(gh, agent, triaged=False, TRIAGE_MODEL="claude:claude-haiku-5-5:low",
                       STEWARD_MODEL="claude:claude-sonnet-5-5:xhigh")
    tick(composed)
    tick(composed)
    assert [(t.model, t.effort) for t in agent.tasks] == [
        ("claude-sonnet-5-5", Effort.XHIGH), ("claude-haiku-5-5", Effort.LOW)]


def test_an_unset_steward_route_runs_on_the_triage_route(role):
    gh = FakeGitHub(issues={5: ready()})
    light = triaged("high")
    agent = Agent(light, light)
    composed, _ = role(gh, agent, triaged=False, TRIAGE_MODEL="claude:claude-haiku-5-5:low")
    tick(composed)
    tick(composed)
    assert [(t.model, t.effort) for t in agent.tasks] == [("claude-haiku-5-5", Effort.LOW)] * 2


def test_the_recorded_leg_result_names_the_effort_it_ran_at(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    composed, _ = role(gh, Agent(did_nothing()), FIX_MODEL_MED="claude:claude-sonnet-5-5:high")
    tick(composed)
    (run,) = (r for r in composed.control.leg_runs("o/r", (5,)) if r.kind == "FIX_CI")
    assert (run.receipt["tier"], run.receipt["effort"]) == ("med", "high")


def test_a_blocked_issue_buys_nothing_until_its_blocker_closes_then_starts_by_itself(role):
    gh = FakeGitHub(issues={5: replace(ready(), blocked_by=(BlockerRef(9, "o/lib"),))})
    agent = Agent(pushed(gh, "fix/5-null-token-crashes", "h1"))
    composed, _ = role(gh, agent)

    assert tick(composed) == [] and tick(composed) == []
    assert agent.tasks == [] and gh.prs == {}

    gh.issues[5] = replace(gh.issues[5], blocked_by=())  # GitHub drops a closed blocker from the open set
    assert dispatched(tick(composed), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 5, tier=Tier.MED)]



def stewarded(*, blocked_by: int | None = None, touches: tuple[str, ...] = (), session: str = "t1"):
    """The steward's claim with a dependency and path guesses, or TRIAGE's grade."""
    steward = {"acceptance_criteria": ["Exports open in the viewer"], "duplicate_of": None, "duplicate_reason": "",
               "blocked_by": blocked_by, "blocked_by_reason": "Needs the export API #2 adds" if blocked_by else "",
               "touches": list(touches)}

    def run(task):
        claim = {"tier": "med", "rationale": "x"} if "tier" in task.schema["properties"] else steward
        return AgentResult(False, structured=claim, cost_usd=0.01, session_id=session)
    return run


def _linked_issue(role):
    # #2 is the harness's own work (ai-ready), held behind #3, a human's design issue.
    gh = FakeGitHub(issues={5: ready(), 2: replace(ready(2, "Add the export API"), blocked_by=(BlockerRef(3),)),
                            3: IssueView(3, title="Design the export format")})
    light = stewarded(blocked_by=2)
    agent = Agent(light, light)
    composed, _ = role(gh, agent, triaged=False)
    assert dispatched(tick(composed), AgentKind.STEWARD) == [RunAgent(AgentKind.STEWARD, 5)]
    assert agent.tasks[0].schema["properties"]["blocked_by"]["enum"] == [2, None]
    assert "Overlap is not a dependency" in agent.tasks[0].prompt
    tick(composed)  # the link, then its one comment
    assert gh.issues[5].blocked_by == (BlockerRef(2),)
    assert len(gh.comments_with("issue", 5, "blocked by #2")) == 1
    assert tick(composed) == []  # held: nothing is bought while #2 is open
    return gh, agent, composed


def test_a_stewards_dependency_is_linked_holds_the_issue_and_releases_when_the_blocker_closes(role):
    gh, agent, composed = _linked_issue(role)
    gh.close_issue(2, "completed", "")
    assert dispatched(tick(composed), AgentKind.TRIAGE) == [RunAgent(AgentKind.TRIAGE, 5)]


@pytest.mark.parametrize("labels", [frozenset(), frozenset({Label.AI_READY, Label.NEEDS_HUMAN})])
def test_a_steward_is_offered_as_blockers_only_open_issues_the_harness_will_work_on(role, labels):
    gh = FakeGitHub(issues={5: ready(), 2: IssueView(2, title="Epic: exports", labels=labels),
                            4: replace(ready(4, "Add the export API"), blocked_by=(BlockerRef(3),)),
                            3: IssueView(3, title="Design the export format"),
                            6: replace(ready(6, "Old export"), state="closed")})
    agent = Agent(stewarded(), stewarded())
    composed, _ = role(gh, agent, triaged=False)
    tick(composed)
    assert agent.tasks[0].schema["properties"]["blocked_by"]["enum"] == [4, None]


def test_a_link_a_human_deletes_is_never_re_added(role):
    gh, agent, composed = _linked_issue(role)
    gh.issues[5] = replace(gh.issues[5], blocked_by=())  # deleted on GitHub
    assert dispatched(tick(composed), AgentKind.TRIAGE) == [RunAgent(AgentKind.TRIAGE, 5)]
    tick(composed)
    assert gh.issues[5].blocked_by == () and len(gh.comments_with("issue", 5, "blocked by #2")) == 1


def test_a_free_slot_goes_to_the_oldest_issue_clear_of_open_pr_files_and_otherwise_fifo(role):
    gh = FakeGitHub(issues={5: ready(5), 6: ready(6, "Docs typo"), 7: ready(7, "In flight")})
    open_pr(gh, 7, changed_files=frozenset({"src/auth/token.ts"}))
    gate = threading.Event()
    agent = SplitAgent(_blocked(gate, _DONE), triaged())
    composed, _ = role(gh, agent, triaged=False, FIX_DISPATCH_CONCURRENCY="1")
    for number, touches in ((5, ["src/auth/"]), (6, ["docs/typo.md"])):
        composed.control.record_unpaid_leg(
            RunId(uuid.uuid4().hex), repository="o/r", role="fix", kind="STEWARD", owner_id="seed",
            issue=number, number=number, head="", runtime="claude", leg_class="agent",
            result={"steward": {"acceptance_criteria": [], "duplicate_of": None, "duplicate_reason": "",
                                "touches": touches}})
        composed.control.record_unpaid_leg(  # an unusable grade: no triage comment is owed
            RunId(uuid.uuid4().hex), repository="o/r", role="fix", kind="TRIAGE", owner_id="seed",
            issue=number, number=number, head="", runtime="claude", leg_class="agent", result={})
    try:
        # #5 waited longer but guesses src/auth/, which PR 100 changes: #6 gets the one slot.
        assert dispatched(composed.daemon.tick(), AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 6, tier=Tier.MED)]
    finally:
        gate.set()
        composed.daemon.wait_for_dispatches()


def test_deleting_both_the_link_and_its_comment_closes_the_matter_and_triage_proceeds(role):
    gh, agent, composed = _linked_issue(role)
    gh.issues[5] = replace(gh.issues[5], blocked_by=(), comments=())  # a human removes both
    tick(composed)  # one closing comment, no new link
    assert gh.issues[5].blocked_by == () and len(gh.comments_with("issue", 5, "will not write that link again")) == 1
    assert dispatched(tick(composed), AgentKind.TRIAGE) == [RunAgent(AgentKind.TRIAGE, 5)]


def test_merging_cancels_every_unfinished_run_on_the_merged_branch_only(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=_APPROVED)
    gh.runs = {1: "fix/5-null-token-crashes", 2: "fix/5-null-token-crashes", 3: "dev", 4: "fix/9-someone-else"}
    composed, _ = role(gh, Agent())

    assert MergePullRequest(100, "h1") in tick(composed)
    assert gh.cancelled == [1, 2]


# ── gap 22: one issue per workflow red on the base branch ────────────────────


def base_run(workflow: str, run: int, conclusion: str, minutes: float) -> BaseRun:
    """A completed push run on dev that finished `minutes` from now (a later run: more minutes)."""
    at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    return BaseRun(workflow, run, f"https://github.com/o/r/actions/runs/{run}", f"sha{run}", conclusion, at, at)


def base_issues(gh: FakeGitHub) -> dict[str, IssueView]:
    found = [i for i in gh.issues.values() if i.title.startswith("CI: ")]
    assert len({i.title for i in found}) == len(found), "two issues for one workflow"
    return {i.title: i for i in found}


def test_a_base_failure_files_one_unlabelled_issue_naming_the_run_and_head(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 1, "success", -30),
                               base_run("macOS Smoke", 2, "failure", -20), base_run("macOS Smoke", 3, "failure", -10)])
    composed, _ = role(gh, Agent())
    tick(composed)

    (issue,) = base_issues(gh).values()
    assert issue.title == "CI: macOS Smoke failing on dev" and issue.labels == frozenset() and issue.state == "open"
    assert "https://github.com/o/r/actions/runs/2 (head `sha2`)" in issue.body  # the streak's first failing run
    assert gh.observed_by[-1] == FIX_BOT


def test_repeated_ticks_on_a_still_red_workflow_file_nothing_more(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -20)])
    composed, _ = role(gh, Agent())
    tick(composed)
    gh.base_runs.append(base_run("macOS Smoke", 3, "failure", -10))
    tick(composed)
    tick(composed)
    assert len(base_issues(gh)) == 1


def test_a_flaky_suite_keeps_one_issue_closed_on_green_and_reopened_on_red(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -20)])
    composed, _ = role(gh, Agent())
    tick(composed)
    (issue,) = base_issues(gh).values()

    gh.base_runs.append(base_run("macOS Smoke", 4, "success", -5))
    tick(composed)
    assert gh.issues[issue.number].state == "closed"
    (note,) = gh.comments_with("issue", issue.number, "passes on `dev` again")
    assert "actions/runs/4 (head `sha4`)" in note.body
    tick(composed)
    assert gh.closed == [issue.number]  # closed once

    gh.base_runs.append(base_run("macOS Smoke", 5, "failure", 5))  # finished after the close
    tick(composed)
    assert list(base_issues(gh).values()) == [gh.issues[issue.number]] and gh.issues[issue.number].state == "open"
    (again,) = gh.comments_with("issue", issue.number, "failing on `dev` again")
    assert "actions/runs/5 (head `sha5`)" in again.body

    gh.base_runs.append(base_run("macOS Smoke", 6, "success", 6))
    tick(composed)
    assert gh.issues[issue.number].state == "closed"
    gh.base_runs.append(base_run("macOS Smoke", 7, "failure", 30))
    tick(composed)
    assert len(base_issues(gh)) == 1 and gh.issues[issue.number].state == "open"  # never a second issue


def test_a_harness_close_reopens_on_the_next_red_even_once_the_green_has_left_the_listing(role):
    gh = FakeGitHub(base_runs=[base_run("Docs", 2, "failure", -20)])
    composed, _ = role(gh, Agent())
    tick(composed)
    (issue,) = base_issues(gh).values()
    gh.base_runs.append(base_run("Docs", 3, "success", -10))
    tick(composed)
    assert gh.issues[issue.number].state == "closed"
    gh.base_runs[:] = [base_run("Docs", 4, "failure", 5)]  # the earlier runs slid off the one listed page
    tick(composed)
    assert gh.issues[issue.number].state == "open" and len(base_issues(gh)) == 1


def test_a_human_close_during_the_red_streak_holds_until_it_passes_and_fails_again(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -20)])
    composed, _ = role(gh, Agent())
    tick(composed)
    (issue,) = base_issues(gh).values()
    gh.close_issue(issue.number, "not planned", "", author="alice")  # still red
    gh.base_runs.append(base_run("macOS Smoke", 3, "failure", 5))
    tick(composed)
    assert gh.issues[issue.number].state == "closed"

    gh.base_runs += [base_run("macOS Smoke", 4, "success", 6), base_run("macOS Smoke", 5, "failure", 10)]
    tick(composed)
    assert gh.issues[issue.number].state == "open" and len(base_issues(gh)) == 1


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
def test_cancelled_or_skipped_runs_neither_file_nor_close(role, conclusion):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "success", -30),
                               base_run("macOS Smoke", 3, conclusion, -25)])
    composed, _ = role(gh, Agent())
    tick(composed)
    assert base_issues(gh) == {}  # green under a cancelled run is still green

    gh.base_runs.append(base_run("macOS Smoke", 4, "failure", -20))
    tick(composed)
    (issue,) = base_issues(gh).values()
    gh.base_runs.append(base_run("macOS Smoke", 5, conclusion, -10))
    tick(composed)
    assert gh.issues[issue.number].state == "open" and gh.closed == []  # red under a cancelled run is still red


def test_two_failing_workflows_file_two_issues(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -5),
                               base_run("Windows smoke", 3, "timed_out", -5), base_run("CI", 4, "success", -5)])
    composed, _ = role(gh, Agent())
    tick(composed)
    tick(composed)
    assert sorted(base_issues(gh)) == ["CI: Windows smoke failing on dev", "CI: macOS Smoke failing on dev"]


def test_a_lost_reply_on_filing_files_exactly_one_issue(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -5)])
    gh.lose_reply.add("create_issue")
    composed, _ = role(gh, Agent())
    tick(composed)  # the issue landed; act raised and the tick logged it
    gh.unindexed = {i.number for i in base_issues(gh).values()}  # the search lags the filing
    tick(composed)
    gh.unindexed = set()
    tick(composed)
    assert len(base_issues(gh)) == 1


def test_a_deleted_issue_of_a_still_red_workflow_is_filed_again_once_then_quiet(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -5)])
    composed, _ = role(gh, Agent())
    tick(composed)
    (issue,) = base_issues(gh).values()
    del gh.issues[issue.number]  # a human deleted it
    tick(composed)
    (again,) = base_issues(gh).values()
    gh.unindexed = {again.number}  # the search lags the new filing
    tick(composed)
    gh.unindexed = set()
    assert list(base_issues(gh).values()) == [again] and again.state == "open"  # filed again, once


def test_an_unread_base_files_and_closes_nothing(role):
    gh = FakeGitHub(base_runs=[base_run("macOS Smoke", 2, "failure", -5)])
    composed, _ = role(gh, Agent())
    gh.build_world = lambda base, **_: WorldState(now=_now())  # type: ignore[method-assign]
    tick(composed)
    assert base_issues(gh) == {}


# ── a repair carries its evidence (tauri #1078) ─────────────────────────────


def _with_sibling(gh: FakeGitHub, bucket: str, job: int = 7) -> None:
    """The run's non-required sibling job (#1078: windows smoke ran ~50 min), begun with its run."""
    pr = gh.prs[100]
    assert isinstance(pr.ci, Observed)
    state = {"pending": "IN_PROGRESS", "fail": "FAILURE"}.get(bucket, "SUCCESS")  # a completed run reports its conclusion
    first = pr.ci.reports[0]
    windows = Check("smoke (windows)", state=state, bucket=bucket, started_at=first.started_at, run_id=900,
                    check_id=job, run_attempt=1)
    gh.prs[100] = replace(pr, ci=replace(pr.ci, reports=(first, windows)))


def test_a_fix_ci_leg_is_told_which_jobs_failed_and_reads_their_logs_itself(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    job = sum(map(ord, "h1")) * 100 + 1
    assert f"gh api --allow-escape-sequences repos/o/r/actions/jobs/{job}/logs" in agent.tasks[0].prompt
    assert "gh run rerun" not in agent.tasks[0].prompt  # the harness re-runs, never the agent


def test_an_address_review_leg_reads_the_reviews_inline_comments_itself(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, reviews=(Review("CHANGES_REQUESTED", "alice", body="", submitted_at=_now(), commit_id="h1", id=77),))
    agent = Agent(did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.ADDRESS_REVIEW)  # an inline-only review dispatches too
    assert "gh api --paginate repos/o/r/pulls/100/reviews/77/comments" in agent.tasks[0].prompt


def test_a_repair_spans_the_ci_run_resuming_its_session_once_per_new_failure(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    _with_sibling(gh, "pending")  # the first failure lands while a sibling job still runs
    agent = Agent(did_nothing(session="s-ci"), did_nothing(session="s-ci"))
    # A climbed tier would route to another runtime, which cannot resume the session.
    composed, _ = role(gh, agent, FIX_MODEL_MED="claude:claude-sonnet-5-5", FIX_MODEL_HIGH="codex:gpt-6-luna")

    first = dispatched(tick(composed), AgentKind.FIX_CI)
    assert first and [f.name for f in first[0].evidence.failures] == ["test"]
    assert first[0].evidence.running == ("smoke (windows)",)
    assert tick(composed) == []  # the same evidence, the sibling still running: nothing to buy

    _with_sibling(gh, "fail")
    second = dispatched(tick(composed), AgentKind.FIX_CI)
    assert second and [f.name for f in second[0].evidence.failures] == ["smoke (windows)"]
    assert agent.tasks[1].resume == "s-ci"  # the same session, handed only the new failure
    assert [t.model for t in agent.tasks] == ["claude-sonnet-5-5"] * 2  # a resume is no attempt: no tier climb
    tiers = [r.receipt["tier"] for r in composed.control.leg_runs("o/r", (5,)) if r.kind == "FIX_CI"]
    assert tiers == ["med", "med"]
    assert "repos/o/r/actions/jobs/7/logs" in agent.tasks[1].prompt
    assert "handed to an earlier leg on this head: test" in agent.tasks[1].prompt

    tick(composed)  # nothing pushed, complete, nothing new, never re-run: the harness re-runs the failed jobs once
    assert len(agent.tasks) == 2 and gh.harness_reruns == [900] and Label.NEEDS_HUMAN not in gh.prs[100].labels



def test_a_failure_after_the_harness_rerun_is_a_counted_attempt_and_a_second_leave_stops(role):
    gh = FakeGitHub(issues={5: ready()})  # the harness's re-run fails again
    open_pr(gh, ci="fail")
    agent = Agent(did_nothing(), did_nothing(), did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    tick(composed)
    assert gh.harness_reruns == [900]
    second = dispatched(tick(composed), AgentKind.FIX_CI)
    assert second and second[0].tier is Tier.HIGH  # a re-run is a new attempt: the tier climbs
    for _ in range(3):
        tick(composed)  # the re-run head left unrepaired: one stop, never a second re-run
    assert len(agent.tasks) == 2 and gh.harness_reruns == [900] and Label.NEEDS_HUMAN in gh.prs[100].labels
    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1


def test_a_flake_left_while_other_jobs_still_run_ends_in_a_harness_rerun_not_needs_human(role):
    gh = FakeGitHub(issues={5: ready()}, rerun_result="pass")
    open_pr(gh, ci="fail")
    _with_sibling(gh, "pending")
    agent = Agent(did_nothing())  # the agent judged `test` a flake and pushed nothing
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    _with_sibling(gh, "pass")
    tick(composed)
    assert gh.harness_reruns == [900] and Label.NEEDS_HUMAN not in gh.prs[100].labels
    for _ in range(2):
        tick(composed)
    assert len(agent.tasks) == 1 and Label.NEEDS_HUMAN not in gh.prs[100].labels  # the re-run went green


def test_a_repair_that_leaves_a_rerun_heads_failures_unrepaired_stops_for_a_human_once(role):
    # Not bought again on the same evidence, and not parked silently either.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    gh.rerun(100)  # someone re-ran this head already: the harness's one re-run is spent
    agent = Agent(did_nothing(), did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    for _ in range(3):
        assert not dispatched(tick(composed), AgentKind.FIX_CI)
    assert len(agent.tasks) == 1 and Label.NEEDS_HUMAN in gh.prs[100].labels
    assert len(gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))) == 1

    gh.comment("pr", 100, "It was a flake; try again.", author="alice")
    tick(composed)  # released
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # a human reset re-arms the same evidence
    assert len(agent.tasks) == 2


def test_a_push_github_does_not_show_yet_is_waited_on_then_repaired_once_shown(role):
    # Read-your-writes (PR #403): the harness reads the branch on the remote after the leg.
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(unseen_push(gh, "fix/5-null-token-crashes", "h2"), did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    for _ in range(3):  # h1's CI is no longer the PR's: no re-run, no stop, no repair
        assert tick(composed) == []
    assert gh.harness_reruns == [] and Label.NEEDS_HUMAN not in gh.prs[100].labels

    gh.push("fix/5-null-token-crashes", "h2", "fail")  # GitHub catches up
    second = dispatched(tick(composed), AgentKind.FIX_CI)
    assert second and second[0].head == "h2"


def test_a_new_run_on_the_head_after_a_leg_is_a_counted_try_and_a_pre_existing_runs_failure_resumes(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    _with_sibling(gh, "pending")
    agent = Agent(did_nothing(), did_nothing(), did_nothing())
    composed, _ = role(gh, agent)
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    _with_sibling(gh, "fail")  # the run that already existed fails later: the same try
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    pr = gh.prs[100]
    assert isinstance(pr.ci, Observed)
    lint = Check("lint", state="FAILURE", bucket="fail", started_at=_now(), run_id=901, check_id=4242, run_attempt=1)
    gh.prs[100] = replace(pr, ci=replace(pr.ci, reports=(*pr.ci.reports, lint)))  # e.g. a label-triggered workflow
    assert dispatched(tick(composed), AgentKind.FIX_CI)
    tiers = [r.receipt["tier"] for r in sorted(composed.control.leg_runs("o/r", (5,)), key=lambda r: r.completed_at)
             if r.kind == "FIX_CI"]
    assert tiers == ["med", "med", "high"]


def test_a_rerun_attempt_and_a_new_head_each_dispatch_again(role):
    gh = FakeGitHub(issues={5: ready()})
    open_pr(gh, ci="fail")
    agent = Agent(reran(gh), pushed(gh, "fix/5-null-token-crashes", "h2", ci="fail"), did_nothing())
    composed, _ = role(gh, agent)

    assert dispatched(tick(composed), AgentKind.FIX_CI)  # reruns: the new attempt fails
    assert dispatched(tick(composed), AgentKind.FIX_CI)  # new attempt's evidence; pushes h2, which fails
    third = dispatched(tick(composed), AgentKind.FIX_CI)
    assert third and third[0].head == "h2" and not third[0].evidence.handled  # a new head starts fresh
    assert len({key for t in agent.tasks for key in t.evidence}) == 3


def test_an_unreadable_issue_index_dispatches_no_steward(role, later):
    gh = FakeGitHub(issues={5: ready()})
    lines: list[str] = []
    agent = Agent(triaged())
    composed, _ = role(gh, agent, lines.append, triaged=False)
    index = gh.issue_index

    def unreadable():
        raise RuntimeError("issue listing is unreadable")

    gh.issue_index = unreadable
    tick(composed)
    assert any("STEWARD #5: not dispatched — issue index unavailable" in x for x in lines)
    gh.issue_index = index
    later()
    assert dispatched(tick(composed), AgentKind.STEWARD)
