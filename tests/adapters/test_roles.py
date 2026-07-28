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
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    REVIEW_DIMENSIONS,
    ReviewTaskConfig,
)
from issue_agent.app.roles import (
    _ci_stall_reporter,
    _fix_state,
    _hold_reporter,
    _recurrence_reporter,
    _review_state,
)
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.fix import FixConfig, FixState, breaker_key, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.refs import issue_from_branch
from issue_agent.domain.review import ReviewConfig, ReviewMode, decide_review
from issue_agent.domain.world import (
    Check,
    IssueView,
    PRView,
    Review,
    REVIEW_CHANGES_REQUESTED,
    WorldState,
)

from test_dispatch import (
    FakeRunner,
    FakeWorktrees,
    RaisingRunner,
    SchemaRunner,
    _gate,
    _val,
)

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
        # Pinned model: keeps the difficulty-router leg out of these scripted
        # sequences (see test_fix_workflow's routing tests for the unpinned path).
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
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


# ══════════════════════════════════════════════════════════════════════════════
# review P0R-3 — the reviewer's ceiling, measured in PAID DISPATCHES
#
# `decide_review` emitted one RunAgent per eligible PR per poll with no
# `consumed` gate and no breaker read: a review that never persists state is
# bought again on the very next poll, forever. These tests assert on the money
# (how many dispatches the loop bought over 20 polls), not on a label, and they
# feed the escalation's own relabel back into the observed world so the
# structural stop — the PR leaving `prs_with(trigger_label)` — is exercised
# rather than assumed.
# ══════════════════════════════════════════════════════════════════════════════
REVIEW_BOT = "review-bot"


def _review_dispatcher(gw, store, runner, **overrides):
    kwargs = dict(review=ReviewTaskConfig("o/r", review_prompt="p"), review_bot=REVIEW_BOT)
    kwargs.update(overrides)
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return AgentDispatcher(DispatchConfig(**kwargs), gw, store, None, runner, executor)


def _simulate_review_polls(store, dispatcher, rcfg, base_pr, polls):
    """`polls` review-daemon ticks: observe → decide_review → execute.

    Returns (paid dispatches, escalation comments, the PR's final label set).
    Relabel actions are applied to the observed world exactly as GitHub would,
    so a poll after an escalation sees the PR the way the next real poll does.
    `Daemon.tick` swallows a dispatch that raises, so this does too — that
    swallow is precisely why a pre-result death used to cost nothing but money.
    """
    labels = set(base_pr.labels)
    dispatches = comments = 0
    for _ in range(polls):
        observed = replace(base_pr, labels=frozenset(labels))
        w = WorldState(prs=(observed,))
        for action in decide_review(w, rcfg, _review_state(store, w)):
            if isinstance(action, RunAgent):
                dispatches += 1
                try:
                    asyncio.run(dispatcher.dispatch(action))
                except BaseException:  # noqa: BLE001 — Daemon.tick's swallow
                    pass
            elif isinstance(action, Relabel):
                labels.discard(action.remove) if action.remove else None
                labels.add(action.add) if action.add else None
            else:
                comments += 1
    return dispatches, comments, labels


def _open_pr(number=500, issue=9, sha="abc"):
    return PRView(
        number, head_ref_name=f"fix/{issue}-x", head_ref_oid=sha, closing_issue=issue,
        title="t", body="b", labels=frozenset({Label.AI_PR_OPEN}), state="OPEN",
        author=FIX_BOT,
    )


def test_a_review_that_always_dies_pre_result_is_bounded_at_the_ceiling(tmp_path):
    """review P0R-3, the headline repro. 20 polls against a runner that dies
    before returning a result used to buy 20 full REVIEW sessions with every
    counter reading zero. The dispatcher already bumped `infra-deaths` in the
    review STATE_DIR — nothing read it."""
    pr_view = _open_pr()
    gw = FakeGateway(prs={pr_view.number: pr_view})
    store = Store(state_dir=str(tmp_path))
    d = _review_dispatcher(gw, store, RaisingRunner())
    rcfg = ReviewConfig(review_bot=REVIEW_BOT, max_unproductive_rounds=5)

    dispatches, comments, labels = _simulate_review_polls(store, d, rcfg, pr_view, 20)

    assert dispatches == 5, "20 polls must not buy 20 review sessions"
    assert comments == 1, "the handoff is announced once, not once per poll"
    assert Label.NEEDS_HUMAN_REVIEW in labels and Label.AI_PR_OPEN not in labels
    assert store.get_infra_deaths(9) == 5  # written under the linked issue number


