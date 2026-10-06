from dataclasses import replace
from types import SimpleNamespace
import json
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
from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef


class _Journal:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _executor(gateway, store, journal, *, issue_cap=Money("USD", 5_000_000), repository_cap=None, global_cap=None, config_version="config", retry_ceiling=3):
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.adapters.event_log import EventLog
    from issue_agent.domain.events import Producer
    events = EventLog(str(store._workflow_db.path) + ".events", Producer("test", "o/r", "fix", 1, "self-clear"))
    store._workflow_db.bind_event_outbox(events.prepare, journal.events.append)
    return ImmediateExecutor(
        gateway,
        journal=journal,
        retry_delays=(),
        effect_executor=GitHubEffectExecutor(gateway, store._workflow_db, repository="o/r", identity="o/r:fix",
            config_version=config_version, issue_cap=issue_cap, repository_cap=repository_cap, global_cap=global_cap,
            retry_ceiling=retry_ceiling),
        mark_self_clear_ceiling=store._workflow_db.mark_self_clear_ceiling,
        discard_machine_block=lambda action: store._workflow_db.retire_machine_block(action.revision),
        discard_budget_block=lambda action: store._workflow_db.retire_budget_block(action.revision),
    )


def _recovery_state(store, issue):
    return store._workflow_db.read_issue_recovery(WorkflowKey("o/r", IssueRef(issue))).component.state


def _observed_budget(store, world, block):
    cfg = SimpleNamespace(repo="o/r", config_version="config", issue_spend_cap_usd=5.0,
                          daily_spend_cap_usd=0.0, global_daily_spend_cap_usd=0.0)
    return _budget_self_clear_worlds(store._workflow_db, cfg, world, {block.issue: block})[1][block.issue]


def _seed_clear_count(store, issue, count):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    with store._workflow_db.transaction() as db:
        current = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(issue)))
        persist_workflow_snapshot(db, replace(current, issue_recovery=replace(current.issue_recovery,
            state=replace(current.issue_recovery.state, self_clears=count))))


