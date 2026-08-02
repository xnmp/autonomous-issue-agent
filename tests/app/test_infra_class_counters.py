"""Infra-class failures measure the harness, not the issue (keifu #130).

2026-08-02: a broken `codex` binary answered three resolve dispatches on issue
#130 with an instant HTTP 400 — exit non-zero, no envelope, no session id, no
tokens, $0. The harness recorded them as three agent attempts. Hours later,
after the binary was fixed and the issue re-promoted, the FIRST healthy
dispatch escalated on `resolve failed 3× without opening a PR` and a human had
to unblock and re-promote by hand. The same phantom counter is difficulty
evidence for the router, which escalated #34 medium→high and spent $14.

These tests pin the rule end to end: a leg that never RAN never touches a
quality counter; a leg that ran and produced no PR still does.
"""

import asyncio

from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.domain.actions import AgentKind, PostComment, RunAgent
from issue_agent.domain.events import InfraPathEntered
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView

from tests.domain.builders import issue as build_issue, world

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees


ISSUE = 130
CFG = FixConfig(fix_bot="fix-bot", max_resolve_retries=3, max_infra_deaths=5)


class RecordingJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append(event)


def workflow(gateway, store, runner, **overrides):
    config = dict(
        base_branch="dev",
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
        fork_threshold=5,
        author_tests_split=False,
        max_contract_rounds=2,
        fix_mcp_config="",
        harness_root="",
        log=lambda _message: None,
    )
    config.update(overrides)
    worktrees = FakeWorktrees()
    return FixWorkflow(
        FixWorkflowConfig(**config), gateway, store, worktrees, runner,
        Executor(gateway, remove_worktree=worktrees.remove),
    )


def stillborn():
    """What the broken binary produced: non-zero exit, nothing else at all."""
    return AgentResult(
        is_error=True,
        summary="stream error: unexpected status 400 Bad Request",
        exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
    )


def ran_and_failed():
    """A real session that reported failure: it has a session and it spent."""
    return AgentResult(
        is_error=True,
        summary="could not make the tests pass",
        session_id="sess-abc",
        cost_usd=1.42,
        num_turns=37,
        exit_reason=ExitReason.AGENT_ERROR,
    )


def run_resolves(store, result, times, gateway=None, **overrides):
    gateway = gateway or FakeGateway(
        issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={}
    )
    for _ in range(times):
        asyncio.run(
            workflow(gateway, store, FakeRunner(result), **overrides).resolve(ISSUE)
        )
    return gateway


def decide_from(store):
    """Run the real decide function over the counters the workflow persisted."""
    state = FixState(
        resolve_retries={ISSUE: store.get_resolve_retries(ISSUE)},
        infra_deaths={ISSUE: store.get_infra_deaths(ISSUE)},
    )
    return decide_fix(
        world(issues=[build_issue(ISSUE, "ai-ready")]), CFG, state, fixer_busy=False
    )


def resolve_dispatches(actions):
    return [
        a for a in actions
        if isinstance(a, RunAgent) and a.kind is AgentKind.RESOLVE
    ]


def comments(actions):
    return " ".join(a.body for a in actions if isinstance(a, PostComment))


# ── the incident ─────────────────────────────────────────────────────────────
def test_three_stillborn_legs_leave_the_resolve_breaker_untouched():
    store = FakeStore()

    run_resolves(store, stillborn(), 3)

    assert store.get_resolve_retries(ISSUE) == 0


def test_next_healthy_dispatch_after_three_stillborn_legs_is_not_escalated():
    """The exact #130 shape: the harness is fixed, the issue is re-promoted,
    and the very first attempt must be a dispatch — not a human escalation."""
    store = FakeStore()
    run_resolves(store, stillborn(), 3)

    actions = decide_from(store)

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE)]
    assert "resolve failed" not in comments(actions)


def test_stillborn_legs_take_the_infra_ceiling_instead():
    """The exemption is not an amnesty: a PERSISTENTLY broken runtime still
    reaches a human, under its own bounded counter."""
    store = FakeStore()

    run_resolves(store, stillborn(), 3)

    assert store.get_infra_deaths(ISSUE) == 3


def test_a_runtime_broken_past_the_infra_ceiling_escalates_as_infra():
    store = FakeStore()
    run_resolves(store, stillborn(), 5)

    actions = decide_from(store)

    assert resolve_dispatches(actions) == []
    body = comments(actions)
    assert "INFRA" in body
    assert "resolve failed" not in body
    assert "measures the harness, not the issue" in body


def test_stillborn_legs_are_journalled_with_the_ceiling_they_advanced():
    """ADR 0012: a counter that deliberately does not move needs an event, or
    the operator sees a leg fail and a breaker stand still with nothing
    connecting the two."""
    store = FakeStore()
    journal = RecordingJournal()

    run_resolves(store, stillborn(), 2, journal=journal)

    payloads = [e.payload for e in journal.events
                if isinstance(e.payload, InfraPathEntered)]
    assert payloads == [
        InfraPathEntered("resolve", "runtime_unavailable", 1),
        InfraPathEntered("resolve", "runtime_unavailable", 2),
    ]
    assert all(e.correlation.issue == ISSUE for e in journal.events)


def test_a_counter_we_cannot_persist_does_not_replace_the_death_it_reports():
    """A store failure here used to escape the workflow, where the dispatcher
    re-catches it and reports a SECOND infra death for the same leg. The leg's
    own follow-up actions must still be applied."""

    class UnwritableStore(FakeStore):
        def bump_infra_deaths(self, key):
            raise OSError("state dir is read-only")

    store = UnwritableStore()
    logged = []
    gateway = FakeGateway(
        issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={}
    )

    asyncio.run(
        workflow(
            gateway, store, FakeRunner(stillborn()), log=logged.append
        ).resolve(ISSUE)
    )

    assert any("infra death not persisted" in m for m in logged)
    assert store.get_resolve_retries(ISSUE) == 0
    assert ("relabel", "issue", ISSUE, "ai-working", "ai-ready") in gateway.calls