def test_without_the_ceiling_the_same_run_buys_a_session_every_poll(tmp_path, monkeypatch):
    """The ABSENCE test (re-review §5): delete the handling, assert the loud
    failure. With `unproductive_ceiling_hit` stubbed to False — the pre-P0R-3
    code — the identical scenario buys one paid session per poll, forever. If
    this ever reports 5 the test above has stopped proving anything."""
    import issue_agent.domain.review as review_dom

    monkeypatch.setattr(review_dom, "unproductive_ceiling_hit", lambda *_a: False)
    pr_view = _open_pr()
    gw = FakeGateway(prs={pr_view.number: pr_view})
    store = Store(state_dir=str(tmp_path))
    d = _review_dispatcher(gw, store, RaisingRunner())
    rcfg = ReviewConfig(review_bot=REVIEW_BOT, max_unproductive_rounds=5)

    dispatches, comments, labels = _simulate_review_polls(store, d, rcfg, pr_view, 20)

    assert dispatches == 20 and comments == 0
    assert Label.NEEDS_HUMAN_REVIEW not in labels


def test_a_multi_mode_fanout_that_never_synthesises_is_bounded(tmp_path):
    """review P0R-3's worst case. The gate SUCCEEDS and the dimension legs die,
    so `synthesize_review` returns None and the workflow returns — a RETURNED
    result, which never touches the raise path the earlier P0 fix covers. The
    whole fan-out (gate + every dimension) was re-run every poll."""
    pr_view = _open_pr()
    gw = FakeGateway(prs={pr_view.number: pr_view})
    store = Store(state_dir=str(tmp_path))
    runner = SchemaRunner(gate=_gate(), dimension=lambda _t: AgentResult(is_error=True),
                          validation=lambda _t: _val(True))
    d = _review_dispatcher(gw, store, runner, review_mode=ReviewMode.MULTI)
    rcfg = ReviewConfig(review_bot=REVIEW_BOT, max_unproductive_rounds=3)

    dispatches, _comments, labels = _simulate_review_polls(store, d, rcfg, pr_view, 20)

    assert dispatches == 3
    # The money is in the LEGS, not the dispatches: one round is a gate plus
    # every dimension specialist. 20 polls would have been 20x this.
    assert len(runner.tasks) == 3 * (1 + len(REVIEW_DIMENSIONS))
    assert Label.NEEDS_HUMAN_REVIEW in labels


def test_a_healthy_review_dispatches_once_and_never_hits_the_ceiling(tmp_path):
    """Anti-vacuity. A ceiling that fires on a working reviewer is worse than no
    ceiling: 20 polls of a healthy PR buy exactly ONE review (the second poll
    sees the saved session), submit it, and escalate nothing."""
    pr_view = _open_pr()
    gw = FakeGateway(prs={pr_view.number: pr_view})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=False, session_id="rs",
                                    structured={"decision": "APPROVE", "body": "lgtm"}))
    d = _review_dispatcher(gw, store, runner)
    rcfg = ReviewConfig(review_bot=REVIEW_BOT, max_unproductive_rounds=5)

    dispatches, comments, labels = _simulate_review_polls(store, d, rcfg, pr_view, 20)

    assert dispatches == 1 and comments == 0
    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)
    assert store.get_pr_session(pr_view.number) == "rs"
    assert store.get_infra_deaths(9) == 0
    assert labels == {Label.AI_PR_OPEN}


def test_the_reviewers_counter_survives_a_key_space_split(tmp_path):
    """P1R-1's trap, closed for the review role. The dispatcher bumps under
    `gateway.issue_for_pr(pr)` — but that lookup is a gh call, and the flakiness
    that kills review sessions is the flakiness that fails it, so the SAME PR's
    deaths land under the issue number on some polls and under `-pr` on others.
    A reader that knows only one key space reads a permanently-short count."""
    pr_view = _open_pr()
    store = Store(state_dir=str(tmp_path))
    store.bump_infra_deaths(9)                  # a poll where issue_for_pr worked
    store.bump_infra_deaths(-pr_view.number)    # a poll where it raised
    w = WorldState(prs=(pr_view,))

    assert _review_state(store, w).unproductive_rounds[pr_view.number] == 2


# ══════════════════════════════════════════════════════════════════════════════
# review P0R-4 — process death (SIGKILL / OOM / systemctl restart)
#
# Every bump site requires the process to survive its own death. A daemon killed
# mid-`await` leaves an `agent_run_start` and nothing else: no settled line, no
# ledger line, `infra_deaths == 0` — and systemd's Restart=always brings it back
# to buy the same session again. These drive the reconstruction from the
# journal through `_fix_state` / `_review_state` into the real decide functions.
# ══════════════════════════════════════════════════════════════════════════════
def _journal_lines(tmp_path, *events) -> None:
    with open(os.path.join(str(tmp_path), "journal.jsonl"), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps({"ts": _now_stamp(), **e}) + "\n")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_DISPATCH = iter(f"d{i}" for i in range(1000))


def _killed(kind, number, issue, dispatch=None):
    """The ONLY trace a SIGKILL mid-`await` leaves. `dispatch` is the id
    `set_context` mints per dispatch — distinct ids are distinct paid rounds."""
    return {"event": "agent_run_start", "role": "fix", "kind": kind,
            "number": number, "issue": issue, "model": "opus",
            "dispatch": dispatch or next(_DISPATCH)}


