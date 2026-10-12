"""Budget-cap outcomes stay outside the agent-failure retry ladder."""

import asyncio

from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflowConfig
from issue_agent.domain.events import BudgetPathEntered
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees
from tests.fakes.accounted_workflow import AccountedFixtureWorkflow, attach_workflow_authority, resolve_state, seed_resolve, machine_ends


class RecordingJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


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
    return AccountedFixtureWorkflow(
        FixWorkflowConfig(**config), gateway, store, worktrees, runner,
        Executor(gateway, remove_worktree=worktrees.remove),
        workflow_control=attach_workflow_authority(gateway, store),
    )


def budget_result():
    return AgentResult(
        is_error=True,
        envelope_subtype="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    )


def assert_budget_path(gateway, store, journal, leg, *, failures=0, legs=1):
    assert resolve_state(store, 5).episode.failures == failures
    assert resolve_state(store, 5).gates == ()
    assert machine_ends(store) == [False] * legs  # budget exhaustion is not a machine failure
    assert Label.AI_WORKING not in gateway.issues[5].labels
    assert {Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW} <= gateway.issues[5].labels
    budget_events = [(event, fields) for event, fields in journal.events if isinstance(event.payload, BudgetPathEntered)]
    assert len(budget_events) == 1
    event, fields = budget_events[0]
    assert fields == {}
    assert event.payload == BudgetPathEntered(leg, "budget_exhausted")
    assert event.correlation.issue == 5


def test_budget_exhausted_resolve_blocks_for_cap_review_without_a_strike():
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    journal = RecordingJournal()

    asyncio.run(workflow(gateway, store, FakeRunner(budget_result()), journal=journal).resolve(5))

    assert_budget_path(gateway, store, journal, "resolve")


def test_agent_error_resolve_still_bumps_the_strike_counter():
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()

    asyncio.run(workflow(gateway, store, FakeRunner(AgentResult(
        is_error=True, exit_reason=ExitReason.AGENT_ERROR,
    ))).resolve(5))

    assert resolve_state(store, 5).episode.failures == 1


def test_budget_outcome_preserves_existing_episode_strikes_and_session():
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    seed_resolve(store, 5, failures=2, session_id="prior-session", model="m")
    journal = RecordingJournal()
    current = workflow(gateway, store, FakeRunner(budget_result()), journal=journal)
    before = resolve_state(store, 5)

    asyncio.run(current.resolve(5))

    assert_budget_path(gateway, store, journal, "resolve", failures=2)
    assert resolve_state(store, 5).episode.continuation == before.episode.continuation


