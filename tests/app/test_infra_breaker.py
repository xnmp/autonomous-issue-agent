"""Deployment-wide infra breaker (keifu, 2026-08-02 04:53-04:58Z).

systemd's minimal PATH resolved a `codex` 0.142.3 whose backend answered every
request with an instant 400, so every leg spawn was stillborn. Issues #34,
#130, #132 and #142 each walked their OWN per-issue infra ladder — a dozen-plus
deaths in five minutes — and each produced its own human escalation. Nothing
recognised that the runtime, not any issue, was broken.

These tests pin the granularity rule end to end: deaths that cross issues pause
PAID admission for the whole deployment and escalate ONCE; deaths on a single
issue stay that issue's per-issue ceiling's business; and a leg that actually
runs re-arms everything.
"""

import asyncio

from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.infra_breaker import InfraBreaker
from issue_agent.app.loop import Daemon
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.events import InfraBreakerCleared, InfraBreakerTripped
from issue_agent.domain.infra_breaker import (
    CLEAR_COOLDOWN_ELAPSED,
    CLEAR_PROBE_SUCCEEDED,
    DEFAULT_BREAKER_COOLDOWN_SECONDS,
    DEFAULT_BREAKER_DEATHS,
    DEFAULT_BREAKER_WINDOW_SECONDS,
    BreakerPolicy,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView, WorldState

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees
from tests.fakes.in_memory_github import InMemoryGitHub as LoopGateway


STORM = (34, 130, 132, 142)


class FakeClock:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


class RecordingJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append(event)


def payloads(journal, kind):
    return [
        event.payload
        for event in journal.events
        if isinstance(event.payload, kind)
    ]


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


def hung():
    """The agent was alive and spent its budget — never an infra death."""
    return AgentResult(
        is_error=True,
        summary="wall clock exceeded",
        session_id="sess-hung",
        cost_usd=9.99,
        num_turns=12,
        exit_reason=ExitReason.WALL_TIMEOUT,
    )


def breaker(clock=None, journal=None, policy=None, logs=None):
    return InfraBreaker(
        policy if policy is not None else BreakerPolicy(),
        journal=journal,
        log=(logs.append if logs is not None else (lambda _m: None)),
        clock=clock if clock is not None else FakeClock(),
    )


def run_resolve(issue, result, infra_breaker, store=None):
    """One real resolve leg through the real FixWorkflow."""
    store = store if store is not None else FakeStore()
    gateway = FakeGateway(
        issues={issue: IssueView(issue, title="t", body="b")}, pr_for_issue={}
    )
    worktrees = FakeWorktrees()
    workflow = FixWorkflow(
        FixWorkflowConfig(
            base_branch="dev",
            fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
            fork_threshold=5,
            author_tests_split=False,
            max_contract_rounds=2,
            fix_mcp_config="",
            harness_root="",
            log=lambda _message: None,
            infra_breaker=infra_breaker,
        ),
        gateway,
        store,
        worktrees,
        FakeRunner(result),
        Executor(gateway, remove_worktree=worktrees.remove),
    )
    asyncio.run(workflow.resolve(issue))
    return store


def storm(infra_breaker, result=None, issues=STORM):
    for issue in issues:
        run_resolve(issue, result or stillborn(), infra_breaker)


# ── the incident ─────────────────────────────────────────────────────────────
def test_a_storm_across_issues_trips_the_breaker():
    """One broken binary, four issues, five minutes: paid admission stops."""
    subject = breaker()

    storm(subject)

    assert subject.tripped
    assert subject.gate_reason() is not None


def _fix_daemon(subject, dispatched, gateway):
    claim = Relabel(Target.ISSUE, 34, Label.AI_READY, Label.AI_WORKING)
    return Daemon(
        "fix",
        lambda: WorldState(),
        lambda _world: [claim, RunAgent(AgentKind.RESOLVE, 34, claim)],
        Executor(gateway, run_agent=dispatched.append),
        tick_gate=subject.gate_reason,
    )


def test_a_tripped_breaker_blocks_paid_dispatch_deployment_wide():
    """The effect is the spend cap's: paid legs withheld, free work proceeds."""
    dispatched: list = []
    subject = breaker()

    # Control: the identical tick dispatches while the breaker is armed.
    assert _fix_daemon(subject, dispatched, LoopGateway()).tick() != []
    assert len(dispatched) == 1

    storm(subject)

    assert _fix_daemon(subject, dispatched, LoopGateway()).tick() == []
    assert len(dispatched) == 1


def test_the_storm_produces_one_escalation_not_one_per_issue():
    """Four issues used to escalate four times for one broken binary."""
    journal = RecordingJournal()
    logs: list[str] = []
    subject = breaker(journal=journal, logs=logs)

    storm(subject)
    for issue in STORM:  # the storm keeps going while it holds
        run_resolve(issue, stillborn(), subject)

    assert len(payloads(journal, InfraBreakerTripped)) == 1
    assert len([line for line in logs if "INFRA BREAKER" in line]) == 1


def test_the_escalation_names_its_cause_and_the_remediation_attempted():
    """ADR 0013: a machine-checkable predicate plus evidence, not prose."""
    journal = RecordingJournal()
    subject = breaker(journal=journal)

    storm(subject)

    tripped = payloads(journal, InfraBreakerTripped)[0]
    assert tripped.cause.startswith("infra_deaths_across_issues(")
    assert "window_s=600" in tripped.cause
    assert "runtime_unavailable" in tripped.cause
    assert tripped.remediation.startswith("per_issue_infra_ceilings_charged(")
    assert tripped.issues == tuple(sorted(STORM))
    assert tripped.deaths == len(STORM)


def test_the_operator_line_names_the_cause_and_the_remediation():
    logs: list[str] = []
    subject = breaker(logs=logs)

    storm(subject)

    line = next(entry for entry in logs if "INFRA BREAKER" in entry)
    assert "infra_deaths_across_issues(" in line
    assert "per_issue_infra_ceilings_charged(" in line


# ── granularity: one issue is NOT the deployment ─────────────────────────────
def test_one_issue_dying_repeatedly_never_trips_the_breaker():
    """That is the per-issue infra ceiling's job, and escalating one issue is
    the right blast radius for a fault only one issue can see."""
    subject = breaker()
    store = FakeStore()

    for _ in range(DEFAULT_BREAKER_DEATHS * 3):
        run_resolve(130, stillborn(), subject, store=store)

    assert not subject.tripped
    assert subject.gate_reason() is None
    # …and the per-issue ceiling did its own counting, unchanged.
    assert store.get_infra_deaths(130) == DEFAULT_BREAKER_DEATHS * 3


def test_deaths_below_the_threshold_do_not_trip_it():
    subject = breaker()

    storm(subject, issues=STORM[: DEFAULT_BREAKER_DEATHS - 1])

    assert not subject.tripped


# ── what does not count ──────────────────────────────────────────────────────
def test_wall_timeouts_never_count_toward_the_breaker():
    """The agent was alive and spent its budget: that is a difficulty signal
    about the issue, not evidence the runtime is broken."""
    subject = breaker()

    storm(subject, result=hung())

    assert not subject.tripped


def test_ordinary_agent_failures_never_count_toward_the_breaker():
    subject = breaker()

    storm(subject, result=ran_and_failed())

    assert not subject.tripped


# ── window ───────────────────────────────────────────────────────────────────
def test_deaths_spread_beyond_the_window_do_not_trip_it():
    """Four deaths over an hour is ordinary weather, not a storm."""
    clock = FakeClock()
    subject = breaker(clock=clock)

    for issue in STORM:
        run_resolve(issue, stillborn(), subject)
        clock.advance(DEFAULT_BREAKER_WINDOW_SECONDS)

    assert not subject.tripped


def test_evidence_at_the_window_edge_has_expired():
    clock = FakeClock()
    subject = breaker(clock=clock)

    run_resolve(34, stillborn(), subject)
    clock.advance(DEFAULT_BREAKER_WINDOW_SECONDS)
    storm(subject, issues=(130, 132, 142))

    assert not subject.tripped


# ── recovery ─────────────────────────────────────────────────────────────────
def test_a_successful_leg_clears_the_breaker():
    journal = RecordingJournal()
    subject = breaker(journal=journal)
    storm(subject)

    run_resolve(34, ran_and_failed(), subject)

    assert not subject.tripped
    assert subject.gate_reason() is None
    cleared = payloads(journal, InfraBreakerCleared)
    assert [event.reason for event in cleared] == [CLEAR_PROBE_SUCCEEDED]
    assert cleared[0].cause.startswith("infra_deaths_across_issues(")


def test_the_cooldown_re_arms_a_breaker_no_leg_can_clear():
    """While it holds nothing paid dispatches, so no leg can succeed to clear
    it — the cooldown is what stops a trip becoming a permanent stop."""
    clock = FakeClock()
    journal = RecordingJournal()
    subject = breaker(clock=clock, journal=journal)
    storm(subject)

    clock.advance(DEFAULT_BREAKER_COOLDOWN_SECONDS - 1)
    assert subject.gate_reason() is not None

    clock.advance(1)
    assert subject.gate_reason() is None
    assert [event.reason for event in payloads(journal, InfraBreakerCleared)] == [
        CLEAR_COOLDOWN_ELAPSED
    ]


def test_a_still_broken_runtime_re_trips_after_the_cooldown():
    clock = FakeClock()
    journal = RecordingJournal()
    subject = breaker(clock=clock, journal=journal)
    storm(subject)
    clock.advance(DEFAULT_BREAKER_COOLDOWN_SECONDS)
    subject.gate_reason()

    storm(subject)

    assert subject.tripped
    assert len(payloads(journal, InfraBreakerTripped)) == 2


def test_a_healthy_leg_starts_a_fresh_window():
    """A success must not leave a storm's evidence one death short of a trip."""
    subject = breaker()
    storm(subject, issues=STORM[: DEFAULT_BREAKER_DEATHS - 1])

    run_resolve(34, ran_and_failed(), subject)
    run_resolve(142, stillborn(), subject)

    assert not subject.tripped


# ── config ───────────────────────────────────────────────────────────────────
def test_absent_knobs_default_sanely():
    policy = BreakerPolicy.sane()

    assert policy == BreakerPolicy(
        deaths=DEFAULT_BREAKER_DEATHS,
        window_seconds=DEFAULT_BREAKER_WINDOW_SECONDS,
        cooldown_seconds=DEFAULT_BREAKER_COOLDOWN_SECONDS,
    )


def test_malformed_knobs_default_sanely():
    """A typo must not silently disable the breaker or make it trip on air."""
    for bad in ("4", "", None, 0, -1, True, float("nan"), float("inf"), object()):
        policy = BreakerPolicy.sane(bad, bad, bad)
        assert policy.deaths == DEFAULT_BREAKER_DEATHS
        assert policy.window_seconds == DEFAULT_BREAKER_WINDOW_SECONDS
        assert policy.cooldown_seconds == DEFAULT_BREAKER_COOLDOWN_SECONDS


def test_operator_knobs_are_honoured():
    clock = FakeClock()
    subject = breaker(
        clock=clock,
        policy=BreakerPolicy.sane(2, 60, 30),
    )

    storm(subject, issues=(34, 130))

    assert subject.tripped
    clock.advance(30)
    assert subject.gate_reason() is None


def test_a_disabled_breaker_is_absent_and_the_workflow_still_counts():
    """None = per-issue ceilings only, the pre-breaker behaviour."""
    store = FakeStore()

    for issue in STORM:
        run_resolve(issue, stillborn(), None, store=store)

    assert store.get_infra_deaths(STORM[0]) == 1


def test_a_broken_breaker_never_fails_the_leg():
    """It is an efficiency control; it must not become a second failure on top
    of the death it is being told about."""

    class Exploding:
        def record_death(self, *_args, **_kwargs):
            raise RuntimeError("boom")

        def record_success(self):
            raise RuntimeError("boom")

    store = run_resolve(130, stillborn(), Exploding())

    assert store.get_infra_deaths(130) == 1
