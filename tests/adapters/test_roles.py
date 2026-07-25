"""app/roles.py — `_fix_state` round-trips (review C4).

C4: `app/roles.py` had ZERO test references, and `_fix_state` is the READ half
of EVERY circuit breaker `decide_fix` trips on. The review's mutation proved
this: replacing all four `_fix_state` reads with empty dicts left 848/848
tests green, because nothing drove a real counter through the write side, back
through `_fix_state`, into `decide_fix`.

Every test below does exactly that round trip against a REAL `Store(tmp_path)`:

    real write side (AgentDispatcher → FixWorkflow, a scripted runner)
        → roles._fix_state(store, world)
        → domain.fix.decide_fix(world, cfg, state, fixer_busy=False)

and asserts the breaker actually trips: the escalation Action appears and the
corresponding `RunAgent` is withheld. Each is written as a two-phase
(below-cap / at-cap) pair, which doubles as the anti-vacuity twin: phase one
proves the breaker does NOT fire early (a `_fix_state` that always reads 0
would pass phase one trivially but then also fail to trip in phase two).

MUTATION CHECK (done by hand, see the final report): temporarily replacing
every `_fix_state` read with `{}` was applied to `src/issue_agent/app/roles.py`,
this whole module was run, all round-trip tests FAILED (the phase-two breaker
never tripped, so `RunAgent` kept appearing where an escalation was expected),
and the mutation was then reverted. See the agent's final message for the
exact result.
"""

from __future__ import annotations

import asyncio

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import FixTaskConfig, ReviewTaskConfig
from issue_agent.app.roles import _fix_state
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.fix import FixConfig, breaker_key, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.refs import issue_from_branch
from issue_agent.domain.world import (
    Check,
    IssueView,
    PRView,
    Review,
    REVIEW_CHANGES_REQUESTED,
    WorldState,
)

from test_dispatch import FakeRunner, FakeWorktrees, RaisingRunner

FIX_BOT = "fix-bot"


class FakeGateway:
    """A minimal GitHubGateway double whose `issue_for_pr` replicates the REAL
    adapter's fallback (closing_issue, else the branch-parsed issue number —
    see `adapters/github.py:770-776`), unlike `test_dispatch.FakeGateway`
    (which only ever reads `closing_issue`).

    That distinction matters HERE specifically: the C4 key-agreement tests
    below exist to prove `domain.fix.breaker_key` (pure, closing_issue-or-
    branch) and `gateway.issue_for_pr` (the write side's key source) agree.
    A gateway double that skips the branch fallback would either hide a real
    disagreement or fabricate a fake one that has nothing to do with
    production code — so this file gets its own, more faithful double rather
    than importing the simplified one.
    """

    def __init__(self, issues=None, prs=None):
        self._issues = issues or {}
        self._prs = prs or {}
        self.calls = []

    def get_issue(self, n):
        return self._issues[n]

    def get_pr(self, n):
        return self._prs[n]

    def pr_for_issue(self, issue):
        return None

    def issue_for_pr(self, pr):
        p = self._prs[pr]
        if p.closing_issue is not None:
            return p.closing_issue
        return issue_from_branch(p.head_ref_name)

    def pr_diff(self, pr):
        return "diff text"

    def submit_review(self, pr, event, body):
        self.calls.append(("review", pr, event, body))
        return True

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))

    def close_issue(self, number, reason, comment):
        self.calls.append(("close", number))

    def enable_auto_merge(self, pr, method):
        self.calls.append(("automerge", pr))

    def update_branch(self, pr):
        self.calls.append(("update_branch", pr))


def _dispatcher(gw, store, runner, **overrides):
    kwargs = dict(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        review=ReviewTaskConfig("o/r", review_prompt="p"),
        fix_bot=FIX_BOT,
    )
    kwargs.update(overrides)
    cfg = DispatchConfig(**kwargs)
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return AgentDispatcher(cfg, gw, store, FakeWorktrees(), runner, executor)


