from dataclasses import replace
from types import SimpleNamespace
import pytest

from issue_agent.adapters.control_store import AccountingDisposition
from issue_agent.adapters.effects import action_from_command, command_for_action
from issue_agent.adapters.store import Store
from issue_agent.app.execute import Executor
from issue_agent.app.roles import _budget_self_clear_worlds
from issue_agent.domain.actions import Target
from issue_agent.domain.contracts import Money
from issue_agent.domain.events import (
    BlockRetired,
    BudgetBlockRetired,
    SelfClearCeilingReached,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.self_clear import (
    BudgetBlock,
    BudgetLimitObservation,
    BudgetWorld,
    MachineBlock,
    MachineBlocker,
    self_clear_actions,
)
from issue_agent.domain.world import IssueView, PRView, WorldState
from tests.fakes.executor import ImmediateExecutor
from tests.fakes.in_memory_github import InMemoryGitHub


class _Journal:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _executor(gateway, store, journal):
    def retire(action):
        store._workflow_db.retire_machine_block(action.revision, blocker=action.blocker,
                                                blocked_head=action.blocked_head)
        return store.increment_self_clear_count(action.issue)
    return ImmediateExecutor(
        gateway,
        journal=journal,
        retire_machine_block=retire,
        retire_budget_block=store.complete_budget_self_clear,
        mark_self_clear_ceiling=store.mark_self_clear_ceiling_reported,
        discard_machine_block=lambda action: store._workflow_db.retire_machine_block(action.revision),
        discard_budget_block=store.clear_budget_block,
    )


def test_machine_retirement_is_journalled_after_label_is_observed_gone(tmp_path):
    import asyncio
    from issue_agent.domain.outcome import AgentResult
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    from tests.app.test_pr_repair_workflow import active_ci_workflow
    pr = PRView(
        100,
        head_ref_oid="published-head",
        labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}),
        closing_issue=5,
    )
    store = Store(str(tmp_path))
    workflow, store, _, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "publication"}, cost_usd=.01), store)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="published-head")
    asyncio.run(workflow.fix_ci(100))
    selected = store._workflow_db.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    block = selected.component.state.machine_block
    assert block is not None
    journal = _Journal()
    gateway = InMemoryGitHub(prs={100: pr})
    actions = self_clear_actions(
        WorldState(prs=(pr,)), {5: block}, {}, {}, {5: 0}, frozenset(),
        machine_revisions={5: selected.revision}
    )

    assert _executor(gateway, store, journal).execute_all(actions)

    assert not gateway.has_label("pr", 100, "ai-blocked")
    assert store._workflow_db.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state.machine_block is None
    assert store.get_self_clear_count(5) == 1
    assert [
        event.payload
        for event in journal.events
        if isinstance(event.payload, BlockRetired)
    ] == [BlockRetired("publication", "published-head", "published-head", 1)]


def test_budget_retirement_removes_both_labels_and_journals_headroom(tmp_path):
    issue = IssueView(
        7,
        labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}),
        updated_at="rev",
    )
    block = BudgetBlock(
        7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000, 100_000
    )
    budget = BudgetWorld((
        BudgetLimitObservation("issue:7", "available", 3_000_000, 5_000_000),
    ), True)
    store = Store(str(tmp_path))
    store.set_budget_block(block)
    journal = _Journal()
    class RevisionBumpingGateway(InMemoryGitHub):
        def relabel(self, kind, number, remove, add):
            super().relabel(kind, number, remove, add)
            current = self.issues[number]
            self.issues[number] = replace(
                current, updated_at=f"{current.updated_at}-next"
            )

    gateway = RevisionBumpingGateway(issues=(issue,))
    actions = self_clear_actions(
        WorldState(issues=(issue,)), {}, {7: block}, {7: budget}, {7: 0}, frozenset()
    )

    assert len(actions) == 2
    relabel = actions[0]
    command = command_for_action(
        relabel,
        repository="o/r",
        executor_identity="fix:test",
    )
    assert action_from_command(command) == relabel
    assert _executor(gateway, store, journal).execute_all(actions)

    assert gateway.has_label("issue", 7, "ai-ready")
    assert not gateway.has_label("issue", 7, "ai-blocked")
    assert not gateway.has_label("issue", 7, "needs-budget-review")
    assert [
        event.payload
        for event in journal.events
        if isinstance(event.payload, BudgetBlockRetired)
    ] == [BudgetBlockRetired(
        "issue:7", 100_000, 2_000_000, 2_000_000, 5_000_000, "USD", 1
    )]


def test_labels_without_harness_marker_do_not_synthesize_budget_authority():
    class _Control:
        def snapshot(self, scope):
            return SimpleNamespace(
                disposition=AccountingDisposition.AVAILABLE,
                committed=Money("USD", 3_000_000),
            )

    cfg = SimpleNamespace(
        repo="o/r",
        fix_budget_usd=2.0,
        issue_spend_cap_usd=5.0,
        daily_spend_cap_usd=0.0,
        global_daily_spend_cap_usd=0.0,
    )
    issue = IssueView(
        7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW})
    )

    blocks, worlds = _budget_self_clear_worlds(
        _Control(), cfg, WorldState(issues=(issue,)), {}
    )

    assert blocks == {}
    assert worlds == {}


def test_harness_marker_observes_a_half_cleared_budget_transition():
    class _Control:
        def snapshot(self, scope):
            return SimpleNamespace(
                disposition=AccountingDisposition.AVAILABLE,
                committed=Money("USD", 3_000_000),
            )

    cfg = SimpleNamespace(
        repo="o/r",
        fix_budget_usd=2.0,
        issue_spend_cap_usd=5.0,
        daily_spend_cap_usd=0.0,
        global_daily_spend_cap_usd=0.0,
    )
    issue = IssueView(7, labels=frozenset({Label.NEEDS_BUDGET_REVIEW}))
    marker = BudgetBlock(
        7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000
    )

    blocks, worlds = _budget_self_clear_worlds(
        _Control(), cfg, WorldState(issues=(issue,)), {7: marker}
    )

    assert blocks == {7: marker}
    assert worlds[7].complete is True


def test_malformed_store_markers_fail_closed(tmp_path):
    store = Store(str(tmp_path))
    store._write(store.issue_path(7, "budget-block"), '{"reservation_micros":-1}')

    with pytest.raises(ValueError, match="control unavailable"):
        store.get_budget_block(7)


def test_ceiling_event_is_persisted_once(tmp_path):
    block = MachineBlock(
        7, Target.PR, 100, 100, MachineBlocker.PUBLICATION, "head"
    )
    pr = PRView(100, head_ref_oid="head", labels=frozenset({Label.AI_BLOCKED}))
    store = Store(str(tmp_path))
    journal = _Journal()
    executor = Executor(
        InMemoryGitHub(prs={100: pr}),
        journal=journal,
        retire_machine_block=lambda action: store.increment_self_clear_count(action.issue),
        retire_budget_block=store.complete_budget_self_clear,
        mark_self_clear_ceiling=store.mark_self_clear_ceiling_reported,
    )
    action = self_clear_actions(
        WorldState(prs=(pr,)), {7: block}, {}, {}, {7: 3}, frozenset()
    )[0]

    executor.execute(action)

    assert store.self_clear_ceiling_reported(7)
    assert any(
        isinstance(event.payload, SelfClearCeilingReached)
        for event in journal.events
    )
