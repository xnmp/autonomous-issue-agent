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
from issue_agent.app.config import from_env
from issue_agent.app.fix import compose_fix_daemon
from issue_agent.domain.actions import AgentKind, MergePullRequest, OpenPullRequest, RunAgent
from issue_agent.domain.contracts import RunId
from issue_agent.domain.effort import Effort
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import StopReason, stop_tag
from issue_agent.domain.world import Branch, Comment, IssueView, PRView, Review, WorldState

FIX_BOT, REVIEW_BOT = "fix-bot", "review-bot"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ci(head: str, bucket: str) -> Observed:
    state = "IN_PROGRESS" if bucket == "pending" else "COMPLETED"
    return Observed(
        head, head, frozenset({RequiredCheck("test")}), (Check("test", state=state, bucket=bucket, required=True),)
    )


@dataclass
class FakeGitHub:
    """Just enough GitHub for the fix role: issues, harness PRs, pushed
    branches, labels and comments. Writes land the way GitHub applies them."""

    issues: dict[int, IssueView] = field(default_factory=dict)
    prs: dict[int, PRView] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)  # name -> pushed head
    pushed_at: dict[str, str] = field(default_factory=dict)  # name -> last push
    closed_at: dict[int, str] = field(default_factory=dict)  # PR -> when a human closed it
    merged: list[tuple[int, str]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    lose_reply: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ── observation ──
    def build_world(self, base: str) -> WorldState:
        with self.lock:
            now = _now()
            return WorldState(
                issues=tuple(i for i in self.issues.values() if i.state == "open"),
                prs=tuple(self.prs.values()),
                branches=tuple(Branch(b, self.pushed_at.get(b, now)) for b in self.branches if self._owed(b)),
                now=now,
            )

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

    def issue_index(self) -> tuple[tuple[int, str, str], ...]:
        return tuple((n, i.state, i.title) for n, i in sorted(self.issues.items()))

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        return self.prs[number]

    def pull_request_for_branch(self, branch: str) -> int | None:
        return next((n for n, p in self.prs.items() if p.head_ref_name == branch and p.state == "OPEN"), None)

    def _subject(self, kind: str, number: int) -> IssueView | PRView:
        return self.issues[number] if kind == "issue" else self.prs[number]

    def has_label(self, kind: str, number: int, label: str) -> bool:
        return any(lab.value == label for lab in self._subject(kind, number).labels)

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        return any(c.body == body for c in self._subject(kind, number).comments)

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

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self.closed.append(number)
        self.issues[number] = replace(self.issues[number], state="closed")

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

    def close_pr(self, number: int) -> None:
        """A human closes the PR unmerged."""
        with self.lock:
            self.prs[number] = replace(self.prs[number], state="CLOSED")
            self.closed_at[number] = _now()

    def comments_with(self, kind: str, number: int, text: str) -> list[Comment]:
        return [c for c in self._subject(kind, number).comments if text in c.body]


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
        worktrees = FakeWorktrees(tmp_path / "worktrees")
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
    assert "410, it is gone for good." in resumed.prompt


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
    composed, _ = role(gh, Agent(did_nothing(), did_nothing(), did_nothing()))
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
    assert retried.resume == "s-q" and "410." in retried.prompt


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
    assert "Mock the clock." in continued and "Also pin the timezone." in continued
    assert "Progress report" not in continued


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
    assert "A null token returns 401, not 500" in gh.prs[100].body
    assert len(gh.comments_with("issue", 5, "Acceptance criteria")) == 1
    assert len(gh.comments_with("issue", 5, "issue-agent:triage")) == 1


def test_an_issue_with_its_own_criteria_keeps_them_as_the_contract(role):
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
    assert tick(composed) == [OpenPullRequest(5, "fix/5-null-token-crashes")]
    assert "The API answers 401" in gh.prs[100].body and "A null token returns 401" not in gh.prs[100].body


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
    agent = Agent(did_nothing(), did_nothing())
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
    agent = Agent(did_nothing(), did_nothing())
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
