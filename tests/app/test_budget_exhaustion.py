"""Budget-cap outcomes stay outside the agent-failure retry ladder."""

import asyncio

from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import IssueView

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees


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


def budget_result():
    return AgentResult(
        is_error=True,
        envelope_subtype="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    )


def assert_budget_path(gateway, store, journal, leg):
    assert store.get_resolve_retries(5) == 0
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gateway.calls
    assert ("relabel", "issue", 5, None, "needs-budget-review") in gateway.calls
    assert journal.events == [(
        "budget_path",
        {"issue": 5, "leg": leg, "exit_reason": "budget_exhausted"},
    )]


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

    assert store.get_resolve_retries(5) == 1


def test_split_author_budget_exhaustion_uses_the_budget_path():
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    journal = RecordingJournal()

    asyncio.run(workflow(
        gateway, store, FakeRunner(budget_result()), author_tests_split=True, journal=journal,
    ).resolve(5))

    assert_budget_path(gateway, store, journal, "author")


def test_split_implementer_budget_exhaustion_uses_the_budget_path():
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    store.set_phase(5, "authored")
    journal = RecordingJournal()

    asyncio.run(workflow(
        gateway, store, FakeRunner(budget_result()), author_tests_split=True, journal=journal,
    ).resolve(5))

    assert_budget_path(gateway, store, journal, "implement")