def _settled(kind, number, issue, dispatch=None):
    return {"event": "agent_run", "role": "fix", "kind": kind, "number": number,
            "issue": issue, "cost_usd": 0.0, "is_error": False, "status": "ok",
            "dispatch": dispatch or next(_DISPATCH)}


def test_process_deaths_are_counted_and_escalate_the_fix_role(tmp_path):
    """review P0R-4. Two SIGKILLs mid-run leave two unsettled starts and NOTHING
    else — no store counter was ever written, because the code that writes it
    died with the process. `_fix_state` reconstructs them and `decide_fix`
    escalates at the same cap it uses for survivable deaths."""
    issue = 8
    store = Store(state_dir=str(tmp_path))
    cfg = _cfg(max_infra_deaths=2)

    def world():
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ))

    _journal_lines(tmp_path, _killed("RESOLVE", issue, issue))
    assert store.get_infra_deaths(issue) == 0, "no write survived the kill"
    assert _fix_state(store, world()).infra_deaths[issue] == 1
    assert _has_run(decide_fix(world(), cfg, _fix_state(store, world()), False),
                    AgentKind.RESOLVE), "1 death < cap of 2"

    _journal_lines(tmp_path, _killed("RESOLVE", issue, issue),
                   _killed("RESOLVE", issue, issue))
    actions = decide_fix(world(), cfg, _fix_state(store, world()), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)
    assert Relabel(Target.ISSUE, issue, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) in actions


def test_healthy_runs_in_the_journal_are_not_process_deaths(tmp_path):
    """Anti-vacuity: N start+settled pairs are N healthy runs, not N deaths. A
    reader that counted starts alone would escalate every issue the harness has
    ever worked on the moment this landed."""
    issue = 8
    store = Store(state_dir=str(tmp_path))
    world = WorldState(issues=(
        IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
    ))
    _journal_lines(tmp_path, *[
        e for d in ("d1", "d2", "d3", "d4")
        for e in (_killed("RESOLVE", issue, issue, d),
                  _settled("RESOLVE", issue, issue, d))
    ])

    assert _fix_state(store, world).infra_deaths[issue] == 0
    assert _has_run(decide_fix(world, _cfg(max_infra_deaths=2), _fix_state(store, world),
                               False), AgentKind.RESOLVE)


def test_a_raise_path_death_is_counted_once_not_twice(tmp_path):
    """Anti-double-count, driven through the REAL write side. A pre-result
    transport death the process SURVIVED bumps the store counter AND emits a
    settled `transport_death` line, so the journal reconstruction must net it to
    zero. Counting it twice would halve MAX_INFRA_DEATHS for the exact failure
    mode it was tuned on — and would make the fix role escalate a healthy issue
    after two flaky sessions."""
    issue = 8
    gw = FakeGateway(issues={issue: IssueView(issue, title="t", body="b")})
    store = Store(state_dir=str(tmp_path))
    journal = Journal(os.path.join(str(tmp_path), "journal.jsonl"))
    runner = JournalingRuntime(RaisingRunner(), journal, "", "o/r", "fix")
    d = _dispatcher(gw, store, runner)

    def world():
        return WorldState(issues=(
            IssueView(issue, title="t", body="b", labels=frozenset({Label.AI_READY})),
        ))

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))

    events = read_events(journal.path)
    assert [e["event"] for e in events] == ["agent_run_start", "agent_run"]
    assert events[1]["exit_reason"] == "transport_death"
    assert store.get_infra_deaths(issue) == 1
    assert _fix_state(store, world()).infra_deaths[issue] == 1, "counted once"
    # …and one more of the same still leaves the cap-of-3 breaker untripped.
    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, issue)))
    assert _fix_state(store, world()).infra_deaths[issue] == 2
    assert _has_run(decide_fix(world(), _cfg(max_infra_deaths=3),
                               _fix_state(store, world()), False), AgentKind.RESOLVE)


def test_process_deaths_bound_the_review_role_with_no_store_write_at_all(tmp_path):
    """The review half of P0R-4: a daemon killed mid-review writes NO counter,
    so without the journal reconstruction the reviewer's brand-new ceiling would
    be blind to exactly the death that restarts it every 30 seconds."""
    pr_view = _open_pr()
    store = Store(state_dir=str(tmp_path))
    rcfg = ReviewConfig(review_bot=REVIEW_BOT, max_unproductive_rounds=2)
    w = WorldState(prs=(pr_view,))

    _journal_lines(tmp_path, _killed("REVIEW", pr_view.number, 9))
    assert _review_state(store, w).unproductive_rounds[pr_view.number] == 1
    assert RunAgent(AgentKind.REVIEW, pr_view.number) in decide_review(
        w, rcfg, _review_state(store, w))

    _journal_lines(tmp_path, _killed("REVIEW", pr_view.number, 9),
                   _killed("RE_REVIEW", pr_view.number, 9))
    actions = decide_review(w, rcfg, _review_state(store, w))
    assert not any(isinstance(a, RunAgent) for a in actions)
    assert Relabel(Target.PR, pr_view.number, Label.AI_PR_OPEN,
                   Label.NEEDS_HUMAN_REVIEW) in actions