@pytest.mark.parametrize("labels", [
    frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}),
    frozenset({Label.AI_BLOCKED}),
    frozenset({Label.NEEDS_BUDGET_REVIEW}),
])
def test_full_fix_planner_recovers_budget_hold_without_starving_ready_peer(tmp_path, labels):
    from issue_agent.app.roles import _fix_state
    from issue_agent.domain.actions import AgentKind, Relabel, RunAgent
    from issue_agent.domain.fix import FixConfig, decide_fix
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold

    control, workflows = workflow_control(tmp_path, numbers=(7, 8))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000)
    seed_imported_budget_hold(control, block)
    held = IssueView(7, labels=labels, updated_at="held-revision")
    ready = IssueView(8, labels=frozenset({Label.AI_READY}), updated_at="ready-revision")
    observed = WorldState(issues=(held, ready))
    state = _fix_state(store, observed, workflow_control=workflows)
    state = replace(state, budget_worlds={7: _observed_budget(store, observed, block)})

    actions = decide_fix(observed, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    recovery = [action for action in actions if isinstance(action, Relabel) and action.self_clear is not None]
    assert len(recovery) == 1
    assert [action.number for action in actions if isinstance(action, RunAgent)] == [8]

    gateway = InMemoryGitHub(issues=(held, ready))
    assert _executor(gateway, store, _Journal()).execute_all(recovery)
    assert gateway.issues[7].labels == frozenset({Label.AI_READY})
    assert gateway.issues[8].labels == ready.labels
    assert _recovery_state(store, 7).budget_block is None
    assert _recovery_state(store, 7).self_clears == 1

    current = WorldState(issues=(gateway.issues[7],))
    next_actions = decide_fix(current, FixConfig(fix_bot="fix-bot"),
        _fix_state(store, current, workflow_control=workflows), fixer_busy=False)
    assert [(action.kind, action.number) for action in next_actions if isinstance(action, RunAgent)] == [(AgentKind.RESOLVE, 7)]


@pytest.mark.parametrize("cause", ["machine", "budget"])
def test_lost_recovery_reply_adopts_same_source_after_actual_paid_return_once(tmp_path, cause, monkeypatch):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.adapters.workflow_rows import load_issue_aggregate
    from tests.app.test_control_record_isolation import workflow_control
    from tests.app.test_durable_workflow_composition import Crash
    from tests.fakes.accounted_workflow import seed_imported_budget_hold, seed_imported_machine_hold
    from tests.adapters.test_continuation_admission import purchase, paid, receipt, money
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    machine = MachineBlock(7, Target.PR, 100, 100, MachineBlocker.PUBLICATION, "same-head")
    budget = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000)
    seed_imported_machine_hold(control, machine)
    seed_imported_budget_hold(control, budget)
    key = WorkflowKey("o/r", IssueRef(7))
    with control.transaction() as db:
        aggregate = load_issue_aggregate(db, key)
    purchased = replace(purchase(aggregate), branch="fix/7-reply")
    requested = replace(paid("actual-return", purchased), dispatch_id=json.dumps({"repository": "o/r", "role": "fix",
        "issue": 7, "leg_id": purchased.capture.leg_id}))
    assert control.reserve_issue_recovery(requested, purchased).admitted
    admission = receipt(control, requested.run_id)
    selected = control.read_issue_recovery(key)
    issue = IssueView(7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    pr = PRView(100, head_ref_oid="same-head", labels=frozenset({Label.AI_BLOCKED, Label.AI_PR_OPEN}))
    world = WorldState(issues=(issue,), prs=(pr,))
    actions = self_clear_actions(world, {7: machine} if cause == "machine" else {},
        {7: budget} if cause == "budget" else {},
        {7: _observed_budget(store, world, budget)}, {}, frozenset(),
        machine_revisions={7: selected.revision}, budget_revisions={7: selected.revision},
        self_clear_bindings={7: selected.binding})
    assert len(actions) == 1
    gateway = InMemoryGitHub(issues=(issue,), prs={100: pr})
    write = gateway.relabel
    def lost_reply(kind, number, remove, add):
        write(kind, number, remove, add)
        if remove == Label.AI_BLOCKED.value:
            raise Crash("lost recovery reply before current continuation returned")
    monkeypatch.setattr(gateway, "relabel", lost_reply)
    executor = _executor(gateway, store, _Journal(), retry_ceiling=1)
    with pytest.raises(Crash):
        executor.execute(actions[0])
    monkeypatch.setattr(gateway, "relabel", write)
    assert control.settle_issue_recovery(money(requested.run_id), admission, cc.IssueRecoveryState()).disposition is AccountingDisposition.AVAILABLE
    current = control.read_issue_recovery(key)
    assert current.revision.version > selected.revision.version
    assert current.component.state.machine_block == machine and current.component.state.budget_block == budget
    discards = self_clear_actions(WorldState(issues=(gateway.issues[7],), prs=(gateway.prs[100],)),
        {7: machine} if cause == "machine" else {}, {7: budget} if cause == "budget" else {}, {}, {}, frozenset(),
        machine_revisions={7: current.revision}, budget_revisions={7: current.revision})
    assert len(discards) == 1 and not executor.execute_all(discards)
    assert executor.execute_all(actions)
    assert executor.execute_all(actions)
    after = _recovery_state(store, 7)
    assert after.self_clears == 1 and after.resumes == 1 and after.consumed_reply_at == purchased.reply_at
    assert (after.machine_block is None) == (cause == "machine")
    assert (after.budget_block is None) == (cause == "budget")
    assert control.snapshot(requested.scope).settled == Money("USD", 1)


def test_remote_recovery_refusal_keeps_existing_ceiling_after_permission_heals(tmp_path):
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000)
    seed_imported_budget_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    issue = IssueView(7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    world = WorldState(issues=(issue,))
    actions = self_clear_actions(world, {}, {7: block}, {7: _observed_budget(store, world, block)}, {}, frozenset(),
        budget_revisions={7: selected.revision}, self_clear_bindings={7: selected.binding})
    assert len(actions) == 1
    gateway = InMemoryGitHub(issues=(issue,))
    write = gateway.relabel
    gateway.relabel = lambda *args: None
    executor = _executor(gateway, store, _Journal())
    for _ in range(8):
        assert not executor.execute_all(actions)
    gateway.relabel = write
    assert not _executor(gateway, store, _Journal()).execute_all(actions)
    assert gateway.issues[7].labels == issue.labels
    assert _recovery_state(store, 7).budget_block == block
    assert _recovery_state(store, 7).self_clears == 0


def test_lost_budget_reply_uses_current_daily_period_after_restart(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests.app.test_control_record_isolation import workflow_control
    from tests.app.test_durable_workflow_composition import Crash
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    from issue_agent.domain.self_clear import BudgetClearProjection, budget_recovery_limits
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "repository", 5_000_000)
    seed_imported_budget_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    original = control.now()
    cap = Money("USD", 5_000_000)
    limits = budget_recovery_limits("o/r", 7, original, cap, None, None)
    provisional = BudgetClearProjection(selected.revision, block, BudgetWorld(tuple(
        BudgetLimitObservation(scope.dimension, "available", 0, maximum.micros, scope.currency, scope)
        for scope, maximum in limits), True, "config"), selected.binding)
    observed = control.observe_budget_clear_accounting(provisional,
        limits, "config")
    issue = IssueView(7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    actions = self_clear_actions(WorldState(issues=(issue,)), {}, {7: block}, {7: observed}, {}, frozenset(),
        budget_revisions={7: selected.revision}, self_clear_bindings={7: selected.binding})
    assert len(actions) == 1
    gateway = InMemoryGitHub(issues=(issue,))
    write = gateway.relabel
    def lose_reply(*args):
        write(*args)
        if args[2] == Label.AI_BLOCKED.value:
            raise Crash("remote success before midnight reply lost")
    gateway.relabel = lose_reply
    with pytest.raises(Crash):
        _executor(gateway, store, _Journal(), issue_cap=None, repository_cap=cap).execute_all(actions)
    gateway.relabel = write
    monkeypatch.setattr(control, "_now", lambda: original + timedelta(days=1))
    restarted = _executor(gateway, store, _Journal(), issue_cap=None, repository_cap=cap)
    assert restarted.execute_all(actions)
    assert restarted.execute_all(actions)
    assert gateway.issues[7].labels == frozenset({Label.AI_READY})
    assert _recovery_state(store, 7).budget_block is None
    assert _recovery_state(store, 7).self_clears == 1


@pytest.mark.parametrize("target", [Target.ISSUE, Target.PR])
@pytest.mark.parametrize("changed", ["cap", "config", "currency", "disabled", "human", "unavailable"])
def test_changed_budget_evidence_refuses_remote_mutation_and_restored_policy_progresses(tmp_path, target, changed):
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = BudgetBlock(7, target, 7 if target is Target.ISSUE else 100, 2_000_000, "issue:7", 5_000_000)
    seed_imported_budget_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    issue = IssueView(7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    pr = PRView(100, head_ref_oid="budget-head", labels=issue.labels, closing_issue=7)
    world = WorldState(issues=(issue,), prs=(pr,))
    actions = self_clear_actions(world, {}, {7: block}, {7: _observed_budget(store, world, block)}, {}, frozenset(),
        budget_revisions={7: selected.revision}, self_clear_bindings={7: selected.binding})
    assert len(actions) == 1
    gateway = InMemoryGitHub(issues=(issue,), prs={100: pr})
    kwargs = {}
    if changed == "cap":
        kwargs["issue_cap"] = Money("USD", 1_000_000)
    elif changed == "config":
        kwargs["config_version"] = "different-config"
    elif changed == "currency":
        kwargs["issue_cap"] = Money("EUR", 5_000_000)
    elif changed == "disabled":
        kwargs["issue_cap"] = None
    elif changed == "human":
        views = gateway.issues if target is Target.ISSUE else gateway.prs
        views[block.number] = replace(views[block.number], labels=views[block.number].labels | {Label.NEEDS_HUMAN_REVIEW})
    else:
        with control.transaction() as db:
            db.execute("ALTER TABLE charges RENAME TO unavailable_charges")
    assert not _executor(gateway, store, _Journal(), **kwargs).execute_all(actions)
    assert _recovery_state(store, 7).budget_block == block and _recovery_state(store, 7).self_clears == 0
    assert (gateway.issues[7] if target is Target.ISSUE else gateway.prs[100]).has(Label.AI_BLOCKED)
    if changed == "unavailable":
        with control.transaction() as db:
            db.execute("ALTER TABLE unavailable_charges RENAME TO charges")
    elif changed == "human":
        gateway.issues[7] = issue
        gateway.prs[100] = pr
    restored = _executor(gateway, store, _Journal())
    assert restored.execute_all(actions)
    assert restored.execute_all(actions)
    assert _recovery_state(store, 7).budget_block is None and _recovery_state(store, 7).self_clears == 1
    assert (gateway.issues[7] if target is Target.ISSUE else gateway.prs[100]).labels == frozenset({
        Label.AI_READY if target is Target.ISSUE else Label.AI_PR_OPEN})


@pytest.mark.parametrize("remote_stage", ["none", "partial", "full"])
@pytest.mark.parametrize("changed_accounting", [False, True])
@pytest.mark.parametrize("dimension", ["issue:7", "repository", "global"])
def test_budget_recovery_rechecks_committed_reservations_before_remote_release(tmp_path, remote_stage, changed_accounting, dimension, monkeypatch):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from issue_agent.adapters.control_store import ReservationRequest
    from issue_agent.domain.contracts import BudgetScope, PriceQuote, ReservationId, RunId
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000)
    seed_imported_budget_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    issue = IssueView(7, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    world = WorldState(issues=(issue,))
    cfg = SimpleNamespace(repo="o/r", config_version="config", issue_spend_cap_usd=5.0,
                          daily_spend_cap_usd=5.0, global_daily_spend_cap_usd=5.0)
    blocks, budgets = _budget_self_clear_worlds(control, cfg, world, {7: block})
    actions = self_clear_actions(world, {}, blocks, budgets, {}, frozenset(),
                                 budget_revisions={7: selected.revision})
    assert len(actions) == 1
    gateway = InMemoryGitHub(issues=(issue,))
    caps = dict(issue_cap=Money("USD", 5_000_000), repository_cap=Money("USD", 5_000_000), global_cap=Money("USD", 5_000_000))
    executor = _executor(gateway, store, _Journal(), **caps)
    if remote_stage != "none":
        from tests.app.test_durable_workflow_composition import Crash
        write = gateway.relabel
        def lost_reply(kind, number, remove, add):
            write(kind, number, remove, add)
            if remove == (Label.AI_BLOCKED.value if remote_stage == "full" else Label.NEEDS_BUDGET_REVIEW.value):
                raise Crash("remote budget labels applied before lost reply")
        monkeypatch.setattr(gateway, "relabel", lost_reply)
        with pytest.raises(Crash):
            executor.execute(actions[0])
        monkeypatch.setattr(gateway, "relabel", write)
        assert gateway.issues[7].labels == (frozenset({Label.AI_READY}) if remote_stage == "full"
                                         else frozenset({Label.AI_READY, Label.AI_BLOCKED}))
        assert _recovery_state(store, 7).budget_block == block
    now = datetime.now(timezone.utc)
    scope = next(item.budget_scope for item in budgets[7].limits if item.scope == dimension)
    assert scope is not None
    reservation = ReservationRequest(ReservationId("competing-reservation"), RunId("competing-run"),
        "competing-dispatch", scope, PriceQuote("model", "card", "1", now, Money("USD", 4_000_000)),
        Money("USD", 5_000_000), "config", "o/r", owner_id="o/r:fix:fixture")
    if changed_accounting:
        assert control.reserve(reservation).admitted
        assert control.snapshot(scope).committed == Money("USD", 4_000_000)
    restarted = _executor(gateway, store, _Journal(), **caps)
    if not changed_accounting:
        assert restarted.execute_all(actions)
        assert restarted.execute_all(actions)
        assert gateway.issues[7].labels == frozenset({Label.AI_READY})
        assert _recovery_state(store, 7).budget_block is None
        assert _recovery_state(store, 7).self_clears == 1
        return
    assert not restarted.execute_all(actions)
    if remote_stage == "full":
        discards = self_clear_actions(WorldState(issues=(gateway.issues[7],)), {}, blocks, {}, {}, frozenset(),
                                     budget_revisions={7: selected.revision})
        assert len(discards) == 1
        with control.transaction() as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT payload_json FROM lifecycle_commands INDEXED BY lifecycle_self_clear_pending "
                "WHERE repository=? AND target=? AND state IN ('pending','executing','retryable_failure','unknown_outcome') "
                "AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.self_clear') END='text' LIMIT 65", ("o/r", "issue:7")).fetchall()
        assert any("SEARCH lifecycle_commands USING INDEX lifecycle_self_clear_pending" in row[3] for row in plan)
        assert all("SCAN" not in row[3] for row in plan)
        assert not restarted.execute_all(discards)
    elif remote_stage == "none":
        assert gateway.issues[7].labels == issue.labels
    assert _recovery_state(store, 7).budget_block == block
    assert _recovery_state(store, 7).self_clears == 0
    from issue_agent.adapters.control_store import SettlementRequest
    from issue_agent.domain.contracts import PricingBasis
    assert control.settle(SettlementRequest(RunId("competing-run"), Money("USD", 1_000_000),
        PricingBasis.PROVIDER_REPORTED, "competing-reported-cost", None, None, None)).disposition is AccountingDisposition.AVAILABLE
    assert control.snapshot(scope).committed == Money("USD", 1_000_000)
    if remote_stage == "none":
        fresh_blocks, fresh_budgets = _budget_self_clear_worlds(control, cfg, world, {7: block})
        actions = self_clear_actions(world, {}, fresh_blocks, fresh_budgets, {}, frozenset(),
                                     budget_revisions={7: selected.revision})
    assert restarted.execute_all(actions)
    assert restarted.execute_all(actions)
    assert gateway.issues[7].labels == frozenset({Label.AI_READY})
    assert _recovery_state(store, 7).budget_block is None
    assert _recovery_state(store, 7).self_clears == 1


def test_alternating_machine_budget_recoveries_and_restart_consume_one_sql_ceiling(tmp_path):
    import asyncio
    from issue_agent.domain.outcome import AgentResult
    from tests.app.test_pr_repair_workflow import active_ci_workflow
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    workflow, store, _, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "publication"}, cost_usd=.01), Store(str(tmp_path)))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="head")
    pr = PRView(100, head_ref_oid="head", labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}), closing_issue=5)
    issue = IssueView(5, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    gateway = InMemoryGitHub(prs={100: pr}, issues=(issue,))
    journal = _Journal()
    key = WorkflowKey("o/r", IssueRef(5))
    for index, cause in enumerate(("machine", "budget", "machine", "budget"), 1):
        if cause == "machine":
            asyncio.run(workflow.fix_ci(100))
            gateway.prs[100] = pr
        else:
            seed_imported_budget_hold(store._workflow_db, BudgetBlock(5, Target.ISSUE, 5, 2_000_000, "issue:5", 5_000_000, 100_000))
            gateway.issues[5] = issue
        selected = store._workflow_db.read_issue_recovery(key)
        state = selected.component.state
        actions = self_clear_actions(WorldState(prs=(gateway.prs[100],), issues=(gateway.issues[5],)),
            {5: state.machine_block} if state.machine_block is not None else {},
            {5: state.budget_block} if state.budget_block is not None else {},
            {5: _observed_budget(store, WorldState(issues=(gateway.issues[5],)), state.budget_block)} if state.budget_block is not None else {},
            {5: state.self_clears}, frozenset({5}) if state.self_clear_ceiling_reported else frozenset(),
            machine_revisions={5: selected.revision}, budget_revisions={5: selected.revision})
        # Recreate the executor before each cause, then redeliver exact work.
        executor = _executor(gateway, store, journal)
        assert executor.execute_all(actions)
        assert executor.execute_all(actions)
        after = _recovery_state(store, 5)
        assert after.self_clears == min(index, 3)
        if index < 4:
            assert after.machine_block is None and after.budget_block is None
        else:
            assert after.budget_block is not None and after.self_clear_ceiling_reported
            assert gateway.issues[5].labels == issue.labels
    assert sum(isinstance(event.payload, SelfClearCeilingReached) for event in journal.events) == 1
    assert not (tmp_path / "issue-5.self-clears").exists()
    assert not (tmp_path / "issue-5.self-clear-ceiling").exists()


@pytest.mark.parametrize("cause", ["machine", "budget"])
def test_external_discard_retires_only_selected_cause_without_count_and_stale_replay_is_inert(tmp_path, cause):
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold, seed_imported_machine_hold
    from issue_agent.domain.actions import DiscardSelfClearMarker
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    machine = MachineBlock(7, Target.PR, 100, 100, MachineBlocker.PUBLICATION, "head")
    budget = BudgetBlock(7, Target.ISSUE, 7, 2_000_000, "issue:7", 5_000_000, 100_000)
    seed_imported_machine_hold(control, machine)
    seed_imported_budget_hold(control, budget)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    pr = PRView(100, head_ref_oid="head" if cause == "machine" else "different-head",
                labels=frozenset() if cause == "machine" else frozenset({Label.AI_BLOCKED}))
    issue = IssueView(7, labels=frozenset() if cause == "budget" else frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    actions = self_clear_actions(WorldState(prs=(pr,), issues=(issue,)), {7: machine}, {7: budget}, {}, {}, frozenset(),
        machine_revisions={7: selected.revision}, budget_revisions={7: selected.revision})
    assert actions == [DiscardSelfClearMarker(7, cause, selected.revision)]
    executor = _executor(InMemoryGitHub(prs={100: pr}, issues=(issue,)), store, _Journal())
    assert executor.execute_all(actions)
    after = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    assert after.component.state.self_clears == 0
    assert (after.component.state.machine_block is None) == (cause == "machine")
    assert (after.component.state.budget_block is None) == (cause == "budget")
    if cause == "machine":
        seed_imported_machine_hold(control, replace(machine, blocked_head="newer-head"))
    else:
        seed_imported_budget_hold(control, replace(budget, cap_micros=6_000_000))
    # Minting newer evidence changes the selected revision as runtime does.
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    with control.transaction() as db:
        current = load_issue_aggregate(db, selected.revision.fence.key)
        persist_workflow_snapshot(db, replace(current, revision=replace(current.revision, version=current.revision.version+1)))
    newer = control.read_issue_recovery(selected.revision.fence.key)
    assert not executor.execute_all(actions)
    assert control.read_issue_recovery(selected.revision.fence.key) == newer


def test_effect_receipt_failure_cannot_keep_partial_hold_or_count_when_caught(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from unittest.mock import patch
    from issue_agent.domain.contracts import EffectReceipt, EffectStatus
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_machine_hold
    control, _ = workflow_control(tmp_path, numbers=(7,))
    store = Store(str(tmp_path))
    object.__setattr__(store, "_workflow_db", control)
    block = MachineBlock(7, Target.PR, 100, 100, MachineBlocker.PUBLICATION, "head")
    seed_imported_machine_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    pr = PRView(100, head_ref_oid="head", labels=frozenset({Label.AI_BLOCKED}))
    action = self_clear_actions(WorldState(prs=(pr,)), {7: block}, {}, {}, {}, frozenset(),
                               machine_revisions={7: selected.revision})[0]
    command = command_for_action(action, repository="o/r", executor_identity="o/r:fix")
    control.enqueue_command(command)
    control.begin_command(command.command_id)
    receipt = EffectReceipt(command.command_id, "o/r:fix", control.now(), control.now(),
                            EffectStatus.SUCCEEDED, remote_revision="head")
    with control.transaction() as db:
        db.execute("CREATE TRIGGER reject_clear_receipt BEFORE INSERT ON effect_receipts "
                   "WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT, 'receipt rejected'); END")
    with control.transaction() as db:
        @contextmanager
        def enclosing_transaction():
            yield db
        with patch.object(control, "transaction", enclosing_transaction):
            with pytest.raises(sqlite3.IntegrityError, match="receipt rejected"):
                control.record_effect(receipt)
            assert control.read_issue_recovery(selected.revision.fence.key) == selected
            assert control.effect_receipt(command.command_id) is None
    assert control.read_issue_recovery(selected.revision.fence.key) == selected
    with control.transaction() as db:
        db.execute("DROP TRIGGER reject_clear_receipt")
    assert control.record_effect(receipt)
    assert control.record_effect(receipt)
    after = _recovery_state(store, 7)
    assert after.machine_block is None and after.self_clears == 1


@pytest.mark.parametrize("initial_count", [0, 2])
@pytest.mark.parametrize("same_target", [False, True])
@pytest.mark.parametrize("budget_available", [False, True])
def test_sibling_recovery_uses_a_fresh_revision_and_shared_count_before_second_relabel(tmp_path, initial_count, same_target, budget_available):
    import asyncio
    from issue_agent.domain.outcome import AgentResult
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    from tests.app.test_pr_repair_workflow import active_ci_workflow
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    workflow, store, _, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "publication"}, cost_usd=.01), Store(str(tmp_path)))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="published-head")
    asyncio.run(workflow.fix_ci(100))
    seed_imported_budget_hold(store._workflow_db, BudgetBlock(5, Target.PR if same_target else Target.ISSUE,
        100 if same_target else 5, 2_000_000, "issue:5", 5_000_000, 100_000))
    _seed_clear_count(store, 5, initial_count)
    pr = PRView(100, head_ref_oid="published-head", labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}), closing_issue=5)
    if same_target:
        pr = replace(pr, labels=pr.labels | {Label.NEEDS_BUDGET_REVIEW})
    issue = IssueView(5, labels=frozenset({Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}))
    gateway = InMemoryGitHub(prs={100: pr}, issues=(issue,))
    journal = _Journal()
    executor = _executor(gateway, store, journal)
    key = WorkflowKey("o/r", IssueRef(5))
    def decide():
        selected = store._workflow_db.read_issue_recovery(key)
        state = selected.component.state
        world = WorldState(prs=(gateway.prs[100],), issues=(gateway.issues[5],))
        budgets = {}
        if state.budget_block is not None:
            budget = _observed_budget(store, world, state.budget_block)
            if not budget_available:
                budget = replace(budget, complete=False)
            budgets[5] = budget
        return self_clear_actions(
            world,
            {5: state.machine_block} if state.machine_block is not None else {},
            {5: state.budget_block} if state.budget_block is not None else {},
            budgets,
            {5: _recovery_state(store, 5).self_clears}, frozenset(),
            machine_revisions={5: selected.revision}, budget_revisions={5: selected.revision})
    assert executor.execute_all(decide())
    assert _recovery_state(store, 5).self_clears == initial_count + 1
    after = store._workflow_db.read_issue_recovery(key).component.state
    assert after.machine_block is None and after.budget_block is not None
    assert gateway.issues[5].labels == issue.labels
    if same_target and not budget_available:
        from issue_agent.app.roles import _fix_state
        from issue_agent.domain.fix import FixConfig, decide_fix
        from issue_agent.domain.actions import RunAgent
        from issue_agent.domain.world import Check
        paid_pr = replace(gateway.prs[100], author="fix-bot", head_ref_name="fix/5-test",
                          checks=(Check("unit", bucket="fail"),))
        paid_world = WorldState(prs=(paid_pr,), issues=(replace(issue, labels=frozenset({Label.AI_PR_OPEN})),))
        state = _fix_state(store, paid_world, workflow_control=workflow.workflow_control)
        cfg = FixConfig(fix_bot="fix-bot", ci_free_rerun=False)
        actions = decide_fix(paid_world, cfg, state, fixer_busy=False)
        assert not any(isinstance(action, RunAgent) for action in actions)
        permitted = replace(paid_pr, labels=frozenset({Label.AI_PR_OPEN}))
        permitted_world = replace(paid_world, prs=(permitted,))
        permitted_state = replace(state, budget_blocks={}, budget_revisions={})
        permitted_actions = decide_fix(permitted_world, cfg, permitted_state, fixer_busy=False)
        assert any(isinstance(action, RunAgent) for action in permitted_actions), (
            state.control_errors, state.machine_control_errors, state.budget_control_errors,
            state.infra_control_errors, permitted_actions,
        )
    assert executor.execute_all(decide())
    after = store._workflow_db.read_issue_recovery(key).component.state
    if not budget_available:
        assert after.budget_block is not None and _recovery_state(store, 5).self_clears == initial_count + 1
        assert gateway.issues[5].labels == issue.labels
        if same_target:
            assert Label.NEEDS_BUDGET_REVIEW in gateway.prs[100].labels
    elif initial_count == 0:
        assert after.budget_block is None and _recovery_state(store, 5).self_clears == 2
        if same_target:
            assert gateway.prs[100].labels == frozenset({Label.AI_PR_OPEN})
        else:
            assert gateway.issues[5].labels == frozenset({Label.AI_READY})
    else:
        assert after.budget_block is not None and _recovery_state(store, 5).self_clears == 3
        assert gateway.issues[5].labels == issue.labels
        assert _recovery_state(store, 5).self_clear_ceiling_reported


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
    assert _recovery_state(store, 5).self_clears == 1
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
    store = Store(str(tmp_path))
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_budget_hold
    from issue_agent.domain.workflow_identity import WorkflowKey, IssueRef
    control, _ = workflow_control(tmp_path, numbers=(7,))
    object.__setattr__(store, "_workflow_db", control)
    seed_imported_budget_hold(control, block)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    from issue_agent.adapters.control_store import ReservationRequest
    from issue_agent.domain.contracts import PriceQuote, ReservationId, RunId
    budget = _observed_budget(store, WorldState(issues=(issue,)), block)
    scope = budget.limits[0].budget_scope
    assert scope is not None
    reserved = ReservationRequest(ReservationId("existing-reservation"), RunId("existing-run"), "existing-dispatch",
        scope, PriceQuote("model", "card", "1", control.now(), Money("USD", 3_000_000)),
        Money("USD", 5_000_000), "config", "o/r", owner_id="o/r:fix:fixture")
    assert control.reserve(reserved).admitted
    budget = _observed_budget(store, WorldState(issues=(issue,)), block)
    assert budget.limits[0].committed_micros == 3_000_000
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
        WorldState(issues=(issue,)), {}, {7: block}, {7: budget}, {7: 0}, frozenset(),
        budget_revisions={7: selected.revision}
    )

    assert len(actions) == 1
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
    assert control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7))).component.state.budget_block is None
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
        config_version="config",
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
        config_version="config",
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


def test_ceiling_event_is_persisted_once(tmp_path):
    block = MachineBlock(
        7, Target.PR, 100, 100, MachineBlocker.PUBLICATION, "head"
    )
    pr = PRView(100, head_ref_oid="head", labels=frozenset({Label.AI_BLOCKED}))
    store = Store(str(tmp_path))
    from tests.app.test_control_record_isolation import workflow_control
    from tests.fakes.accounted_workflow import seed_imported_machine_hold
    control, _ = workflow_control(tmp_path, numbers=(7,))
    object.__setattr__(store, "_workflow_db", control)
    seed_imported_machine_hold(control, block)
    _seed_clear_count(store, 7, 3)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    journal = _Journal()
    executor = _executor(InMemoryGitHub(prs={100: pr}), store, journal)
    action = self_clear_actions(
        WorldState(prs=(pr,)), {7: block}, {}, {}, {7: 3}, frozenset(),
        machine_revisions={7: selected.revision},
    )[0]

    executor.execute(action)
    executor.execute(action)

    assert _recovery_state(store, 7).self_clear_ceiling_reported
    assert sum(
        isinstance(event.payload, SelfClearCeilingReached)
        for event in journal.events
    ) == 1
