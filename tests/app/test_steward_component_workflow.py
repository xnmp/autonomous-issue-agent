"""Public triage uses paid component admission and exact durable delivery."""

import asyncio
import pytest
from dataclasses import replace
from datetime import datetime, timedelta

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import GroomTaskConfig, TriageTaskConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import Money
from issue_agent.domain.labels import Label
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable, UnknownModelPolicy
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import WorkflowFamily
from issue_agent.domain.world import IssueView
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import workflow_scope
from tests.adapters.test_continuation_admission import active, install_secondary
from tests.adapters.test_steward_delivery import bind_events, OWNER
from tests.adapters.test_workflow_admission import NOW
from tests.app.test_steward_stall_ceiling import BUDGET, INFRA, VERDICT, _Runner
from tests.app.test_steward_workflows import _Merged
from tests.fakes.in_memory_github import InMemoryGitHub
from tests.fakes.workflow_authority import resolve_snapshot
from test_dispatch import FakeStore


class DeniedWrites(InMemoryGitHub):
    denied = True

    def comment(self, kind, number, body):
        if not self.denied:
            super().comment(kind, number, body)
            current = self.issues[number]
            self.issues[number] = replace(current, comments=current.comments[:-1] +
                                         (replace(current.comments[-1], author="steward-bot"),))

    def relabel(self, kind, number, remove, add):
        if not self.denied:
            super().relabel(kind, number, remove, add)


def composed(tmp_path, gateway, inner, *, install=True, groom=False):
    control = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    control.initialize()
    bind_events(control)
    revision = resolve_snapshot(7).revision
    if install:
        component = active(revision.fence.key, WorkflowFamily.STEWARD)
        shell = IssueWorkflowShell(revision, steward=component)
        with control.transaction() as db:
            db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                       (*workflow_scope(revision.fence.key), revision.fence.generation, revision.version, encode_workflow(shell)))
        install_secondary(control, shell, component)
    task = TriageTaskConfig("steward-bot", triage_prompt="T")
    runner = AccountingRuntime(inner, control, "o/r", "steward", None, None, None, "fixture", "claude",
        BudgetPolicy({("steward", "TRIAGE"): Money.from_major("USD", task.max_budget_usd),
                      ("steward", "GROOM"): Money.from_major("USD", 0.75)}),
        RateTable("fixture", "1", "USD", {}, UnknownModelPolicy.USE_FALLBACK, ModelRates(1,1,1,1)),
        owner_id=OWNER)
    effects = GitHubEffectExecutor(gateway, control, repository="o/r", identity=OWNER)
    executor = Executor(gateway, remove_worktree=lambda n: None, effect_executor=effects, retry_delays=())
    files = FakeStore()
    workflow = StewardWorkflow(StewardWorkflowConfig(task,
        TriageConfig("steward-bot", enabled=True, promote=True), lambda message: None,
        dedupe=control, repo="o/r", owner_id=OWNER,
        groom=GroomTaskConfig("steward-bot") if groom else None), gateway, files, runner, executor)
    return workflow, control, files, revision.fence.key


def issue():
    return IssueView(7, title="Export data", body="CSV export", author="alice",
                     updated_at=NOW.isoformat(), labels=frozenset({Label.AI_TRIAGE}))


def test_queued_triage_does_not_claim_delivered_baseline(tmp_path):
    gateway = DeniedWrites((issue(),))
    inner = _Runner(replace(VERDICT, cost_usd=0.01))
    workflow, control, files, key = composed(tmp_path, gateway, inner)
    asyncio.run(workflow.triage(7))
    assert control.pending_commands()
    assert control.read_steward(key).component.state.triage.baseline_at == ""
    assert files.triage_at == {}
    asyncio.run(workflow.triage(7))
    assert inner.runs == 1
    gateway.denied = False
    asyncio.run(workflow.triage(7))
    assert Label.AI_READY in gateway.get_issue(7).labels
    assert control.read_steward(key).component.state.triage.baseline_at == NOW.isoformat()
    asyncio.run(workflow.triage(7))
    assert inner.runs == 1
    assert files.triage_at == {}