def _cfg(**overrides):
    kwargs = dict(fix_bot=FIX_BOT)
    kwargs.update(overrides)
    return FixConfig(**kwargs)


def _has_run(actions, kind) -> bool:
    return any(isinstance(a, RunAgent) and a.kind is kind for a in actions)


# ── resolve-retries (issue-keyed) ──────────────────────────────────────────────
def test_resolve_retries_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: two erroring RESOLVE dispatches bump resolve-retries
    via the REAL FixWorkflow.resolve → _bump_resolve_retry. Read: _fix_state
    surfaces the count under the issue number; decide_fix, given the same cap,
    withholds RunAgent(RESOLVE) and escalates instead once the cap is hit."""
    issue = 5
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True))  # error, no PR opened, no infra death
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_resolve_retries=2)

    def world():
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ))

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert store.get_resolve_retries(issue) == 1  # sanity: the write side moved
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.RESOLVE)          # anti-vacuity: below cap, still dispatches
    assert Relabel(Target.ISSUE, issue, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) not in actions

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert store.get_resolve_retries(issue) == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)      # at cap: withheld
    assert Relabel(Target.ISSUE, issue, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) in actions


# ── ci-retries (PR-keyed via closing_issue) ────────────────────────────────────
def _ci_pr(issue):
    return PRView(
        100, head_ref_name="fix/5-x", closing_issue=issue,
        checks=(Check("unit", bucket="fail"),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_ci_retries_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: FIX_CI dispatches bump ci-retries via the REAL
    FixWorkflow.fix_ci. Read: _fix_state keys ci_retries by breaker_key(pr)
    (== the closing issue here); decide_fix withholds RunAgent(FIX_CI) and
    escalates the PR once the cap is hit."""
    issue, pr = 5, 100
    gw = FakeGateway(prs={pr: _ci_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c"))
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_ci_retries=2)

    def world():
        return WorldState(prs=(_ci_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr)))
    assert store.get_ci_retries(issue) == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.FIX_CI)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr)))
    assert store.get_ci_retries(issue) == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.FIX_CI)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


