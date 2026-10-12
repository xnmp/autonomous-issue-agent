"""The composed review role (slice 5c) beside the fix role: production wiring,
one real SQLite authority, one fake GitHub.

Each test ticks `compose_review_daemon` (and `compose_fix_daemon` where the
split of work matters) and asserts on what GitHub ends up holding (reviews on
which commit, labels, comments, merges) and on how many paid legs ran.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.config import from_env
from issue_agent.app.roles import compose_fix_daemon, compose_review_daemon
from issue_agent.domain.actions import AgentKind, MergePullRequest, RunAgent
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.review import Verdict
from issue_agent.domain.state import StopReason, stop_tag
from issue_agent.domain.world import PullRequestDiff, Review
from tests.app.test_fix_role import (
    FIX_BOT, REVIEW_BOT, Agent, FakeGitHub, FakeWorktrees, _ci, _now, died, dispatched, open_pr, ready, tick,
)


@dataclass
class ReviewGitHub(FakeGitHub):
    """FakeGitHub plus the review bot's reads and writes. A submitted review
    lands on the commit it names, the way GitHub records `commit_id`."""

    submitted: list[tuple[int, str, str, str]] = field(default_factory=list)  # (pr, event, body, head)
    refuse_submit: int = 0  # submissions GitHub refuses before landing (a 502)

    def pr_diff(self, pr: int, base_oid: str, head_oid: str) -> PullRequestDiff:
        return PullRequestDiff(base_oid, head_oid, "diff --git a/x.py b/x.py\n+fix\n")

    def reviewed_on(self, pr: int, head: str, login: str) -> bool:
        return any(r.author == login and r.commit_id == head and r.state in ("APPROVED", "CHANGES_REQUESTED")
                   for r in self.prs[pr].reviews)

    def submit_review(self, pr: int, event: str, body: str, head: str) -> bool:
        if self.refuse_submit:
            self.refuse_submit -= 1
            raise RuntimeError("HTTP 502 from the reviews endpoint")
        with self.lock:
            self.submitted.append((pr, event, body, head))
            state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[event]
            p = self.prs[pr]
            self.prs[pr] = replace(p, reviews=(*p.reviews, Review(state, REVIEW_BOT, submitted_at=_now(),
                                                                  body=body, commit_id=head)))
        self._lossy("submit_review")
        return True


class FakeCheckouts:
    def __init__(self, root: Path):
        self.root = root
        self.pinned: list[tuple[int, str]] = []

    def ensure(self, pr: int, head: str) -> str:
        self.pinned.append((pr, head))
        path = self.root / f"review-pr-{pr}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def remove(self, pr: int) -> None:
        pass


def verdict(event: str, body: str = "Looks right."):
    return lambda _task: AgentResult(False, structured={"decision": event, "body": body}, cost_usd=0.2,
                                     session_id="r1")


def garbled(_task):
    return AgentResult(False, structured={"decision": "LGTM!!", "body": 7}, cost_usd=0.2, session_id="r1")


@pytest.fixture
def roles(tmp_path):
    def cfg(**env):
        return replace(
            from_env({
                "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEW_BOT": REVIEW_BOT, "BASE_BRANCH": "dev",
                "STATE_DIR": str(tmp_path / "state"), "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
                "REVIEWER_RUNTIME": "claude", **env,
            }),
            control_authority_root=str(tmp_path / "machine-state"),
        )

    class Roles:
        checkouts = FakeCheckouts(tmp_path / "checkouts")

        def review(self, gh, agent, **env):
            c = cfg(**env)
            return compose_review_daemon(c, gh, Store(c.review_state_dir), agent, checkouts=self.checkouts,
                                         log=lambda _line: None)

        def fix(self, gh, agent, **env):
            c = cfg(**env)
            return compose_fix_daemon(c, gh, Store(c.state_dir), FakeWorktrees(tmp_path / "worktrees"), agent,
                                      log=lambda _line: None)
    return Roles()


@pytest.fixture
def later(monkeypatch):
    """Moves the roles' tick clock past every machine backoff."""
    from datetime import datetime, timedelta

    import issue_agent.app.tick as tick_module

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=2)

    return lambda: monkeypatch.setattr(tick_module, "datetime", Later)


def review_legs(composed, pr: int = 100) -> list:
    return [r for r in composed.control.leg_runs("o/r", (5,)) if r.kind == "REVIEW" and r.number == pr]


# ── the design table's REVIEW row ────────────────────────────────────────────