# ── the breaker still works ──────────────────────────────────────────────────
def test_three_legs_that_ran_without_opening_a_pr_still_trip_the_breaker():
    """Anti-vacuity twin. The breaker exists because this leg loops unbounded
    at full session cost; the fix must not have disarmed it."""
    store = FakeStore()
    run_resolves(store, ran_and_failed(), 3)

    assert store.get_resolve_retries(ISSUE) == 3

    actions = decide_from(store)
    assert resolve_dispatches(actions) == []
    assert "resolve failed 3× without opening a PR" in comments(actions)


def test_mixed_history_counts_only_the_legs_that_ran():
    """Two real failures and three tombstones is TWO strikes, not five — and
    two is below the cap, so the issue keeps working."""
    store = FakeStore()
    run_resolves(store, ran_and_failed(), 2)
    run_resolves(store, stillborn(), 3)

    assert store.get_resolve_retries(ISSUE) == 2

    actions = decide_from(store)
    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE)]
    assert "resolve failed" not in comments(actions)


def test_a_leg_that_ran_decays_the_infra_deaths_that_preceded_it():
    """The infra ceiling bounds CONSECUTIVE failures to run. A leg that
    returned proves the runtime is alive again, so the tombstones clear."""
    store = FakeStore()
    run_resolves(store, stillborn(), 3)
    run_resolves(store, ran_and_failed(), 1)

    assert store.get_infra_deaths(ISSUE) == 0
    assert store.get_resolve_retries(ISSUE) == 1


# ── the rest of the taxonomy keeps its existing meaning ──────────────────────
def test_wall_timeout_is_a_quality_strike_not_an_infra_failure():
    """The leg RAN and spent its whole budget. A reliably-hanging task is a
    difficulty signal; exempting it buys the identical hang forever."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True, session_id="sess-1", exit_reason=ExitReason.WALL_TIMEOUT,
    ), 3)

    assert store.get_resolve_retries(ISSUE) == 3
    assert store.get_infra_deaths(ISSUE) == 0


def test_budget_exhausted_admission_denial_touches_neither_counter():
    """It never started, so it is not a strike — and it has its own operator
    surface (needs-budget-review), so it is not an infra death either."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True,
        envelope_subtype="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    ), 3)

    assert store.get_resolve_retries(ISSUE) == 0
    assert store.get_infra_deaths(ISSUE) == 0


def test_admission_refusal_subtypes_are_infra_not_strikes():
    """The accounting runtime reports a refused claim as AGENT_ERROR because it
    ends a dispatch, but nothing about the ISSUE caused it."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True,
        status="dispatch_claim_unavailable",
        envelope_subtype="dispatch_claim_unavailable",
        exit_reason=ExitReason.AGENT_ERROR,
    ), 3)

    assert store.get_resolve_retries(ISSUE) == 0
    assert store.get_infra_deaths(ISSUE) == 3


# ── router evidence (#16) ────────────────────────────────────────────────────
def test_router_sees_no_difficulty_evidence_from_stillborn_legs():
    """`EscalationSignals.resolve_retries` documents itself as the fixer's
    NON-INFRA failure counter; #130 proved the invariant was only a comment.
    A phantom notch here buys a stronger tier and a higher effort — #34 was
    escalated medium→high on this evidence and spent $14."""
    store = FakeStore()
    run_resolves(store, stillborn(), 3)

    signals = workflow(
        FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}),
        store,
        FakeRunner(stillborn()),
    )._signals(ISSUE)

    assert signals.resolve_retries == 0
    assert signals.failure_count() == 0
    assert signals.notches() == 0
    assert not signals.requires_ceiling()


def test_router_still_sees_evidence_from_legs_that_ran():
    store = FakeStore()
    run_resolves(store, ran_and_failed(), 2)

    signals = workflow(
        FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}),
        store,
        FakeRunner(ran_and_failed()),
    )._signals(ISSUE)

    assert signals.resolve_retries == 2
    assert signals.requires_ceiling()


# ── malformed / absent counter state ─────────────────────────────────────────
def test_absent_counters_read_as_no_history():
    store = FakeStore()

    actions = decide_from(store)

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE)]


def test_a_store_that_cannot_answer_degrades_to_no_evidence_not_a_dead_leg():
    """The counters are an optimisation of WHICH configuration to dispatch,
    never a precondition for dispatching."""

    class BrokenStore(FakeStore):
        def get_resolve_retries(self, issue):
            raise OSError("state dir is gone")

        def get_ci_retries(self, issue):
            raise OSError("state dir is gone")

    signals = workflow(
        FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}),
        BrokenStore(),
        FakeRunner(stillborn()),
    )._signals(ISSUE)

    assert signals.resolve_retries == 0
    assert signals.ci_retries == 0


def test_a_ready_issue_at_neither_ceiling_is_never_escalated_by_either_reason():
    """Boundary: one below both caps."""
    state = FixState(resolve_retries={ISSUE: 2}, infra_deaths={ISSUE: 4})

    actions = decide_fix(
        world(issues=[build_issue(ISSUE, "ai-ready")]), CFG, state, fixer_busy=False
    )

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE)]
    assert not any(
        a.to_label is Label.NEEDS_HUMAN_REVIEW
        for a in actions
        if hasattr(a, "to_label")
    )
