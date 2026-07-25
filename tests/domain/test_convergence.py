"""Loop-convergence property for the pure fix decision (T3).

`decide_fix` is edge-triggered: it dispatches a PAID agent session when the world
looks a certain way, and the ONLY thing that stops it re-dispatching on the next
poll is state the daemon itself writes back — a label move, a resumed-at mark, a
retry counter. Every historical money-burn in FAILURE-MODES (#13, #14, #39, #64,
V1, V2) has the same shape: one of those write-backs was missing, mis-keyed, or
compared against a different fact than the read side, so an UNCHANGED world
re-fired a full session every tick, forever.

This file tests that shape rather than one instance of it. Each scenario runs the
daemon's tick loop against a FROZEN GitHub world — the pathological case where
the agent accomplishes nothing (no push, no PR, no new review, no comment) —
while faithfully applying the two things the daemon itself changes:

  a) its own ``Relabel`` writes (labels are the coordination channel), and
  b) the orchestration state ``app/workflows/fix.py`` persists around each
     dispatch (resumed-at mark, review / CI / rebase retry counters).

Asserted property: the number of PAID ``RunAgent`` dispatches over an unbounded
poll loop is capped by the relevant circuit breaker, and `decide_fix` then
settles on a STABLE, RunAgent-free fixed point. Repeated $0 actions (an
idempotent auto-merge arm) are acceptable; repeated sessions are not.

The harness is deliberately tiny and explicit: `_apply_relabels` +
`_after_run` are the whole simulator, so a new scenario is one table row.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from builders import Check, Comment, Review, issue, pr, world

from issue_agent.domain import fix as fix_domain
from issue_agent.domain import outcome
from issue_agent.domain.ci import latest_other_review
from issue_agent.domain.actions import (
    Action,
    AgentKind,
    Relabel,
    RunAgent,
    Target,
)
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.world import WorldState

CFG = FixConfig(fix_bot="fix-bot")

# Well past every cap in CFG (3) — a bounded leg must be quiet long before this.
TICKS = 12
# How many trailing ticks must be identical to call the loop settled.
STABLE_TAIL = 3

# The pathological agent run: it errors, and — critically — it changed NOTHING on
# GitHub (no push, no PR, no comment). is_error=True → Outcome.ERROR; session_id
# is empty, matching a run that died before the SDK returned one.
ERRORED_RUN = outcome.AgentResult(is_error=True)


# ── simulator ────────────────────────────────────────────────────────────────
def _moved(labels: frozenset[Label], action: Relabel) -> frozenset[Label]:
    out = set(labels)
    if action.remove is not None:
        out.discard(action.remove)
    if action.add is not None:
        out.add(action.add)
    return frozenset(out)


def _apply_relabels(state_of_world: WorldState, actions) -> WorldState:
    """Apply the daemon's OWN Relabel writes back into the simulated world.

    These are not incidental: the label move IS how a decision records that it
    already fired (ai-ready→ai-working, ai-pr-open→needs-human-review). A
    simulation that dropped them would invent loops that don't exist and — far
    worse — hide the ones that do. Every other action (PostComment, CloseIssue,
    EnableAutoMerge, RemoveWorktree) is $0 and does not feed back into
    `decide_fix`, so the world ignores them.
    """
    issues = list(state_of_world.issues)
    prs = list(state_of_world.prs)
    for action in actions:
        if not isinstance(action, Relabel):
            continue
        if action.target is Target.ISSUE:
            issues = [
                replace(i, labels=_moved(i.labels, action)) if i.number == action.number else i
                for i in issues
            ]
        else:
            prs = [
                replace(p, labels=_moved(p.labels, action)) if p.number == action.number else p
                for p in prs
            ]
    return WorldState(issues=tuple(issues), prs=tuple(prs))


def _find(views, number: int):
    for view in views:
        if view.number == number:
            return view
    raise AssertionError(f"scenario world has no #{number}")


def _latest_changes_requested_at(pr_view, fix_bot: str) -> str:
    """Mirror of `FixWorkflow._current_review_at` (app/workflows/fix.py) — the
    key the review retry counter is stored under: the latest DISTINCT-actor
    review iff it requests changes, i.e. the SAME selector `decide_fix` gates
    dispatch on. Re-implemented here rather than imported so the domain suite
    stays free of the app layer; if the workflow's keying changes, this must
    too."""
    review = latest_other_review(pr_view.reviews, fix_bot)
    if review is not None and review.state == "CHANGES_REQUESTED":
        return review.submitted_at
    return ""


def _with_session(state: FixState, issue_number: int, session_id: str) -> FixState:
    """`store.save_session` (adapters/store.py:72-74) is a no-op for an empty id,
    so an errored run leaves the session set untouched."""
    if not session_id:
        return state
    return replace(state, sessions=state.sessions | {issue_number})


def _after_raised_run(
    run: RunAgent, state_of_world: WorldState, state: FixState
) -> tuple[list[Action], FixState]:
    """The OTHER terminal shape (review C3): the session dies BEFORE
    `runner.run` returns, so `AgentRunner` raises and the workflow tail never
    executes — no session saved, no retry counter, no follow-up actions, and the
    label stays wherever the dispatch relabel left it. The ONLY write is
    `AgentDispatcher.dispatch`'s bump-then-re-raise, mirrored here.

    Key: the issue behind the work, or the negated PR number when a PR has no
    resolvable issue — `breaker_key`'s convention, which is what the dispatcher
    uses (`issue if issue is not None else -action.number`).
    """
    key = run.number
    if run.kind in (AgentKind.ADDRESS_REVIEW, AgentKind.FIX_CI, AgentKind.REBASE):
        key = fix_domain.breaker_key(_find(state_of_world.prs, run.number))
    deaths = dict(state.infra_deaths)
    deaths[key] = deaths.get(key, 0) + 1
    return [], replace(state, infra_deaths=deaths)


def _after_run(
    run: RunAgent, state_of_world: WorldState, state: FixState, cfg: FixConfig
) -> tuple[list[Action], FixState]:
    """The state transition the fix workflow persists around one dispatch, plus
    the follow-up actions it executes — for a run that ERRORED and changed
    nothing. Returns (follow-up actions, new FixState).

    Each branch mirrors `app/workflows/fix.py` exactly; the anti-loop guarantee
    lives in the pairing of these writes with `decide_fix`'s reads, so any drift
    between the two is the bug this file exists to catch. It is a HAND-mirror —
    nothing forces it to stay in step with the workflow, which is why every
    scenario here has a write-side twin in tests/adapters/test_fix_workflow.py.
    """
    if run.kind is AgentKind.RESOLVE:
        # fix.py resolve(): save_session, bump the resolve counter when the
        # follow-up is a REQUEUE (`_bump_resolve_retry` — the T3 gap fix), then
        # resolve_actions with the ground-truth PR (None — nothing opened).
        # ERROR → requeue ai-ready.
        new_state = _with_session(state, run.number, ERRORED_RUN.session_id)
        if outcome.resolve_followup(ERRORED_RUN, False) is outcome.ResolveFollowup.RETRY:
            resolves = dict(new_state.resolve_retries)
            resolves[run.number] = resolves.get(run.number, 0) + 1
            new_state = replace(new_state, resolve_retries=resolves)
        return (
            outcome.resolve_actions(run.number, ERRORED_RUN, None),
            new_state,
        )

    if run.kind is AgentKind.RESUME:
        # fix.py:138-165 resume(): the resumed-at mark is written BEFORE the run
        # (fix.py:151) with the SAME comment selector the decide side reads
        # (`last_human_reply`) — that pairing is the V1/#13 fix. Then
        # resume_actions on the result: ERROR keeps the issue ai-blocked.
        iss = _find(state_of_world.issues, run.number)
        reply = fix_domain.last_human_reply(iss, cfg.fix_bot)
        resumed = dict(state.resumed_at)
        if reply is not None:
            resumed[run.number] = reply.created_at
        return (
            outcome.resume_actions(run.number, ERRORED_RUN, None),
            _with_session(replace(state, resumed_at=resumed), run.number, ERRORED_RUN.session_id),
        )

    if run.kind is AgentKind.ADDRESS_REVIEW:
        # fix.py:167-191 address_review(): the counter is re-keyed to the review
        # being addressed — a count stored under an OLDER review restarts at 0,
        # and there is deliberately NO clear-on-success (V2/#64).
        pr_view = _find(state_of_world.prs, run.number)
        key = fix_domain.breaker_key(pr_view)
        result = outcome.address_review_actions(run.number, ERRORED_RUN)
        retries = dict(state.review_retries)
        if result.bump_retry:
            review_at = _latest_changes_requested_at(pr_view, cfg.fix_bot)
            at, n = retries.get(key, ("", 0))
            retries[key] = (review_at, (n if at == review_at else 0) + 1)
        issue_n = fix_domain._issue_for_pr(pr_view)
        new_state = replace(state, review_retries=retries)
        if issue_n is not None:
            new_state = _with_session(new_state, issue_n, ERRORED_RUN.session_id)
        return result.actions, new_state

    if run.kind is AgentKind.FIX_CI:
        # fix.py:193-210 fix_ci(): counter bumped unconditionally after the run.
        pr_view = _find(state_of_world.prs, run.number)
        key = fix_domain.breaker_key(pr_view)
        retries = dict(state.ci_retries)
        retries[key] = retries.get(key, 0) + 1
        return (
            outcome.fix_ci_actions(run.number, ERRORED_RUN),
            replace(state, ci_retries=retries),
        )

    if run.kind is AgentKind.REBASE:
        # fix.py:212-224 rebase(): same shape as fix_ci, own counter, and it
        # reuses fix_ci_actions for the tail.
        pr_view = _find(state_of_world.prs, run.number)
        key = fix_domain.breaker_key(pr_view)
        retries = dict(state.rebase_retries)
        retries[key] = retries.get(key, 0) + 1
        return (
            outcome.fix_ci_actions(run.number, ERRORED_RUN),
            replace(state, rebase_retries=retries),
        )

    raise AssertionError(f"unmodelled fixer dispatch: {run.kind}")


@dataclass(frozen=True)
class Tick:
    decided: tuple[Action, ...]        # what decide_fix emitted this poll
    dispatches: tuple[RunAgent, ...]   # the PAID subset


@dataclass(frozen=True)
class Scenario:
    """One frozen world + the convergence budget it must respect."""

    name: str
    world: WorldState
    state: FixState = field(default_factory=FixState)
    cfg: FixConfig = CFG
    expect_dispatches: int = 0            # exact PAID sessions over the whole run
    expect_kinds: frozenset = frozenset()  # which legs fired
    cap: int = 0                          # the retry cap that must bound them
    # The agent dies pre-result (raises) instead of returning an errored result
    # — the C3 shape, whose tail is `_after_raised_run` rather than `_after_run`.
    runner_raises: bool = False


def simulate(scenario: Scenario, ticks: int = TICKS) -> list[Tick]:
    """Run the daemon's poll loop. The GitHub world only ever changes through the
    daemon's own writes — the agent is inert, and no human intervenes."""
    state_of_world, state = scenario.world, scenario.state
    trace: list[Tick] = []
    for _ in range(ticks):
        decided = decide_fix(state_of_world, scenario.cfg, state, fixer_busy=False)
        # The dispatch relabel lands before the agent runs (executor order), and
        # the run is synchronous (roles.py runs it with asyncio.run), so the next
        # poll observes both the relabel and the run's tail.
        state_of_world = _apply_relabels(state_of_world, decided)
        dispatches = tuple(a for a in decided if isinstance(a, RunAgent))
        for run in dispatches:
            followup, state = (
                _after_raised_run(run, state_of_world, state)
                if scenario.runner_raises
                else _after_run(run, state_of_world, state, scenario.cfg)
            )
            state_of_world = _apply_relabels(state_of_world, followup)
        trace.append(Tick(tuple(decided), dispatches))
    return trace