def test_inactive_steward_refuses_before_paid_runtime(tmp_path):
    inner = _Runner(VERDICT)
    workflow, control, files, key = composed(tmp_path, InMemoryGitHub((issue(),)), inner, install=False)
    asyncio.run(workflow.triage(7))
    assert inner.runs == 0 and not control.pending_commands()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert files.triage_at == {}


def test_inactive_groom_refuses_before_paid_runtime(tmp_path):
    inner = _Runner(replace(VERDICT, structured={}, cost_usd=0.01))
    workflow, control, _, _ = composed(tmp_path, InMemoryGitHub((issue(),)), inner,
                                      install=False, groom=True)
    workflow.runner.set_context("steward", "GROOM", 7, 7)
    asyncio.run(workflow.groom(7))
    assert inner.runs == 0 and not control.pending_commands()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_no_finding_groom_records_success_once_without_body_mutation(tmp_path):
    gateway = InMemoryGitHub((issue(),))
    inner = _Runner(replace(VERDICT, structured={}, cost_usd=0.01))
    workflow, control, files, key = composed(tmp_path, gateway, inner, groom=True)
    workflow.runner.set_context("steward", "GROOM", 7, 7)
    original_triage = control.read_steward(key).component.state.triage
    asyncio.run(workflow.groom(7))
    asyncio.run(workflow.groom(7))
    selected = control.read_steward(key).component
    assert inner.runs == 1 and selected.state.groom.baseline_at == issue().updated_at
    assert selected.state.triage == original_triage
    assert gateway.get_issue(7).body == issue().body and gateway.calls == []
    assert files.groom_at == {} and files.groom_content == {}


class AdvisoryGateway(DeniedWrites):
    def recently_merged_issues(self, author, limit=30):
        return (_Merged(42, 300),)


def test_owed_groom_advisory_recovers_before_sibling_purchase(tmp_path):
    gateway = AdvisoryGateway((issue(),))
    inner = _Runner(replace(VERDICT, structured={"resolved_by": 42}, cost_usd=0.01))
    workflow, control, files, key = composed(tmp_path, gateway, inner, groom=True)
    workflow.cfg = replace(workflow.cfg, fix_bot="fix-bot")
    asyncio.run(workflow.groom(7))
    owed = control.read_steward(key).component.state
    assert owed.groom.pending_delivery and owed.groom.baseline_at == ""
    assert owed.groom_content.successful == ""
    asyncio.run(workflow.groom(7))
    asyncio.run(workflow.triage(7))
    assert inner.runs == 1 and control.read_steward(key).component.state.triage == owed.triage
    gateway.denied = False
    asyncio.run(workflow.groom(7))
    delivered = control.read_steward(key).component.state
    assert not delivered.groom.pending_delivery and delivered.groom_content.successful
    assert delivered.triage == owed.triage
    assert Label.MAYBE_RESOLVED in gateway.get_issue(7).labels
    assert len(gateway.comments[("issue", 7)]) == 1 and gateway.get_issue(7).body == issue().body
    asyncio.run(workflow.groom(7))
    assert inner.runs == 1 and files.groom_at == {} and files.groom_content == {}
    inner.result = replace(VERDICT, cost_usd=0.01)
    asyncio.run(workflow.triage(7))
    assert inner.runs == 2
    assert control.read_steward(key).component.state.groom == delivered.groom
    assert control.read_steward(key).component.state.groom_content == delivered.groom_content