def test_an_issueless_prs_process_death_lands_on_the_key_the_reader_uses(tmp_path):
    """The PR #112 key space, for the death nobody could write: with no closing
    ref and a Jira-keyed branch the dispatcher would have used `-pr`, so the
    journal reconstruction must too."""
    pr_view = PRView(112, head_ref_name="feat/ACME-2468-x", head_ref_oid="abc",
                     labels=frozenset({Label.AI_PR_OPEN}), state="OPEN")
    store = Store(state_dir=str(tmp_path))
    w = WorldState(prs=(pr_view,))
    _journal_lines(tmp_path, _killed("REVIEW", 112, None))

    assert _review_state(store, w).unproductive_rounds[112] == 1


# ── steward: the third role P0R-3 names, bounded on the SAME two mechanisms ────
# `triage-N.at` (and the jira phase) are written ONLY on success, so a session
# that dies leaves the issue in `triage_candidates` forever — one paid TRIAGE
# per untriaged issue per poll, which in aggregate is larger than the reviewer's
# loop. Driven through the REAL `build_steward_daemon` closure, not a
# re-implementation of its gate: a test that restates the implementation cannot
# fail when the implementation is deleted.
def _steward_decide(tmp_path, monkeypatch, **cfg_kw):
    from issue_agent.app import roles
    from issue_agent.app.config import HarnessConfig

    store = Store(state_dir=str(tmp_path))
    # Pin the journal beside this store (production does exactly this; the
    # HarnessConfig default would point at the real XDG state dir).
    monkeypatch.setattr(
        roles, "_journal",
        lambda _s: Journal(os.path.join(str(tmp_path), "journal.jsonl")),
    )
    cfg = HarnessConfig(repo="o/r", target_dir=str(tmp_path), **cfg_kw)
    gw = FakeGateway()
    gw.build_triage_world = lambda: WorldState()   # observe is never called here
    daemon = roles.build_steward_daemon(cfg, gw, store, FakeRunner(AgentResult(is_error=False)))
    return daemon.decide, store


def _triaged(number=30):
    return WorldState(issues=(IssueView(number, title="t", body="b", updated_at="T1"),))


def test_the_steward_stops_dispatching_for_an_issue_whose_sessions_keep_dying(
    tmp_path, monkeypatch
):
    decide, store = _steward_decide(tmp_path, monkeypatch)
    world = _triaged()

    for _ in range(5):
        assert decide(world) == [RunAgent(AgentKind.TRIAGE, 30)]  # anti-vacuity
        store.bump_infra_deaths(30)

    assert decide(world) == [], "5 dead sessions must stop the paid loop"


def test_the_steward_still_triages_a_healthy_issue(tmp_path, monkeypatch):
    """Anti-vacuity twin: a steward that suppressed everything would look
    identical to a bounded one on the test above."""
    decide, store = _steward_decide(tmp_path, monkeypatch)
    assert decide(_triaged()) == [RunAgent(AgentKind.TRIAGE, 30)]
    store.set_triage_at(30, "T1")
    assert decide(_triaged()) == [], "unchanged since last triage — not the ceiling"


def test_the_stewards_ceiling_also_sees_a_process_death(tmp_path, monkeypatch):
    """Without the journal read a daemon OOM-killed on every triage never
    reaches any ceiling: nothing it could write survived (review P0R-4)."""
    decide, store = _steward_decide(tmp_path, monkeypatch)
    _journal_lines(tmp_path, *[_killed("TRIAGE", 30, 30) for _ in range(5)])

    assert store.get_infra_deaths(30) == 0, "no write survived the kills"
    assert decide(_triaged()) == []


def test_the_steward_suppression_is_per_issue(tmp_path, monkeypatch):
    decide, store = _steward_decide(tmp_path, monkeypatch)
    for _ in range(5):
        store.bump_infra_deaths(30)
    world = WorldState(issues=(
        IssueView(30, title="t", body="b", updated_at="T1"),
        IssueView(31, title="t", body="b", updated_at="T1"),
    ))

    assert decide(world) == [RunAgent(AgentKind.TRIAGE, 31)]


# ── infra-death key split (review P1R-1) ───────────────────────────────────────
def test_infra_deaths_written_under_the_orphan_key_still_reach_the_ceiling(tmp_path):
    """review P1R-1. When the dispatcher's pre-handler `issue_for_pr` lookup
    RAISES, the death is recorded under `-pr` while the decide side asks for
    `breaker_key(pr)` (the linked issue). The flakiness that drops a session is
    the same flakiness that fails that lookup, so before this the counts split
    across two keys and the ceiling never tripped — the breaker failed exactly
    when it was needed. `_fix_state` now folds the orphan half back in."""
    store = Store(str(tmp_path))
    pr = _ci_pr(5)  # breaker_key == 5 (its closing issue)
    w = WorldState(prs=(pr,))
    cap = 5

    # Deaths split: 3 landed under the resolved issue, 2 under the orphan key.
    for _ in range(3):
        store.bump_infra_deaths(5)
    for _ in range(2):
        store.bump_infra_deaths(-100)

    state = _fix_state(store, w)
    assert state.infra_deaths[breaker_key(pr)] == cap, "both halves must be summed"

    cfg = _cfg(max_infra_deaths=cap)
    actions = decide_fix(w, cfg, state, fixer_busy=False)
    assert not _has_run(actions, AgentKind.FIX_CI), "at the ceiling: no paid dispatch"


