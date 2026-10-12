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
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.roles import _fix_state
from issue_agent.app.workflows.fix import FixWorkflowConfig
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow import OpenGate, ResolveCeiling

from tests.domain.builders import issue as build_issue, world
from tests.fakes.accounted_workflow import AccountedFixtureWorkflow, attach_workflow_authority, issue_recovery_state, resolve_state, seed_resolve

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees


ISSUE = 130
CFG = FixConfig(fix_bot="fix-bot", max_resolve_retries=3)


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
    """Use the SQL-owned episode; no leg history means no machine backoff."""
    state = FixState(resolve_workflows={ISSUE: resolve_state(store, ISSUE)})
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


# ── the rest of the taxonomy keeps its existing meaning ──────────────────────
def test_wall_timeout_is_a_quality_strike_not_a_machine_failure():
    """The leg RAN and spent its whole budget. A reliably-hanging task is a
    difficulty signal; exempting it buys the identical hang forever."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True, session_id="sess-1", exit_reason=ExitReason.WALL_TIMEOUT,
    ), 3)

    assert resolve_state(store, ISSUE).episode.failures == 3


def test_budget_exhausted_outcome_is_not_a_resolve_strike():
    """Budget outcomes have their own operator surface, not a quality strike
    or a machine failure, even when returned by an admitted runtime."""
    store = FakeStore()

    run_resolves(store, AgentResult(
        is_error=True,
        envelope_subtype="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    ), 3)

    assert resolve_state(store, ISSUE).episode.failures == 0


def test_admission_refusal_subtypes_are_machine_failures_not_strikes():
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


def test_pre_admission_refusals_are_spaced_by_the_machine_backoff_and_never_escalate(monkeypatch):
    """#262 shape on RESOLVE. A selected provider the accounting runtime cannot
    serve is refused before any paid run, every time. The refusal is a machine
    end, so a tick every 30s re-dispatches only on the doubling backoff, never
    escalates, and claims/releases the issue once per dispatch, not per tick."""
    store = FakeStore()
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={})
    runner = FakeRunner(ran_and_failed())
    wf = workflow(gateway, store, runner, fix=FixTaskConfig("o/r", agent_md="AM", model="m", runtime=RuntimeName.CODEX))
    start = datetime(2026, 10, 13, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setattr(store._workflow_db, "_now", lambda: clock[0])
    dispatched, escalations = [], 0
    for tick in range(32):
        clock[0] = start + timedelta(seconds=30 * tick)
        view = world(issues=[build_issue(ISSUE, "ai-ready")], now=clock[0].isoformat().replace("+00:00", "Z"))
        state = _fix_state(store, view, workflow_control=wf.workflow_control, available_runtimes=frozenset({"claude"}))
        actions = decide_fix(view, CFG, state, fixer_busy=False)
        escalations += sum(isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW for a in actions)
        if resolve_dispatches(actions):
            dispatched.append(30 * tick)
            asyncio.run(wf.resolve(ISSUE))
    assert runner.tasks == [] and escalations == 0
    assert dispatched == [0, 60, 180, 420, 900]
    assert len([c for c in gateway.calls if c[0] == "relabel"]) <= 2 * len(dispatched)


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


def test_a_ready_issue_below_the_resolve_ceiling_is_never_escalated():
    """Boundary: one below the cap."""
    store = FakeStore()
    seed_resolve(store, ISSUE, failures=2)
    attach_workflow_authority(FakeGateway(issues={ISSUE: IssueView(ISSUE)}), store)
    state = FixState(resolve_workflows={ISSUE: resolve_state(store, ISSUE)})

    actions = decide_fix(
        world(issues=[build_issue(ISSUE, "ai-ready")]), CFG, state, fixer_busy=False
    )

    assert resolve_dispatches(actions) == [RunAgent(AgentKind.RESOLVE, ISSUE, workflow=resolve_state(store, ISSUE).binding)]
    assert not any(
        a.add is Label.NEEDS_HUMAN_REVIEW
        for a in actions
        if isinstance(a, Relabel)
    )
