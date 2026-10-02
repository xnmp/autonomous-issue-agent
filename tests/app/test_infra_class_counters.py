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

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflowConfig
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent
from issue_agent.domain.events import InfraPathEntered
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow import OpenGate, ResolveCeiling

from tests.domain.builders import issue as build_issue, world
from tests.fakes.accounted_workflow import AccountedFixtureWorkflow, attach_workflow_authority, issue_recovery_state, resolve_state, seed_resolve

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
    authority = attach_workflow_authority(gateway, store, resolve_ceiling=CFG.max_resolve_retries)
    return AccountedFixtureWorkflow(
        FixWorkflowConfig(**config), gateway, store, worktrees, runner,
        Executor(gateway, remove_worktree=worktrees.remove),
        workflow_control=authority,
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
    """Use the SQL-owned episode and separate infrastructure counter."""
    state = FixState(
        resolve_workflows={ISSUE: resolve_state(store, ISSUE)},
        infra_deaths={ISSUE: issue_recovery_state(store, ISSUE).infra_failures},
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

    assert resolve_state(store, ISSUE).episode.failures == 0


def test_next_healthy_dispatch_after_three_stillborn_legs_is_not_escalated():
    """The exact #130 shape: the harness is fixed, the issue is re-promoted,
    and the very first attempt must be a dispatch — not a human escalation."""
    store = FakeStore()
    run_resolves(store, stillborn(), 3)

    actions = decide_from(store)

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE, workflow=resolve_state(store, ISSUE).binding)]
    assert "resolve failed" not in comments(actions)


def test_stillborn_legs_take_the_infra_ceiling_instead():
    """The exemption is not an amnesty: a PERSISTENTLY broken runtime still
    reaches a human, under its own bounded counter."""
    store = FakeStore()

    run_resolves(store, stillborn(), 3)

    assert issue_recovery_state(store, ISSUE).infra_failures == 3


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


def test_retired_file_counter_failure_does_not_replace_sql_death_or_followups():
    """Retired file writes cannot replace the fault or charge its SQL episode twice."""

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

    assert issue_recovery_state(store, ISSUE).infra_failures == 1
    assert resolve_state(store, ISSUE).episode.failures == 0
    assert ("relabel", "issue", ISSUE, "ai-working", "ai-ready") in gateway.calls


# ── the breaker still works ──────────────────────────────────────────────────
def test_three_legs_that_ran_without_opening_a_pr_still_trip_the_breaker():
    """Anti-vacuity twin. The breaker exists because this leg loops unbounded
    at full session cost; the fix must not have disarmed it."""
    store = FakeStore()
    run_resolves(store, ran_and_failed(), 3)

    assert resolve_state(store, ISSUE).episode.failures == 3

    actions = decide_from(store)
    assert resolve_dispatches(actions) == []
    state = resolve_state(store, ISSUE)
    assert len(state.gates) == 1
    gate = state.gates[0]
    assert isinstance(gate, OpenGate)
    assert gate.cause == ResolveCeiling(state.episode.episode_id, 3, 3, "could not make the tests pass")
    assert "resolve failed 3×" in comments(actions)


def test_mixed_history_counts_only_the_legs_that_ran():
    """Two real failures and three tombstones is TWO strikes, not five — and
    two is below the cap, so the issue keeps working."""
    store = FakeStore()
    run_resolves(store, ran_and_failed(), 2)
    run_resolves(store, stillborn(), 3)

    assert resolve_state(store, ISSUE).episode.failures == 2

    actions = decide_from(store)
    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE, workflow=resolve_state(store, ISSUE).binding)]
    assert "resolve failed" not in comments(actions)


def test_a_leg_that_ran_decays_the_infra_deaths_that_preceded_it():
    """The infra ceiling bounds CONSECUTIVE failures to run. A leg that
    returned proves the runtime is alive again, so the tombstones clear."""
    store = FakeStore()
    run_resolves(store, stillborn(), 3)
    run_resolves(store, ran_and_failed(), 1)

    assert issue_recovery_state(store, ISSUE).infra_failures == 0
    assert resolve_state(store, ISSUE).episode.failures == 1
    session = resolve_state(store, ISSUE).episode.continuation.session
    assert session is not None and session.session_id == "sess-abc"


# ── the rest of the taxonomy keeps its existing meaning ──────────────────────
def test_wall_timeout_is_a_quality_strike_not_an_infra_failure():
    """The leg RAN and spent its whole budget. A reliably-hanging task is a
    difficulty signal; exempting it buys the identical hang forever."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True, session_id="sess-1", exit_reason=ExitReason.WALL_TIMEOUT,
    ), 3)

    assert resolve_state(store, ISSUE).episode.failures == 3
    assert issue_recovery_state(store, ISSUE).infra_failures == 0


def test_budget_exhausted_outcome_touches_neither_counter():
    """Budget outcomes have their own operator surface, not a quality strike
    or an infrastructure death, even when returned by an admitted runtime."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True,
        envelope_subtype="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    ), 3)

    assert resolve_state(store, ISSUE).episode.failures == 0
    assert issue_recovery_state(store, ISSUE).infra_failures == 0


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

    assert resolve_state(store, ISSUE).episode.failures == 0
    assert issue_recovery_state(store, ISSUE).infra_failures == 3


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
def test_initialized_episode_starts_with_no_failure_history():
    store = FakeStore()
    seed_resolve(store, ISSUE)
    attach_workflow_authority(FakeGateway(issues={ISSUE: IssueView(ISSUE)}), store)

    actions = decide_from(store)

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE, workflow=resolve_state(store, ISSUE).binding)]


def test_unavailable_ci_selection_does_not_erase_resolve_failure_evidence(monkeypatch):
    store = FakeStore()
    seed_resolve(store, ISSUE, failures=2)
    current = workflow(
        FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}),
        store,
        FakeRunner(stillborn()),
    )
    def unavailable(_key):
        raise ValueError("CI selected source is unavailable")
    monkeypatch.setattr(current.workflow_control, "read_issue_recovery", unavailable)
    signals = current._signals(ISSUE)

    assert signals.resolve_retries == 2
    assert signals.ci_retries == 0


def test_corrupt_sql_episode_blocks_paid_resolve_instead_of_resetting_evidence():
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")})
    store = FakeStore()
    runner = FakeRunner(ran_and_failed())
    current = workflow(gateway, store, runner)
    binding = resolve_state(store, ISSUE).binding
    with store._workflow_db.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='corrupt' WHERE repository='o/r' AND target_number=?", (ISSUE,))

    with pytest.raises(WorkflowAuthorityError):
        asyncio.run(current.resolve(ISSUE, binding=binding))
    assert runner.tasks == []


def test_a_ready_issue_at_neither_ceiling_is_never_escalated_by_either_reason():
    """Boundary: one below both caps."""
    store = FakeStore()
    seed_resolve(store, ISSUE, failures=2)
    attach_workflow_authority(FakeGateway(issues={ISSUE: IssueView(ISSUE)}), store)
    state = FixState(resolve_workflows={ISSUE: resolve_state(store, ISSUE)}, infra_deaths={ISSUE: 4})

    actions = decide_fix(
        world(issues=[build_issue(ISSUE, "ai-ready")]), CFG, state, fixer_busy=False
    )

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE, workflow=resolve_state(store, ISSUE).binding)]
    assert not any(
        a.add is Label.NEEDS_HUMAN_REVIEW
        for a in actions
        if isinstance(a, Relabel)
    )