def test_the_orphan_key_is_not_double_counted_for_an_issueless_pr(tmp_path):
    """Anti-vacuity twin: for an ISSUELESS PR `breaker_key(pr)` IS `-pr`, so
    summing both would count every death twice and escalate at half the cap."""
    store = Store(str(tmp_path))
    pr = PRView(
        100, head_ref_name="chore/no-issue-here", closing_issue=None,
        checks=(Check("unit", bucket="fail"),),
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT,
    )
    w = WorldState(prs=(pr,))
    assert breaker_key(pr) == -100, "fixture must exercise the coinciding-key case"

    for _ in range(3):
        store.bump_infra_deaths(-100)

    state = _fix_state(store, w)
    assert state.infra_deaths[-100] == 3, "3 deaths must read as 3, not 6"

    # Anti-vacuity on the sum itself: one more death reads as 4, not 8 — i.e.
    # the count tracks reality rather than being frozen. (This PR escalates for
    # a DIFFERENT, pre-existing reason — decide_fix escalates any issueless PR
    # needing work — so asserting on dispatch here would test that rule, not
    # this one.)
    store.bump_infra_deaths(-100)
    assert _fix_state(store, w).infra_deaths[-100] == 4


# ── hold reporting (harness #6) ───────────────────────────────────────────────
def _held_world(pr_state="OPEN"):
    """One ai-ready issue whose declared path collides with a PR's changed
    files. `pr_state="MERGED"` is the same world after the hold RELEASES."""
    return WorldState(
        issues=(
            IssueView(522, title="t", body="b", labels=frozenset({Label.AI_READY}),
                      declared_paths=frozenset({"backend/app/x.py"})),
        ),
        prs=(
            PRView(123, head_ref_name="fix/9-x", state=pr_state, author=FIX_BOT,
                   changed_files=frozenset({"backend/app/x.py"})),
        ),
    )


def _holds_in(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "hold"]


def test_a_hold_is_journaled_once_and_logged_every_poll(tmp_path):
    """AC: the hold appears ONCE in the journal however many polls it survives —
    while the LOG keeps answering "why is #522 not moving?" on every poll."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _hold_reporter(journal, "o/r", logs.append)

    for _ in range(3):
        report(_held_world())

    assert _holds_in(journal) == [
        {
            "ts": _holds_in(journal)[0]["ts"], "event": "hold", "issue": 522,
            "reason": "overlap", "blocker": "pr#123",
            "block_key": "hold:o/r:522:overlap:pr#123",
        }
    ]
    assert len(logs) == 3 and all("#522" in ln and "overlap" in ln for ln in logs)


def test_releasing_a_hold_journals_nothing_extra(tmp_path):
    """AC: releasing the hold journals nothing extra — the event says a hold
    STARTED; the absence of further holds (and the PR's own merge line) is the
    release. Re-holding on the SAME blocker stays deduped too."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _hold_reporter(journal, "o/r", lambda _m: None)

    report(_held_world())
    report(_held_world("MERGED"))   # released
    report(_held_world())           # same (issue, reason, blocker) again

    assert len(_holds_in(journal)) == 1


def test_a_second_blocker_is_its_own_hold(tmp_path):
    """Anti-vacuity for the dedup: it keys on the TUPLE, so a different blocking
    PR — a genuinely new fact — is still reported."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _hold_reporter(journal, "o/r", lambda _m: None)

    report(_held_world())
    w = _held_world()
    report(replace(w, prs=(replace(w.prs[0], number=124),)))

    assert [e["blocker"] for e in _holds_in(journal)] == ["pr#123", "pr#124"]


def test_the_hold_dedup_survives_a_daemon_restart(tmp_path):
    """The flood shape is a daemon restarting (hot reload / Restart=always), so
    an in-process set alone would re-emit every boot. A FRESH reporter over the
    same journal must stay silent."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _hold_reporter(journal, "o/r", lambda _m: None)(_held_world())
    _hold_reporter(journal, "o/r", lambda _m: None)(_held_world())

    assert len(_holds_in(journal)) == 1


# ── CI pending-deadline reporting (#30) ──────────────────────────────────────
_STALL_CFG = FixConfig(fix_bot=FIX_BOT, ci_pending_max_seconds=3600)
_EMPTY_FIX_STATE = FixState()


def _stalled_world(head="sha-aaa", elapsed=3601, started="2026-07-28T12:00:00Z"):
    """One fixer PR with a check wedged IN_PROGRESS past the deadline."""
    return WorldState(
        prs=(
            PRView(144, head_ref_name="fix/9-x", head_ref_oid=head, state="OPEN",
                   author=FIX_BOT, labels=frozenset({Label.AI_PR_OPEN}),
                   checks=(Check("Test (windows-latest)", state="IN_PROGRESS",
                                 started_at=started),)),
        ),
        now=(
            datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=elapsed)
        ).isoformat(),
    )