# ── scenarios ────────────────────────────────────────────────────────────────
def _resume_dedup() -> Scenario:
    """V1/#13: one unanswered human reply must buy exactly ONE resume session,
    even though the resume leg keeps failing and the issue keeps returning to
    ai-blocked with that same reply still last in the thread."""
    return Scenario(
        name="resume-dedup",
        world=world(issues=[
            issue(7, "ai-blocked", comments=(
                Comment("alice", "here is the missing context", "2026-01-01T00:00:00Z"),
            )),
        ]),
        state=FixState(sessions=frozenset({7})),
        expect_dispatches=1,
        expect_kinds=frozenset({AgentKind.RESUME}),
        cap=0,  # not a retry loop: the dedup mark alone must hold it to one
    )


def _address_review_breaker() -> Scenario:
    """A CHANGES_REQUESTED review on the CURRENT head with a fixer that never
    lands a fix: the attempt counter (keyed to the review) must trip the breaker
    and escalate instead of paying forever."""
    p = pr(
        100, "ai-pr-open",
        head_ref_oid="sha-head",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-01-02T00:00:00Z",
                        "please fix", "sha-head"),),
    )
    return Scenario(
        name="address-review-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_review_retries,
        expect_kinds=frozenset({AgentKind.ADDRESS_REVIEW}),
        cap=CFG.max_review_retries,
    )