def test_a_green_head_without_a_bot_review_gets_one_review_leg_and_one_review_on_that_commit(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    agent = Agent(verdict("APPROVE", "Correct and tested."))
    review = roles.review(gh, agent)

    assert dispatched(tick(review), AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h1")]
    assert tick(review) == []  # reviewed: nothing more is owed on h1

    assert gh.submitted == [(100, "APPROVE", "Correct and tested.", "h1")]
    assert len(agent.tasks) == 1 and roles.checkouts.pinned == [(100, "h1")]
    task = agent.tasks[0]
    assert task.cwd.endswith("review-pr-100") and "diff --git a/x.py" in task.prompt


def test_review_waits_for_green_ci(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pending")
    review = roles.review(gh, Agent())

    assert tick(review) == []
    assert gh.submitted == []


def test_a_review_on_a_moved_head_is_skipped_without_a_paid_leg(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    agent = Agent()
    review = roles.review(gh, agent)
    original = gh.get_pr

    def moved(number, **kw):
        gh.push("fix/5-null-token-crashes", "h2", ci="pass")
        return original(number, **kw)

    gh.get_pr = moved  # the author pushes between the decision and the leg
    tick(review)
    assert agent.tasks == [] and gh.submitted == []


# ── idempotent submission ────────────────────────────────────────────────────

def test_a_crash_after_the_review_lands_never_posts_a_second_review(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    gh.lose_reply.add("submit_review")  # GitHub records the review; the reply is lost
    agent = Agent(verdict("REQUEST_CHANGES", "Handle None."))
    review = roles.review(gh, agent)
    tick(review)

    restarted = roles.review(gh, Agent())
    for _ in range(3):
        tick(restarted)
    composed = roles.review(gh, Agent())
    composed.act.submit_review(100, "h1", Verdict("REQUEST_CHANGES", "Handle None."))  # a replayed submit

    assert gh.submitted == [(100, "REQUEST_CHANGES", "Handle None.", "h1")]
    assert len(agent.tasks) == 1


def test_a_refused_submission_is_resubmitted_after_backoff_without_buying_the_review_again(roles, later):
    gh = ReviewGitHub(issues={5: ready()}, refuse_submit=1)
    open_pr(gh, head="h1", ci="pass")
    agent = Agent(verdict("APPROVE"))
    review = roles.review(gh, agent)
    tick(review)
    assert gh.submitted == []
    assert tick(review) == []  # the refusal is a machine end: backoff, no stop

    later()
    assert dispatched(tick(review), AgentKind.REVIEW)
    assert gh.submitted == [(100, "APPROVE", "Looks right.", "h1")]
    assert len(agent.tasks) == 1


# ── the split of work between the two roles ──────────────────────────────────

def test_a_bot_change_request_is_addressed_by_the_fix_role_not_reviewed_again(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass", reviews=(
        Review("CHANGES_REQUESTED", REVIEW_BOT, body="Handle None.", submitted_at=_now(), commit_id="h1"),))
    review = roles.review(gh, Agent())
    fix = roles.fix(gh, Agent(lambda _t: AgentResult(False, status="pushed", cost_usd=0.1, session_id="s1")))

    assert tick(review) == []
    assert dispatched(tick(fix), AgentKind.ADDRESS_REVIEW) == [RunAgent(AgentKind.ADDRESS_REVIEW, 100, head="h1")]


def test_an_approval_with_a_red_required_check_never_merges(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="fail", reviews=(
        Review("APPROVED", REVIEW_BOT, submitted_at=_now(), commit_id="h1"),))
    review = roles.review(gh, Agent())
    fix = roles.fix(gh, Agent(lambda _t: AgentResult(False, status="pushed", cost_usd=0.1, session_id="s1")))

    assert tick(review) == []
    taken = tick(fix)
    assert not any(isinstance(a, MergePullRequest) for a in taken)
    assert dispatched(taken, AgentKind.FIX_CI) and gh.merged == []


def test_a_push_after_approval_is_reviewed_again_on_the_new_head(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    agent = Agent(verdict("APPROVE"), verdict("APPROVE", "Still right."))
    review = roles.review(gh, agent)
    tick(review)

    gh.push("fix/5-null-token-crashes", "h2", ci="pass")
    assert dispatched(tick(review), AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h2")]
    assert [s[3] for s in gh.submitted] == ["h1", "h2"]


def test_three_unparseable_verdicts_on_one_head_stop_for_a_human_written_by_the_fix_role(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    agent = Agent(garbled, garbled, garbled)
    review = roles.review(gh, agent)
    fix = roles.fix(gh, Agent())
    for _ in range(3):
        assert dispatched(tick(review), AgentKind.REVIEW)
    assert tick(review) == []  # capped: the review role writes nothing
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels

    tick(fix)
    tick(fix)
    assert Label.NEEDS_HUMAN in gh.prs[100].labels
    stops = gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))
    assert len(stops) == 1 and "REVIEW" in stops[0].body and stops[0].author == FIX_BOT
    assert gh.submitted == [] and len(agent.tasks) == 3


def test_a_machine_death_counts_no_attempt_and_backs_off(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="pass")
    review = roles.review(gh, Agent(died))
    assert dispatched(tick(review), AgentKind.REVIEW)

    assert tick(review) == []  # backing off: no redispatch, no stop
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels
    legs = review_legs(review)
    assert len(legs) == 1 and legs[0].receipt is None


def test_a_review_leg_and_a_fix_leg_on_different_items_run_concurrently(roles):
    gh = ReviewGitHub(issues={5: ready(), 6: ready(6, "Other bug")})
    open_pr(gh, issue=5, head="h1", ci="pass")
    both = threading.Barrier(2, timeout=10)
    release = threading.Event()

    def reviewing(_task):
        both.wait()
        assert release.wait(timeout=10)
        return AgentResult(False, structured={"decision": "APPROVE", "body": "ok"}, cost_usd=0.2)

    def resolving(_task):
        both.wait()
        assert release.wait(timeout=10)
        return AgentResult(False, status="pushed", cost_usd=0.1, session_id="s6")

    review = roles.review(gh, Agent(reviewing))
    fix = roles.fix(gh, Agent(resolving))
    assert dispatched(review.daemon.tick(), AgentKind.REVIEW)
    gh.prs[100] = replace(gh.prs[100], ci=_ci("h1", "fail"))  # while the review runs, CI goes red
    taken = fix.daemon.tick()
    assert dispatched(taken, AgentKind.RESOLVE) == [RunAgent(AgentKind.RESOLVE, 6, head="")]
    assert not dispatched(taken, AgentKind.FIX_CI)  # the reviewed PR is busy for the fix role
    release.set()
    review.daemon.wait_for_dispatches()
    fix.daemon.wait_for_dispatches()
    assert gh.submitted == [(100, "APPROVE", "ok", "h1")]