def _stalls_in(journal):
    return [
        e for e in read_events(journal.path) if e.get("event") == "ci_pending_deadline"
    ]


def test_a_wedged_check_is_journaled_once_and_logged_every_poll(tmp_path):
    """AC: the deadlock stops being invisible. The journal carries the PR, the
    head, the elapsed seconds and the pinning check names — enough to compute an
    escalation rate and to go cancel the job — once per stuck run, while the log
    keeps answering "why is PR #144 not moving?" on every poll."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, logs.append)

    for _ in range(3):
        report(_stalled_world())

    assert _stalls_in(journal) == [
        {
            "ts": _stalls_in(journal)[0]["ts"], "event": "ci_pending_deadline",
            "pr": 144, "head": "sha-aaa", "seconds": 3601,
            "checks": ["Test (windows-latest)"],
            "block_key": "ci-pending:o/r:144:sha-aaa",
        }
    ]
    assert len(logs) == 3 and all("#144" in ln for ln in logs)


def test_a_healthy_pending_pr_is_neither_logged_nor_journaled(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, logs.append)(
        _stalled_world(elapsed=23 * 60)
    )
    assert _stalls_in(journal) == [] and logs == []


def test_a_new_head_wedging_again_is_its_own_event(tmp_path):
    """Anti-vacuity for the dedup: it keys on the head SHA, so a push that
    produces a fresh check run which ALSO wedges is a genuinely new fact."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    report = _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)

    report(_stalled_world(head="sha-aaa"))
    report(_stalled_world(head="sha-bbb"))

    assert [e["head"] for e in _stalls_in(journal)] == ["sha-aaa", "sha-bbb"]


def test_the_stall_dedup_survives_a_daemon_restart(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)(_stalled_world())
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)(_stalled_world())

    assert len(_stalls_in(journal)) == 1


def test_the_journal_agrees_with_the_escalation_it_explains(tmp_path):
    """The two halves read the SAME derivation, so a journaled stall is always a
    PR decide_fix escalates in that very poll — never a line about a PR nothing
    acted on, nor an escalation with no record."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    w = _stalled_world()
    _ci_stall_reporter(journal, "o/r", _STALL_CFG, lambda _m: None)(w)

    escalated = {
        a.number for a in decide_fix(w, _STALL_CFG, _EMPTY_FIX_STATE, fixer_busy=False)
        if isinstance(a, Relabel) and a.add == Label.NEEDS_HUMAN_REVIEW
    }
    assert escalated == {e["pr"] for e in _stalls_in(journal)} == {144}


def test_hold_reporting_without_a_journal_still_logs(tmp_path):
    """Observability must never add a failure mode: no journal → log only."""
    logs = []
    _hold_reporter(None, "o/r", logs.append)(_held_world())
    assert len(logs) == 1


def test_a_journaled_hold_explains_the_issue_decide_fix_actually_withheld(tmp_path):
    """The two halves must agree: the issue named in the journal is exactly the
    one `decide_fix` refuses to dispatch this poll."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    w = _held_world()
    _hold_reporter(journal, "o/r", lambda _m: None)(w)

    actions = decide_fix(w, _cfg(), _fix_state(Store(str(tmp_path)), w), fixer_busy=False)
    assert not _has_run(actions, AgentKind.RESOLVE)
    assert [e["issue"] for e in _holds_in(journal)] == [522]


# ── reopen recurrences (harness #10) ─────────────────────────────────────────
class _ReopenGateway:
    """The two recurrence reads, scripted. Records every state-reason lookup so a
    test can assert the steady state costs nothing."""

    def __init__(self, reasons=None, merged=(), merges_raise=False):
        self.reasons = reasons or {}
        self.merged = merged
        self.merges_raise = merges_raise
        self.asked = []

    def issue_state_reason(self, number):
        self.asked.append(number)
        return self.reasons.get(number, "")

    def recently_merged_issues(self, author, limit=30):
        if self.merges_raise:
            raise RuntimeError("gh boom")
        return self.merged


class _Merged:
    def __init__(self, issue, pr):
        self.issue, self.pr = issue, pr


def _resolved_world(*, state="open", label=Label.AI_DONE):
    return WorldState(issues=(
        IssueView(522, title="t", body="b", labels=frozenset({label}), state=state),
    ))


def _recurrences_in(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "recurrence"]