def _address_review_superseded() -> Scenario:
    """V2/#64: the review judged an OLDER commit than the current head, so the
    ball is in the REVIEWER's court. Zero sessions, ever — not "a few then a
    breaker". A frozen world here means the review daemon is down, which is
    exactly when the old code burned a session per poll."""
    p = pr(
        101, "ai-pr-open",
        head_ref_oid="sha-new",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-01-02T00:00:00Z",
                        "please fix", "sha-old"),),
    )
    return Scenario(
        name="address-review-superseded",
        world=world(prs=[p]),
        expect_dispatches=0,
        expect_kinds=frozenset(),
        cap=0,
    )


def _fix_ci_breaker() -> Scenario:
    p = pr(102, "ai-pr-open", checks=(Check("unit", "COMPLETED", "fail"),))
    return Scenario(
        name="fix-ci-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_ci_retries,
        expect_kinds=frozenset({AgentKind.FIX_CI}),
        cap=CFG.max_ci_retries,
    )


def _rebase_breaker() -> Scenario:
    p = pr(103, "ai-pr-open", mergeable="CONFLICTING")
    return Scenario(
        name="rebase-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_rebase_retries,
        expect_kinds=frozenset({AgentKind.REBASE}),
        cap=CFG.max_rebase_retries,
    )