# ── rebase-retries (PR-keyed via closing_issue) ────────────────────────────────
def _rebase_pr(issue):
    return PRView(
        200, head_ref_name="fix/6-y", closing_issue=issue, mergeable="CONFLICTING",
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_rebase_retries_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: REBASE dispatches bump rebase-retries via the REAL
    FixWorkflow.rebase. Read/decide as above, for the CONFLICTING-branch leg."""
    issue, pr = 6, 200
    gw = FakeGateway(prs={pr: _rebase_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True))  # a failed rebase attempt, not an infra death
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_rebase_retries=2)

    def world():
        return WorldState(prs=(_rebase_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, pr)))
    assert store.get_rebase_retries(issue) == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.REBASE)

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, pr)))
    assert store.get_rebase_retries(issue) == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.REBASE)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


# ── review-retry (PR-keyed via closing_issue; (at, count) tuple) ──────────────
def _review_pr(issue, submitted_at="T1"):
    return PRView(
        300, head_ref_name="fix/7-z", closing_issue=issue,
        reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at=submitted_at),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )


def test_review_retry_round_trip_trips_the_breaker(tmp_path):
    """review C4. Write: ADDRESS_REVIEW dispatches bump review-retry (keyed to
    the review's submitted_at) via the REAL FixWorkflow.address_review. Read/
    decide: decide_fix compares the SAME submitted_at before trusting the
    stored count (a superseding review would reset it — not exercised here)."""
    issue, pr = 7, 300
    gw = FakeGateway(prs={pr: _review_pr(issue)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="a"))
    d = _dispatcher(gw, store, runner)
    cfg = _cfg(max_review_retries=2)

    def world():
        return WorldState(prs=(_review_pr(issue),))

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, pr)))
    assert store.get_review_retry(issue) == ("T1", 1)
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.ADDRESS_REVIEW)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, pr)))
    assert store.get_review_retry(issue) == ("T1", 2)
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.ADDRESS_REVIEW)
    assert Relabel(Target.PR, pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


# ── infra-deaths (issue-keyed) ─────────────────────────────────────────────────
def test_infra_deaths_round_trip_trips_the_ceiling_for_an_issue_leg(tmp_path):
    """review C4 / C3. Write: a RESOLVE session that dies pre-result (raises,
    never returns) bumps infra-deaths via AgentDispatcher.dispatch's except
    block. Read/decide: _fix_state surfaces it under the issue number (the
    same key an issue leg's OTHER counters use); decide_fix's infra ceiling —
    separate from, and checked independently of, the resolve-retry breaker —
    trips once the SEPARATE cap is hit."""
    issue = 8
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(gw, store, RaisingRunner())
    cfg = _cfg(max_infra_deaths=2)

    def world():
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ))

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert store.get_infra_deaths(issue) == 1
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert _has_run(actions, AgentKind.RESOLVE)           # 1 death < cap of 2

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert store.get_infra_deaths(issue) == 2
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)
    assert any(
        isinstance(a, Relabel) and a.number == issue and a.add is Label.NEEDS_HUMAN_REVIEW
        for a in actions
    )


# ── C4 key-agreement: breaker_key(pr) vs gateway.issue_for_pr(pr) ──────────────
def test_breaker_key_agrees_with_gateway_issue_for_pr_when_issueless(tmp_path):
    """review C4 (the PR #112 class). A PR with NO closing-ref AND a
    non-numeric branch is genuinely issueless: domain.fix.breaker_key falls
    back to -pr.number.

    FixWorkflow.fix_ci/rebase/address_review all early-return WITHOUT writing
    anything for such a PR (`if issue is None: return`) — and decide_fix's own
    routing never dispatches those kinds on an issueless PR in the first place
    (see fix.py's `issue_n is None` branch), so there is no "workflow write"
    for ci/rebase/review-retry to round-trip here at all. The ONE production
    writer that ever touches a PR's negated key is AgentDispatcher.dispatch's
    exception-bump path (`key = issue if issue is not None else -action.number`),
    which is common to every PR-keyed AgentKind — so REVIEW stands in for it.
    This proves the key FORMULA agrees; see the report for the write-side gap
    this surfaces (not a bug: decide_fix's own gating makes it moot)."""
    pr = PRView(400, head_ref_name="feat/ACME-2468-x")  # no closing_issue, non-numeric branch
    assert issue_from_branch(pr.head_ref_name) is None  # sanity: genuinely issueless
    assert breaker_key(pr) == -400

    gw = FakeGateway(prs={pr.number: pr})
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, pr.number)))

    state = _fix_state(store, WorldState(prs=(pr,)))
    assert state.infra_deaths[breaker_key(pr)] == 1
    assert store.get_infra_deaths(-400) == 1


def test_breaker_key_agrees_with_gateway_issue_for_pr_when_branch_numbered_no_closing_ref(tmp_path):
    """review C4. The other half of #112: NO closing-ref, but the branch DOES
    encode an issue number (the fixer's own branch-naming convention). Both
    key implementations must resolve to the SAME real issue number, never the
    negated PR number — write through FixWorkflow.fix_ci, read through
    breaker_key + _fix_state."""
    issue, pr_no = 42, 401
    pr = PRView(
        pr_no, head_ref_name=f"fix/{issue}-thing", closing_issue=None,
        checks=(Check("unit", bucket="fail"),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )
    assert issue_from_branch(pr.head_ref_name) == issue
    assert breaker_key(pr) == issue  # NOT -pr_no

    gw = FakeGateway(prs={pr_no: pr})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c"))
    d = _dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, pr_no)))

    # The workflow write (FixWorkflow.fix_ci → store.set_ci_retries(issue, …),
    # where `issue` comes from gateway.issue_for_pr) must land under the SAME
    # key `_fix_state` reads via the domain's breaker_key.
    state = _fix_state(store, WorldState(prs=(pr,)))
    assert state.ci_retries[breaker_key(pr)] == 1
    assert store.get_ci_retries(issue) == 1  # explicit: the real issue number, not -401