def test_a_reopened_resolved_issue_is_journaled_once_and_logged_every_poll(tmp_path):
    """AC: a reopened harness-resolved issue journals EXACTLY one recurrence
    event, however many polls the reopen survives — while the log keeps saying so
    every pass."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"}, merged=(_Merged(522, 300),))
    logs = []
    report = _recurrence_reporter(journal, gw, "o/r", "fix-bot", logs.append)

    report(_resolved_world())
    report(_resolved_world())

    assert _recurrences_in(journal) == [
        {
            "ts": _recurrences_in(journal)[0]["ts"], "event": "recurrence",
            "kind": "reopen", "issue": 522, "of_issue": 522, "pr": 300,
            "block_key": "recurrence:o/r:522:reopen",
        }
    ]
    assert len(logs) == 2
    assert all("#522" in ln and "reopened" in ln for ln in logs)


def test_an_ai_done_issue_that_was_never_reopened_is_not_a_recurrence(tmp_path):
    """The harness merges to a non-default base and closes issues itself, so
    'ai-done and open' is ambiguous — only stateReason=reopened confirms it.
    Without this every ai-done issue on a close_issue_on_merge=off deployment
    would journal a recurrence."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    _recurrence_reporter(
        journal, _ReopenGateway(reasons={522: ""}), "o/r", "fix-bot", logs.append
    )(_resolved_world())

    assert _recurrences_in(journal) == [] and logs == []


def test_only_resolved_open_issues_cost_a_state_reason_call(tmp_path):
    """Zero extra gh calls in the steady state: a closed ai-done issue and an
    open issue the harness never resolved are both never asked about."""
    gw = _ReopenGateway(reasons={522: "reopened"})
    report = _recurrence_reporter(None, gw, "o/r", "fix-bot", lambda _m: None)

    report(_resolved_world(state="closed"))
    report(_resolved_world(label=Label.AI_READY))

    assert gw.asked == []


def test_the_reopen_dedup_survives_a_daemon_restart(tmp_path):
    """The recurrence RATE is computed by counting these events, so a duplicate
    corrupts the metric — an in-process set alone would re-emit on every boot."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"})
    _recurrence_reporter(journal, gw, "o/r", "fix-bot", lambda _m: None)(_resolved_world())
    _recurrence_reporter(journal, gw, "o/r", "fix-bot", lambda _m: None)(_resolved_world())

    assert len(_recurrences_in(journal)) == 1


def test_reopen_reporting_without_a_journal_still_logs(tmp_path):
    """Observability must never add a failure mode: no journal → log only."""
    logs = []
    _recurrence_reporter(
        None, _ReopenGateway(reasons={522: "reopened"}), "o/r", "fix-bot", logs.append
    )(_resolved_world())
    assert len(logs) == 1


def test_an_unreadable_merged_list_still_records_the_reopen(tmp_path):
    """The PR number is enrichment. Losing it must not lose the recurrence."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = _ReopenGateway(reasons={522: "reopened"}, merges_raise=True)
    _recurrence_reporter(journal, gw, "o/r", "fix-bot", lambda _m: None)(_resolved_world())

    assert [e["pr"] for e in _recurrences_in(journal)] == [0]


def test_a_gateway_without_the_recurrence_reads_is_inert(tmp_path):
    """A fake or legacy adapter must yield an inert reporter, not an
    AttributeError out of the steward's decide closure."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _recurrence_reporter(journal, object(), "o/r", "fix-bot", lambda _m: None)(
        _resolved_world()
    )
    assert _recurrences_in(journal) == []


# ── FIXER_RUNTIME=glm wiring (build_fix_daemon) ──────────────────────────────
# The wrapping itself (env injection, task-env precedence) is proven in
# tests/adapters/test_claude_profile.py; what these pin is the BUILD-TIME
# contract of the glm branch — that a profile is resolved once at startup and
# that its outcome is announced in one operator-facing line either way. The
# composed runner is not reachable from the returned Daemon without reading a
# private attribute, so it is deliberately not asserted here.


class _BuildableGateway(FakeGateway):
    """FakeGateway plus the one member `build_fix_daemon` binds at build time."""

    def build_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")


def _glm_profile(tmp_path, settings: str) -> str:
    d = tmp_path / ".cc-glm"
    d.mkdir()
    (d / "settings.json").write_text(settings)
    return str(d / "settings.json")


def _glm_daemon(tmp_path, settings_file: str):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_fix_daemon

    cfg = from_env({
        "REPO": "o/r", "FIX_BOT": FIX_BOT, "FIXER_RUNTIME": "glm",
        "GLM_SETTINGS_FILE": settings_file, "STATE_DIR": str(tmp_path / "state"),
    })
    return build_fix_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)),
    )


def test_a_glm_profile_is_resolved_once_at_startup_and_announced(tmp_path, capsys):
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}})

    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, settings))

    line = capsys.readouterr().err
    assert "glm: runtime ready" in line
    assert "api.z.ai" in line              # endpoint host: useful, not secret
    assert "NOT READY" not in line
    assert daemon.name == "fix"


def test_a_missing_glm_profile_says_so_loudly_at_startup_instead_of_dying(tmp_path, capsys):
    """A misconfig must surface as ONE startup line, not as an opaque failure on
    the first dispatch — and the daemon must still be built (visibly wrong beats
    a crash-looping fixer)."""
    daemon = _glm_daemon(tmp_path, str(tmp_path / "absent" / "settings.json"))

    err = capsys.readouterr().err
    assert "glm: NOT READY at startup" in err
    assert "settings.json" in err
    assert daemon.name == "fix"


def test_a_malformed_glm_profile_is_not_ready_rather_than_a_traceback(tmp_path, capsys):
    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, '{"env": {,,}'))

    assert "glm: NOT READY at startup" in capsys.readouterr().err
    assert daemon.name == "fix"


# ── #27/#32: the daily caps gate the TICK, not the dispatch ──────────────────
# Driven through the real `build_*_daemon` wiring and the real `Daemon.tick`,
# because the defect was in HOW the loop reached the check, not in the check —
# the pre-fix code reported the block faithfully on every one of the ticks it
# should never have run.


class _ObservingGateway(FakeGateway):
    """FakeGateway that counts world builds — the thing #27 was paying for."""

    def __init__(self):
        super().__init__()
        self.observations = 0

    def _seen(self):
        self.observations += 1
        return WorldState()

    build_world = _seen
    build_review_world = _seen
    build_triage_world = _seen
    build_steward_world = _seen