def _zero_spend_steady_state() -> Scenario:
    """The happy end of the pipeline: a merged PR to clean up and an approved,
    green PR to arm for auto-merge. Neither is a session, and cleanup must stop
    once its labels are applied rather than re-emitting every poll."""
    merged = pr(104, "ai-pr-open", state="MERGED", closing_issue=9)
    approved = pr(
        105, "ai-pr-open",
        reviews=(Review("APPROVED", "review-bot", "2026-01-03T00:00:00Z"),),
    )
    return Scenario(
        name="zero-spend-steady-state",
        world=world(issues=[issue(9, "ai-pr-open")], prs=[merged, approved]),
        expect_dispatches=0,
        expect_kinds=frozenset(),
        cap=0,
    )


def _resolve_retry_loop() -> Scenario:
    """The T3 headline finding, now FIXED: an ai-ready issue whose resolve run
    keeps erroring is requeued to ai-ready by `outcome.resolve_actions`
    (ResolveFollowup.RETRY), and the workflow counts each requeue
    (`_bump_resolve_retry`). At `max_resolve_retries` the decide side escalates
    the issue to needs-human-review instead of paying for another full session
    — the same breaker shape as review/CI/rebase, on the most expensive leg."""
    return Scenario(
        name="resolve-retry-loop",
        world=world(issues=[issue(5, "ai-ready")]),
        expect_dispatches=CFG.max_resolve_retries,
        expect_kinds=frozenset({AgentKind.RESOLVE}),
        cap=CFG.max_resolve_retries,
    )


def _pre_result_death_loop() -> Scenario:
    """Review C3, as a convergence row. The session dies before returning a
    result, so `AgentRunner` raises: the workflow tail never runs, the issue is
    left ai-working, the reaper requeues it to ai-ready, and the poll after that
    pays for another full session — the observed "one paid session every two
    polls, forever", with $0.00 on the ledger and every retry breaker reading 0.

    The infra-death counter (bumped by `AgentDispatcher.dispatch` on the raise
    path) is the ONLY thing that can bound this, because no attempt is ever
    recorded. Cap 2 here rather than the production default 5 purely to fit the
    tick budget; the shape is what matters.
    """
    return Scenario(
        name="pre-result-death-loop",
        world=world(issues=[issue(5, "ai-ready")]),
        cfg=replace(CFG, max_infra_deaths=2),
        expect_dispatches=2,
        expect_kinds=frozenset({AgentKind.RESOLVE}),
        cap=2,
        runner_raises=True,
    )