def test_owed_triage_delivery_reenables_groom_without_resetting_it(tmp_path):
    gateway = DeniedWrites((issue(),))
    inner = _Runner(replace(VERDICT, cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, inner, groom=True)
    asyncio.run(workflow.triage(7))
    before = control.read_steward(key).component.state
    asyncio.run(workflow.groom(7))
    assert inner.runs == 1 and control.read_steward(key).component.state.groom == before.groom
    gateway.denied = False
    asyncio.run(workflow.triage(7))
    after = control.read_steward(key).component.state
    assert not after.triage.pending_delivery and after.groom == before.groom
    assert after.groom_content == before.groom_content
    inner.result = replace(VERDICT, structured={}, cost_usd=0.01)
    asyncio.run(workflow.groom(7))
    assert inner.runs == 2 and control.read_steward(key).component.state.triage == after.triage


def test_stopped_notice_retains_ceiling_without_claiming_success(tmp_path):
    gateway = DeniedWrites((issue(),))
    gateway.denied = False
    inner = _Runner(replace(BUDGET, cost_usd=0.01))
    workflow, control, files, key = composed(tmp_path, gateway, inner)
    workflow.cfg = replace(workflow.cfg, error_ceiling=2)
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    lane = control.read_steward(key).component.state.triage
    assert lane.stalls == 2 and lane.baseline_at == "" and lane.pending_delivery == ()
    assert Label.NEEDS_BUDGET_REVIEW in gateway.get_issue(7).labels
    assert len(gateway.comments[("issue", 7)]) == 1
    asyncio.run(workflow.triage(7))
    assert inner.runs == 2 and files.triage_at == {}
    gateway.issues[7] = replace(gateway.get_issue(7), updated_at=(NOW + timedelta(minutes=1)).isoformat(),
                               body="Smaller scope", labels=frozenset({Label.AI_TRIAGE}))
    inner.result = replace(VERDICT, cost_usd=0.01)
    asyncio.run(workflow.triage(7))
    assert inner.runs == 3
    assert control.read_steward(key).component.state.triage.stalls == 0


def test_infrastructure_failure_keeps_triage_stall_allowance(tmp_path):
    inner = _Runner(replace(INFRA, cost_usd=0.01))
    workflow, control, files, key = composed(tmp_path, InMemoryGitHub((issue(),)), inner)
    asyncio.run(workflow.triage(7))
    state = control.read_steward(key).component.state
    lane = state.triage
    assert lane.stalls == 0 and lane.baseline_at == ""
    assert lane.infra_failures == 1
    assert state.groom == cc.StewardLaneState()
    assert files.triage_at == {} and not control.pending_commands()
    reopened = SQLiteControlStore(control.path, now=lambda: NOW)
    reopened.initialize()
    assert reopened.read_steward(key).component.state == state
    with reopened.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1


@pytest.mark.parametrize("deterministic", [False, True])
def test_launched_steward_exception_charges_sql_without_releasing_unknown_money(tmp_path, monkeypatch, deterministic):
    from issue_agent.domain.contracts import RunId
    inner = _Runner(VERDICT)
    workflow, control, _, key = composed(tmp_path, InMemoryGitHub((issue(),)), inner)
    workflow.cfg = replace(workflow.cfg, infra_ceiling=3)
    run_ids = []
    async def die(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        run_ids.append(run_id)
        control.register_process_lease(run_id, expected_owner_id=OWNER, pid=123, pgid=123, process_start_ticks=99)
        raise RuntimeError("exceeded maximum buffer size" if deterministic else "transport disappeared")
    monkeypatch.setattr(inner, "run", die)
    with pytest.raises(RuntimeError):
        asyncio.run(workflow.triage(7))
    state = control.read_steward(key)
    assert state.component.state.triage.infra_failures == (3 if deterministic else 1)
    assert state.component.state.triage.stalls == 0 and state.component.state.groom == cc.StewardLaneState()
    assert control.active_reservation_runs(OWNER) == tuple(run_ids)
    assert control.pending_commands() == ()
    assert control.abandoned_dispatch_counts(OWNER) == {}
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT state,outcome FROM runs").fetchone()[:] == ("completed", "spend_unreconciled")


def test_never_launched_steward_exception_does_not_charge_or_strand_input(tmp_path, monkeypatch):
    inner = _Runner(VERDICT)
    workflow, control, _, key = composed(tmp_path, InMemoryGitHub((issue(),)), inner)
    async def refuse(task):
        raise RuntimeError("provider refused before spawning")
    monkeypatch.setattr(inner, "run", refuse)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(workflow.triage(7))
    state = control.read_steward(key)
    assert state.component.state.triage.infra_failures == state.component.state.triage.stalls == 0
    assert state.component.watermarks.last_applied == state.component.watermarks.last_admitted
    assert control.active_reservation_runs(OWNER) == () and control.pending_commands() == ()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


@pytest.mark.parametrize("groom", [False, True])
def test_lane_infra_ceiling_and_new_human_reset_preserve_sibling(tmp_path, groom):
    gateway = InMemoryGitHub((issue(),))
    inner = _Runner(replace(INFRA, cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, inner, groom=groom)
    workflow.cfg = replace(workflow.cfg, infra_ceiling=2)
    run = workflow.groom if groom else workflow.triage
    for _ in range(3):
        asyncio.run(run(7))
    assert inner.runs == 2
    before = control.read_steward(key).component.state
    lane = before.groom if groom else before.triage
    sibling = before.triage if groom else before.groom
    assert lane.infra_failures == 2 and lane.stalls == 0
    gateway.issues[7] = replace(gateway.get_issue(7), body="New human scope",
        updated_at=(NOW + timedelta(minutes=1)).isoformat())
    asyncio.run(run(7))
    assert inner.runs == 3
    after = control.read_steward(key).component.state
    assert (after.groom if groom else after.triage).infra_failures == 1
    assert (after.triage if groom else after.groom) == sibling


@pytest.mark.parametrize("groom", [False, True])
@pytest.mark.parametrize("remote_state", ["closed", "absent"])
def test_terminal_issue_during_steward_return_has_no_lifecycle_mutation(tmp_path, monkeypatch, groom, remote_state):
    gateway = InMemoryGitHub((issue(),))
    inner = _Runner(replace(VERDICT, cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, inner, groom=groom)
    async def returned_after_close(task):
        inner.runs += 1
        if remote_state == "absent":
            del gateway.issues[7]
        else:
            gateway.issues[7] = replace(gateway.get_issue(7), state="closed")
        return replace(VERDICT, cost_usd=0.01)
    monkeypatch.setattr(inner, "run", returned_after_close)
    if remote_state == "absent":
        with pytest.raises(KeyError):
            asyncio.run((workflow.groom if groom else workflow.triage)(7))
    else:
        asyncio.run((workflow.groom if groom else workflow.triage)(7))
    assert inner.runs == 1
    assert gateway.calls == [] and control.pending_commands() == ()
    assert (7 not in gateway.issues if remote_state == "absent" else gateway.get_issue(7).state == "closed")


def test_first_failure_of_new_human_input_has_a_fresh_stall_allowance(tmp_path):
    gateway = DeniedWrites((issue(),))
    gateway.denied = False
    inner = _Runner(replace(BUDGET, cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, inner)
    workflow.cfg = replace(workflow.cfg, error_ceiling=2)
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    assert len(gateway.comments[("issue",7)]) == 1
    gateway.issues[7] = replace(gateway.get_issue(7), body="new scope", updated_at=(NOW + timedelta(minutes=1)).isoformat(),
                               labels=frozenset({Label.AI_TRIAGE}))
    asyncio.run(workflow.triage(7))
    assert inner.runs == 3 and control.read_steward(key).component.state.triage.stalls == 1
    assert len(gateway.comments[("issue",7)]) == 1
    assert Label.NEEDS_BUDGET_REVIEW not in gateway.get_issue(7).labels


class PartialDelivery(InMemoryGitHub):
    def comment(self, kind, number, body):
        super().comment(kind, number, body)
        v=self.issues[number]
        self.issues[number]=replace(v, updated_at=(datetime.fromisoformat(v.updated_at) + timedelta(seconds=1)).isoformat(), comments=v.comments[:-1]+(replace(v.comments[-1],author='steward-bot'),))
    def relabel(self,kind,number,remove,add):
        pass

def test_own_comment_does_not_reset_failed_verdict_allowance(tmp_path):
    gateway=PartialDelivery((issue(),))
    runner=_Runner(replace(VERDICT,cost_usd=0.01))
    workflow,control,_,key=composed(tmp_path,gateway,runner)
    workflow.cfg=replace(workflow.cfg,error_ceiling=2)
    workflow.executor._effect_executor.retry_ceiling=1
    states=[]
    assert isinstance(VERDICT.structured, dict)
    for _ in range(12):
        runner.result=replace(VERDICT,cost_usd=0.01,structured={**VERDICT.structured,"plain_summary":f"clear {runner.runs}"})
        asyncio.run(workflow.triage(7))
        lane=control.read_steward(key).component.state.triage
        states.append((runner.runs,lane.stalls,lane.attempted_at,len(lane.pending_delivery),gateway.get_issue(7).updated_at))
    assert runner.runs == 2, states
    lane = control.read_steward(key).component.state.triage
    assert lane.stalls == 2 and lane.baseline_at == "" and not lane.pending_delivery
    gateway.issues[7] = replace(gateway.get_issue(7), body="Human narrowed the scope",
        updated_at=(NOW + timedelta(minutes=1)).isoformat())
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    assert runner.runs == 3
    assert control.read_steward(key).component.state.triage.stalls == 1


def test_identical_verdict_after_new_human_input_settles(tmp_path):
    gateway=InMemoryGitHub((issue(),))
    gateway.comment_author='steward-bot'
    runner=_Runner(replace(VERDICT,cost_usd=0.01))
    workflow,control,_,key=composed(tmp_path,gateway,runner)
    asyncio.run(workflow.triage(7))
    assert not control.pending_commands()
    gateway.issues[7]=replace(gateway.get_issue(7),body='New human clarification',updated_at=(NOW+timedelta(minutes=1)).isoformat(),labels=frozenset({Label.AI_TRIAGE}))
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    lane=control.read_steward(key).component.state.triage
    assert not lane.pending_delivery, [(p.command.kind.value,p.state) for p in control.pending_commands()]
    assert lane.baseline_at==gateway.get_issue(7).updated_at


class DeniedClose(InMemoryGitHub):
    denied = True

    def close_issue(self, number, reason, comment):
        if not self.denied:
            super().close_issue(number, reason, comment)


def test_existing_close_comment_does_not_strand_new_input(tmp_path):
    from tests.app.test_steward_workflows import _close_verdict
    gateway = DeniedClose((issue(),))
    gateway.comment_author = "steward-bot"
    runner = _Runner(replace(_close_verdict(), cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, runner)
    workflow.executor._effect_executor.retry_ceiling = 1
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    assert not control.read_steward(key).component.state.triage.pending_delivery
    gateway.denied = False
    gateway.issues[7] = replace(gateway.get_issue(7), body="New human clarification",
        updated_at=(NOW + timedelta(minutes=1)).isoformat(), labels=frozenset({Label.AI_TRIAGE}))
    asyncio.run(workflow.triage(7))
    asyncio.run(workflow.triage(7))
    assert gateway.get_issue(7).state == "closed"
    assert not control.read_steward(key).component.state.triage.pending_delivery
    assert runner.runs == 2


class PartialClose(DeniedClose):
    def comment(self, kind, number, body):
        super().comment(kind, number, body)
        current = self.issues[number]
        self.issues[number] = replace(current,
            updated_at=(datetime.fromisoformat(current.updated_at) + timedelta(seconds=1)).isoformat())


def test_own_partial_close_does_not_reset_allowance(tmp_path):
    from tests.app.test_steward_workflows import _close_verdict
    gateway = PartialClose((issue(),))
    gateway.comment_author = "steward-bot"
    runner = _Runner(replace(_close_verdict(), cost_usd=0.01))
    workflow, control, _, key = composed(tmp_path, gateway, runner)
    workflow.cfg = replace(workflow.cfg, error_ceiling=2)
    workflow.executor._effect_executor.retry_ceiling = 1
    for _ in range(12):
        runner.result = replace(_close_verdict(close_reason=f"Fixed already {runner.runs}"), cost_usd=0.01)
        asyncio.run(workflow.triage(7))
    lane = control.read_steward(key).component.state.triage
    assert runner.runs == 2 and lane.stalls == 2 and lane.baseline_at == ""
    assert not lane.pending_delivery and gateway.get_issue(7).state == "open"