def _today_spend(cost, repo="o/r"):
    return {"ts": datetime.now(timezone.utc).isoformat(), "event": "spend",
            "repo": repo, "issue": 5, "kind": "RESOLVE", "cost_usd": cost}


def _capped_daemons(tmp_path, monkeypatch, ledger_entries, **cfg_kw):
    from issue_agent.app import roles
    from issue_agent.app.config import HarnessConfig

    ledger = tmp_path / "spend.jsonl"
    ledger.write_text("".join(json.dumps(e) + "\n" for e in ledger_entries))
    monkeypatch.setattr(roles, "default_ledger_path", lambda: str(ledger))
    monkeypatch.setattr(
        roles, "_journal",
        lambda _s: Journal(os.path.join(str(tmp_path), "journal.jsonl")),
    )
    cfg = HarnessConfig(
        repo="o/r", fix_bot=FIX_BOT, target_dir=str(tmp_path),
        state_dir=str(tmp_path / "state"), **cfg_kw
    )
    store = Store(str(tmp_path / "state"))
    runner = FakeRunner(AgentResult(is_error=False))
    gw = _ObservingGateway()
    return [
        roles.build_fix_daemon(cfg, gw, store, FakeWorktrees(), runner),
        roles.build_review_daemon(cfg, gw, store, runner),
        roles.build_steward_daemon(cfg, gw, store, runner),
    ], gw


def test_every_role_skips_the_world_build_while_the_repo_cap_is_tripped(
    tmp_path, monkeypatch
):
    """#27's cost was ~2,520 GraphQL points and 360 label writes per hour, per
    capped daemon, to accomplish nothing — and the orphaned ai-working label it
    left behind defeated the world probe built to avoid exactly that. Asserted on
    the observation count, which is what the quota is spent on."""
    daemons, gw = _capped_daemons(
        tmp_path, monkeypatch, [_today_spend(60.0)], daily_spend_cap_usd=50.0
    )
    for daemon in daemons:
        for _ in range(3):
            assert daemon.tick() == []
    assert gw.observations == 0
    assert gw.calls == []


def test_a_neighbours_spend_does_not_gate_this_repos_daemons(tmp_path, monkeypatch):
    """Anti-vacuity twin (#32): the gate must be scoped to this deployment, or it
    is just the starvation bug moved earlier in the tick."""
    daemons, gw = _capped_daemons(
        tmp_path, monkeypatch,
        [_today_spend(709.55, repo="other/repo")], daily_spend_cap_usd=50.0,
    )
    for daemon in daemons:
        daemon.tick()
    assert gw.observations == 3


def test_the_global_cap_gates_every_role_regardless_of_repo(tmp_path, monkeypatch):
    daemons, gw = _capped_daemons(
        tmp_path, monkeypatch,
        [_today_spend(709.55, repo="other/repo")],
        daily_spend_cap_usd=50.0, global_daily_spend_cap_usd=700.0,
    )
    for daemon in daemons:
        assert daemon.tick() == []
    assert gw.observations == 0


def test_no_gate_is_wired_when_both_daily_caps_are_disabled(tmp_path, monkeypatch):
    """0 means disabled, and an unarmed daemon must not pay a ledger read/poll."""
    daemons, gw = _capped_daemons(
        tmp_path, monkeypatch, [_today_spend(9_999.0)],
        daily_spend_cap_usd=0.0, global_daily_spend_cap_usd=0.0,
    )
    assert all(d.tick_gate is None for d in daemons)
    for daemon in daemons:
        daemon.tick()
    assert gw.observations == 3