SCENARIOS = [
    pytest.param(_resume_dedup(), id="resume-dedup"),
    pytest.param(_address_review_breaker(), id="address-review-breaker"),
    pytest.param(_address_review_superseded(), id="address-review-superseded"),
    pytest.param(_fix_ci_breaker(), id="fix-ci-breaker"),
    pytest.param(_rebase_breaker(), id="rebase-breaker"),
    pytest.param(_zero_spend_steady_state(), id="zero-spend-steady-state"),
    pytest.param(_resolve_retry_loop(), id="resolve-retry-loop"),
    pytest.param(_pre_result_death_loop(), id="pre-result-death-loop"),
]


# ── the property ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fix_loop_converges_on_a_frozen_world(scenario: Scenario):
    trace = simulate(scenario)
    dispatches = [run for tick in trace for run in tick.dispatches]

    # 1) PAID work is bounded by the circuit breaker, not by the poll count.
    assert len(dispatches) <= scenario.cap + 1, (
        f"{scenario.name}: {len(dispatches)} agent dispatches over {TICKS} ticks on an "
        f"UNCHANGED world (cap {scenario.cap}) — this is a money-burning loop"
    )
    assert len(dispatches) == scenario.expect_dispatches
    # 2) …and the right leg fired (a leg that goes quiet for the wrong reason —
    #    e.g. the PR silently dropping out of scope — is not convergence).
    assert {run.kind for run in dispatches} == scenario.expect_kinds

    tail = trace[-STABLE_TAIL:]
    # 3) The fixed point is RunAgent-free: no session is ever paid for again.
    assert all(not tick.dispatches for tick in tail), (
        f"{scenario.name}: still dispatching at tick {TICKS}"
    )
    # 4) …and stable: the same ($0) actions repeat forever rather than the
    #    decision oscillating between states.
    assert all(tick.decided == tail[0].decided for tick in tail), (
        f"{scenario.name}: decide_fix does not settle — tail={[t.decided for t in tail]}"
    )


def test_escalation_is_the_only_thing_that_stops_a_stuck_pr():
    """The breaker must escalate to a human exactly ONCE and then go silent —
    an escalation that re-fires every poll would spam the PR (and would mean the
    label write-back that removes it from scope never happened)."""
    trace = simulate(_address_review_breaker())
    escalated = [
        i for i, tick in enumerate(trace)
        if Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in tick.decided
    ]
    assert escalated == [CFG.max_review_retries]  # right after the last attempt
    assert all(tick.decided == () for tick in trace[CFG.max_review_retries + 1:])


def test_cleanup_stops_once_its_labels_are_applied():
    """Merged-PR cleanup is $0 but not free of consequences (it closes the issue
    and drops worktrees) — it must run once, not every poll."""
    trace = simulate(_zero_spend_steady_state())
    cleanup_ticks = [
        i for i, tick in enumerate(trace)
        if Relabel(Target.PR, 104, Label.AI_PR_OPEN, None) in tick.decided
    ]
    assert cleanup_ticks == [0]


def test_pre_result_death_is_unbounded_without_the_infra_ceiling():
    """Anti-vacuity twin for the pre-result-death row: with the ceiling disabled
    (MAX_INFRA_DEATHS=0) the SAME frozen world pays for a full session every
    other tick, forever — dispatch, die, reaper requeues, dispatch. That is the
    C3 signature, and no other breaker in the file can see it, because a raised
    run records no attempt anywhere."""
    scenario = replace(_pre_result_death_loop(), cfg=replace(CFG, max_infra_deaths=0))
    trace = simulate(scenario)
    assert sum(len(tick.dispatches) for tick in trace) == TICKS // 2


def test_simulator_can_detect_an_unbounded_loop():
    """Guard against a vacuously-passing harness: if the daemon's label
    write-backs are NOT fed to the next poll, the resume leg re-fires every
    tick. The property above is only meaningful because this fails."""
    scenario = _resume_dedup()
    state_of_world, state = scenario.world, scenario.state
    dispatches = 0
    for _ in range(TICKS):
        decided = decide_fix(state_of_world, scenario.cfg, state, fixer_busy=False)
        dispatches += sum(1 for a in decided if isinstance(a, RunAgent))
        # deliberately drop both feedback channels (no relabels, no state writes)
    assert dispatches == TICKS
