"""PR repair uses selected SQL continuity before buying a CI leg."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import pytest
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError

from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Check, PRView, Review
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey
from tests.app.test_dispatch import FakeGateway, FakeRunner, FakeStore
from tests.app.test_fix_workflow import _workflow
from tests.fakes.review_workflow import held
from tests.fakes.workflow_authority import install_workflow


def test_fix_ci_without_active_pr_component_refuses_without_purchase():
    gateway = FakeGateway(prs={100:PRView(100, head_ref_name="fix/5-remote", closing_issue=5,
        checks=(Check("tests", "fail"),))})
    inner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="unselected"))
    files = FakeStore()
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    install_workflow(files._workflow_db, held(key=WorkflowKey("o/r", PullRequestRef(100))))
    asyncio.run(workflow.fix_ci(100))
    assert inner.tasks == []
    assert files.sessions == {} and files.branches == {}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_address_review_without_active_pr_component_refuses_without_purchase():
    gateway = FakeGateway(prs={100: PRView(
        100, head_ref_name="fix/5-remote", closing_issue=5, head_ref_oid="a" * 40,
        reviews=(Review("CHANGES_REQUESTED", "reviewer", "2026-10-02T00:00:01Z", body="fix it"),),
    )})
    inner = FakeRunner(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="unselected"))
    files = FakeStore()
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    install_workflow(files._workflow_db, held(key=WorkflowKey("o/r", PullRequestRef(100))))

    asyncio.run(workflow.address_review(100))

    assert inner.tasks == []
    assert files.sessions == {} and files.branches == {}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_rebase_without_active_pr_component_refuses_without_purchase(tmp_path):
    from issue_agent.adapters.store import Store
    gateway = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-remote", closing_issue=5,
        head_ref_oid="a" * 40)})
    inner = FakeRunner(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="unselected"))
    files = Store(str(tmp_path / "state"))
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    install_workflow(files._workflow_db, held(key=WorkflowKey("o/r", PullRequestRef(100))))

    asyncio.run(workflow.rebase(100))

    assert inner.tasks == []
    assert files.get_session(5) == "" and files.get_branch(5) == ""
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def active_ci_workflow(result, files=None):
    from dataclasses import replace
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from tests.adapters.test_continuation_admission import active, install_secondary

    gateway = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-remote", closing_issue=5,
        checks=(Check("tests", "fail"),))})
    inner = FakeRunner(result)
    files = files if files is not None else FakeStore()
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    key = WorkflowKey("o/r", PullRequestRef(100))
    base = replace(held(key=key), gates=())
    install_workflow(files._workflow_db, base)
    component = active(key, WorkflowFamily.PR_REPAIR)
    install_secondary(files._workflow_db, replace(base, pr_repair=component), component)
    return workflow, files, inner, key


def issue_ci_state(files, issue=5):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate
    with files._workflow_db.transaction() as db:
        aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(issue)))
        return cc.select_issue_recovery(aggregate).component.state


def test_machine_block_return_survives_sql_reopen_and_exact_result_retry(tmp_path):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.adapters.store import Store
    from issue_agent.domain.self_clear import MachineBlocker, BudgetBlock
    from issue_agent.domain.actions import Target
    files = Store(str(tmp_path / "state"))
    budget = BudgetBlock(5, Target.PR, 100, 10000, "issue", 100000)
    files.set_budget_block(budget)
    workflow, files, inner, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "checks"}, cost_usd=.01), files)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.fix_ci(100))
    reopened = SQLiteControlStore(files._workflow_db.path)
    key = WorkflowKey("o/r", IssueRef(5))
    block = reopened.read_issue_recovery(key).component.state.machine_block
    assert block is not None and block.blocker is MachineBlocker.CHECKS
    assert block.blocked_head == "a" * 40 and block.pr == 100
    assert files.get_budget_block(5) == budget
    with reopened.transaction() as db:
        returned = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    reopened.settle_pr_repair_result(returned, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"))
    assert reopened.read_issue_recovery(key).component.state.machine_block == block
    assert len(inner.tasks) == 1
    with reopened.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_fix_infra_return_and_exact_recovery_count_one_issue_death():
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=True, status="timeout", exit_reason=ExitReason.TRANSPORT_DEATH, cost_usd=.01))

    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    recorded = decode_workflow(raw, cc.PRRepairResult)
    files._workflow_db.settle_pr_repair_result(recorded, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"))

    state = issue_ci_state(files)
    assert state.infra_failures == 1
    assert state.ci_failures == 0 and state.pending_ci is None
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_fix_noninfra_error_return_resets_sql_episode_after_reopen_without_erasing_ci_debt():
    from issue_agent.domain.outcome import ExitReason
    from issue_agent.adapters.control_store import SQLiteControlStore
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=True, status="timeout", exit_reason=ExitReason.TRANSPORT_DEATH, cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).infra_failures == 1
    with files._workflow_db.transaction() as db:
        original = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    inner.result = AgentResult(is_error=True, status="error", exit_reason=ExitReason.AGENT_ERROR, cost_usd=.01)
    asyncio.run(workflow.fix_ci(100))

    reopened = SQLiteControlStore(files._workflow_db.path)
    selected = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    assert selected.component.state.infra_failures == 0 and selected.component.state.ci_failures == 1
    before = selected.component
    reopened.settle_pr_repair_result(original, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"))
    assert reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component == before
    with reopened.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (2, 20000)



@pytest.mark.parametrize("recovery_first", [False, True])
def test_fix_launched_death_and_reclaim_charge_once_keep_unknown_spend(monkeypatch, recovery_first):
    from issue_agent.domain.contracts import RunId
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    observed = []
    async def lost_reply(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        observed.append(run_id)
        store.register_process_lease(run_id, expected_owner_id="o/r:fix:fixture",
            pid=123, pgid=123, process_start_ticks=99)
        if recovery_first:
            assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == (run_id,)
        raise RuntimeError("stream closed after paid launch")
    monkeypatch.setattr(inner, "run", lost_reply)
    with pytest.raises(RuntimeError, match="stream closed after paid launch"):
        asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).infra_failures == 1
    if not recovery_first:
        assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == ()
        # Exception retention already completed this run and removed its lease.
    assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == ()
    from issue_agent.adapters.control_store import SQLiteControlStore
    reopened = SQLiteControlStore(store.path)
    assert reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state.infra_failures == 1
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0] == 1


@pytest.mark.parametrize("normal", [False, True])
def test_fix_reclaimed_run_actual_reply_classifies_once(monkeypatch, normal):
    from issue_agent.domain.contracts import RunId
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=True, cost_usd=.01,
        exit_reason=ExitReason.AGENT_ERROR if normal else ExitReason.TRANSPORT_DEATH))
    store = files._workflow_db
    original = inner.run
    async def reclaimed_reply(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        store.register_process_lease(run_id, expected_owner_id="o/r:fix:fixture",
            pid=123, pgid=123, process_start_ticks=99)
        assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == (run_id,)
        assert issue_ci_state(files).infra_failures == 1
        return await original(task)
    monkeypatch.setattr(inner, "run", reclaimed_reply)
    asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).infra_failures == (0 if normal else 1)
    assert issue_ci_state(files).ci_failures == (1 if normal else 0)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)
        receipt = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0], cc.FixInfraReceipt)
    assert receipt.outcome is (cc.FixInfraOutcome.RETURNED_NORMAL if normal else cc.FixInfraOutcome.RETURNED_INFRA)
    store.record_fix_infra_return(receipt.admission.attempt.run_id, receipt.outcome, files._workflow_authority.writer)
    assert issue_ci_state(files).infra_failures == (0 if normal else 1)


@pytest.mark.parametrize("namespace", ["fix-infra-outcome", "continuation-admission"])
def test_fix_missing_selected_infra_source_refuses_read_and_new_purchase(namespace):
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=True, cost_usd=.01,
        exit_reason=ExitReason.TRANSPORT_DEATH))
    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        if namespace == "continuation-admission":
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key LIKE '%issue_recovery%'", (namespace,))
        else:
            db.execute("DELETE FROM dedupe_keys WHERE namespace=?", (namespace,))
    with pytest.raises(WorkflowAuthorityError, match="episode|admission|continuation"):
        files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    with pytest.raises(WorkflowAuthorityError, match="episode|admission|continuation"):
        asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_caught_fix_reclaim_receipt_failure_keeps_joint_mutation_retryable(monkeypatch):
    import sqlite3
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    original = store.reserve
    captured = []
    def interrupted(request):
        assert original(request).admitted
        captured.append(request)
        raise RuntimeError("lost after admission before launch")
    with monkeypatch.context() as patch:
        patch.setattr(store, "reserve", interrupted)
        with pytest.raises(RuntimeError, match="lost after admission"):
            asyncio.run(workflow.fix_ci(100))
    request = captured[0]
    store.register_process_lease(request.run_id, expected_owner_id=request.owner_id,
        pid=123, pgid=123, process_start_ticks=99)
    before = issue_ci_state(files)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_infra BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='fix-infra-outcome' BEGIN SELECT RAISE(ABORT,'refused infra'); END")
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(sqlite3.IntegrityError, match="refused infra"):
                store.reclaim_dead_leases(request.owner_id, lambda *_: False)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('scratch','o/r','outer','0','{}')")
    assert issue_ci_state(files) == before
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1
        assert db.execute("SELECT state FROM runs").fetchone()[0] == "running"
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='scratch'").fetchone()[0] == 1
        db.execute("DROP TRIGGER refuse_infra")
    assert store.reclaim_dead_leases(request.owner_id, lambda *_: False) == (request.run_id,)
    assert store.reclaim_dead_leases(request.owner_id, lambda *_: False) == ()
    assert issue_ci_state(files).infra_failures == 1

def pr_reply_workflow(*, establish_session=True, body="Use A."):
    from issue_agent.domain.world import Comment, IssueView
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="selected-pr-session"))
    workflow.gateway.issues[5] = IssueView(5, title="Choose an option")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    if establish_session:
        asyncio.run(workflow.fix_ci(100))
        # Retained files are deliberately present; the selected SQL receipt owns continuity.
        files.save_session(5, "selected-pr-session")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(
        Comment("alice", body, "2026-10-02T00:00:01Z"),))
    return workflow, files, inner, key


def test_duplicate_pr_reply_purchases_one_resume_and_keeps_issue_marker_independent():
    workflow, files, inner, key = pr_reply_workflow()
    before_issue = issue_ci_state(files)
    asyncio.run(workflow.resume_pr(100))
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 2  # One initial CI leg and one reply-driven resume.
    assert replace(issue_ci_state(files), infra_episode=before_issue.infra_episode) == before_issue
    assert issue_ci_state(files).infra_failures == 0
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.consumed_reply_at == "2026-10-02T00:00:01Z" and state.resumes == 1
    assert files.pr_resumed_at == {}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_nonblank_pr_reply_without_compatible_session_stays_unconsumed():
    workflow, files, inner, key = pr_reply_workflow(establish_session=False)
    before = files._workflow_authority.read_pr_repair(key)
    asyncio.run(workflow.resume_pr(100))
    assert inner.tasks == [] and files._workflow_authority.read_pr_repair(key) == before
    assert files.pr_resumed_at == {}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0


def test_blank_pr_reply_consumes_once_without_session_money_or_issue_reblock():
    workflow, files, inner, key = pr_reply_workflow(establish_session=False, body=" \n ")
    before_issue = issue_ci_state(files)
    asyncio.run(workflow.resume_pr(100))
    selected = files._workflow_authority.read_pr_repair(key)
    assert selected.component.state.consumed_reply_at == "2026-10-02T00:00:01Z"
    asyncio.run(workflow.resume_pr(100))
    assert files._workflow_authority.read_pr_repair(key) == selected
    assert issue_ci_state(files) == before_issue and inner.tasks == [] and workflow.gateway.calls == []
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM lifecycle_commands").fetchone()[0] == 0


@pytest.mark.parametrize("blank", [False, True])
def test_reordered_pr_reply_cannot_overwrite_newer_consumption(blank):
    from issue_agent.domain.world import Comment
    workflow, files, inner, key = pr_reply_workflow()
    asyncio.run(workflow.resume_pr(100))
    latest = files._workflow_authority.read_pr_repair(key)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(
        Comment("alice", " " if blank else "older", "2026-10-01T00:00:01Z"),))
    asyncio.run(workflow.resume_pr(100))
    assert files._workflow_authority.read_pr_repair(key) == latest and len(inner.tasks) == 2


@pytest.mark.parametrize("changed", ["closed", "branch", "runtime"])
def test_changed_pr_or_incompatible_session_defers_unconsumed(changed):
    workflow, files, inner, key = pr_reply_workflow()
    before = files._workflow_authority.read_pr_repair(key)
    if changed == "closed":
        workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], state="CLOSED")
    elif changed == "branch":
        workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_name="fix/5-new")
    else:
        workflow.cfg = replace(workflow.cfg, fixer_runtime="codex")
    asyncio.run(workflow.resume_pr(100))
    assert files._workflow_authority.read_pr_repair(key) == before and len(inner.tasks) == 1


def test_pr_reply_budget_refusal_preserves_session_marker_and_allowance(monkeypatch):
    from issue_agent.adapters.control_store import AccountingDisposition, ReservationResult
    from issue_agent.domain.contracts import Money
    workflow, files, inner, key = pr_reply_workflow()
    before = files._workflow_authority.read_pr_repair(key)
    monkeypatch.setattr(files._workflow_db, "reserve", lambda request: ReservationResult(
        AccountingDisposition.EXHAUSTED, None, Money("USD", 0), request.quote.maximum_charge, Money("USD", 0), "cap"))
    asyncio.run(workflow.resume_pr(100))
    assert files._workflow_authority.read_pr_repair(key) == before and len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_pr_reply_missing_selected_session_receipt_refuses_before_money():
    workflow, files, inner, key = pr_reply_workflow()
    with files._workflow_db.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='continuation-return'")
    with pytest.raises(WorkflowAuthorityError, match="output receipt"):
        asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_same_head_relink_keeps_pr_consumption_and_does_not_grant_issue_allowance():
    from issue_agent.domain.world import Comment, IssueView
    from tests.fakes.workflow_authority import resolve_snapshot, with_issue_recovery
    workflow, files, inner, key = pr_reply_workflow()
    asyncio.run(workflow.resume_pr(100))
    install_workflow(files._workflow_db, with_issue_recovery(resolve_snapshot(6)))
    before_issue = issue_ci_state(files, 6)
    workflow.gateway.issues[6] = IssueView(6)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], closing_issue=6)
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 2 and issue_ci_state(files, 6) == before_issue
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(
        Comment("alice", "new", "2026-10-02T00:00:02Z"),))
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 3
    assert replace(issue_ci_state(files, 6), infra_episode=before_issue.infra_episode) == before_issue
    assert issue_ci_state(files, 6).infra_failures == 0
    assert files._workflow_authority.read_pr_repair(key).component.state.resumes == 2


def test_pr_reply_fresh_process_recovers_captured_return_once(monkeypatch):
    import json
    import subprocess
    import sys
    workflow, files, inner, key = pr_reply_workflow()
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "settle_pr_repair", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("lost after capture")))
        with pytest.raises(RuntimeError, match="lost after capture"):
            asyncio.run(workflow.resume_pr(100))
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    with files._workflow_db.transaction() as db:
        current = load_review_workflow_row(db, key)
        assert current is not None
        retained = replace(current, gates=held(key=key).gates)
        persist_workflow_snapshot(db, retained)
    sibling = (retained.episode, retained.execution, retained.gates, retained.authority)
    child = subprocess.run([sys.executable, "-c", '''
import json,sys
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.workflow_identity import WorkflowWriter
store=SQLiteControlStore(sys.argv[1]);store.initialize()
control=SQLiteWorkflowControl(store,WorkflowWriter("o/r","fix","o/r:fix:fixture"),resolve_ceiling=0)
print(json.dumps([control.reconcile_pr_repair_returns(),control.reconcile_pr_repair_returns()]))
''', files._workflow_db.path], check=False, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == [1, 0]
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 2
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.resumes == 1 and state.branch == "fix/5-remote" and state.session.handle == "selected-pr-session"
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (2, 20000)
        after = load_review_workflow_row(db, key)
        assert after is not None
    assert (after.episode, after.execution, after.gates, after.authority) == sibling


def test_blank_pr_publication_caught_write_failure_rolls_back_selected_source(monkeypatch):
    import issue_agent.adapters.workflow_rows as rows
    workflow, files, _, key = pr_reply_workflow(body=" ")
    store = files._workflow_db
    selected = files._workflow_authority.read_pr_repair(key)
    original = rows.persist_workflow_snapshot
    def fail(db, state):
        original(db, state)
        raise RuntimeError("after blank component write")
    with store.transaction() as db:
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            patch.setattr(rows, "persist_workflow_snapshot", fail)
            with pytest.raises(RuntimeError, match="blank component"):
                files._workflow_authority.consume_blank_pr_reply(selected.binding, "2026-10-02T00:00:01Z", " ")
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('scratch','o/r','outer','0','{}')")
    assert files._workflow_authority.read_pr_repair(key) == selected
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-reply-noop'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='scratch'").fetchone()[0] == 1


@pytest.mark.parametrize("corruption", ["missing", "malformed", "wrong_binding"])
def test_selected_blank_receipt_corruption_refuses_new_paid_reply(corruption):
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.world import Comment
    workflow, files, inner, key = pr_reply_workflow(body=" ")
    asyncio.run(workflow.resume_pr(100))
    with files._workflow_db.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='pr-reply-noop'")
        elif corruption == "malformed":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='pr-reply-noop'")
        else:
            receipt = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-reply-noop'").fetchone()[0], tuple)
            wrong = replace(receipt[0], fence=replace(receipt[0].fence, key=WorkflowKey("o/r", PullRequestRef(101))))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='pr-reply-noop'", (encode_workflow((wrong, *receipt[1:])),))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(Comment("alice", "newer", "2026-10-02T00:00:02Z"),))
    with pytest.raises(ValueError):
        asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_reopened_blank_source_allows_new_paid_reply_and_clears_only_its_reference():
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.domain.world import Comment
    workflow, files, inner, key = pr_reply_workflow(body=" ")
    asyncio.run(workflow.resume_pr(100))
    reopened = SQLiteControlStore(files._workflow_db.path)
    reopened.initialize()
    control = SQLiteWorkflowControl(reopened, files._workflow_authority.writer, resolve_ceiling=0)
    selected = control.read_pr_repair(key)
    assert not control.consume_blank_pr_reply(selected.binding, "2026-10-02T00:00:01Z", " ")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(Comment("alice", "newer", "2026-10-02T00:00:02Z"),))
    asyncio.run(workflow.resume_pr(100))
    current = control.read_pr_repair(key).component.state
    assert current.blank_reply is None and current.resumes == 1 and len(inner.tasks) == 2
    assert current.consumed_reply_at == "2026-10-02T00:00:02Z"


def interrupted_linked_reply(monkeypatch):
    from tests.app.test_issue_reply_admission import setup_reply
    from tests.fakes.accounted_workflow import attach_workflow_authority
    workflow, gateway, files, inner = setup_reply()
    gateway.prs[100] = PRView(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
    gateway._pr_for_issue[5] = 100
    attach_workflow_authority(gateway, files, pr_repair=True)
    inner.result = AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="linked-return")
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "settle_pr_repair", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("lost after capture")))
        with pytest.raises(RuntimeError, match="lost after capture"):
            asyncio.run(workflow.resume(5))
    with files._workflow_db.transaction() as db:
        returned = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    return workflow, files, inner, WorkflowKey("o/r", PullRequestRef(100)), returned


def test_generic_linked_issue_settlement_caught_refusal_keeps_real_recovery_usable(monkeypatch):
    from issue_agent.domain.workflow_identity import WorkflowFamily
    workflow, files, inner, key, returned = interrupted_linked_reply(monkeypatch)
    store = files._workflow_db
    before = store.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    before_pr = store.read_pr_repair(key)
    with store.transaction() as db:
        admission = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND key=?",
            (store._continuation_receipt_key(returned.admission.attempt.run_id, WorkflowFamily.ISSUE_RECOVERY),)).fetchone()[0], tuple)[1]
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(WorkflowAuthorityError, match="paired repair result"):
                store.settle_issue_recovery(returned.settlement, admission, before.component.state)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('scratch','o/r','outer','0','{}')")
    assert store.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))) == before and store.read_pr_repair(key) == before_pr
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    asyncio.run(workflow.resume(5))
    assert len(inner.tasks) == 1 and files._workflow_authority.read_pr_repair(key).component.state.session.handle == "linked-return"


@pytest.mark.parametrize("namespace", ["continuation-admission", "continuation-return"])
def test_linked_pr_exact_retry_requires_selected_issue_receipt(monkeypatch, namespace):
    from issue_agent.domain.workflow_identity import WorkflowFamily
    _, files, _, _, returned = interrupted_linked_reply(monkeypatch)
    store = files._workflow_db
    store.settle_pr_repair_result(returned, files._workflow_authority.writer)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key=?", (namespace,
            store._continuation_receipt_key(returned.admission.attempt.run_id, WorkflowFamily.ISSUE_RECOVERY)))
    with pytest.raises(WorkflowAuthorityError, match="admission|receipt"):
        store.settle_pr_repair_result(returned, files._workflow_authority.writer)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_issue_and_pr_with_same_number_consume_their_own_threads():
    from issue_agent.domain.world import Comment, IssueView
    from tests.fakes.workflow_authority import resolve_snapshot, with_issue_recovery
    workflow, files, inner, key = pr_reply_workflow()
    install_workflow(files._workflow_db, with_issue_recovery(resolve_snapshot(100)))
    workflow.gateway.issues[100] = IssueView(100, title="separate", comments=(
        Comment("alice", "issue answer", "2026-10-02T00:00:01Z"),))
    asyncio.run(workflow.resume(100))
    assert issue_ci_state(files, 100).consumed_reply_at == "2026-10-02T00:00:01Z"
    assert files._workflow_authority.read_pr_repair(key).component.state.consumed_reply_at == ""
    asyncio.run(workflow.resume_pr(100))
    assert files._workflow_authority.read_pr_repair(key).component.state.consumed_reply_at == "2026-10-02T00:00:01Z"
    assert issue_ci_state(files, 100).resumes == 1 and issue_ci_state(files).consumed_reply_at == ""
    assert len(inner.tasks) == 3


def test_linked_reply_proven_unlaunched_release_finishes_both_without_paid_session(monkeypatch):
    from issue_agent.domain.world import Comment
    from tests.app.test_issue_reply_admission import setup_reply
    from tests.fakes.accounted_workflow import attach_workflow_authority
    workflow, gateway, files, inner = setup_reply()
    gateway.prs[100] = PRView(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
    gateway._pr_for_issue[5] = 100
    attach_workflow_authority(gateway, files, pr_repair=True)
    async def failed_spawn(task):
        raise FileNotFoundError("provider executable missing")
    with monkeypatch.context() as patch:
        patch.setattr(inner, "run", failed_spawn)
        with pytest.raises(FileNotFoundError):
            asyncio.run(workflow.resume(5))
    assert files._workflow_authority.read_pr_repair(WorkflowKey("o/r", PullRequestRef(100))).component.state.session is None
    assert issue_ci_state(files).resumes == 0
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='continuation-return'").fetchone()[0] == 2
    gateway.issues[5] = replace(gateway.issues[5], comments=(Comment("alice", "newer", "2026-10-02T00:00:00Z"),))
    inner.result = AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="new")
    asyncio.run(workflow.resume(5))
    assert len(inner.tasks) == 1 and issue_ci_state(files).resumes == 0


@pytest.mark.parametrize("entrypoint", ["register_process_lease", "register_lease"])
def test_selected_blank_source_corruption_after_reservation_refuses_launch(monkeypatch, entrypoint):
    from datetime import datetime, timezone
    from issue_agent.adapters.control_store import Lease
    from tests.app.test_issue_reply_admission import setup_reply
    from tests.fakes.accounted_workflow import attach_workflow_authority
    workflow, gateway, files, inner = setup_reply()
    gateway.prs[100] = PRView(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
    gateway._pr_for_issue[5] = 100
    attach_workflow_authority(gateway, files, pr_repair=True)
    store = files._workflow_db
    selected = files._workflow_authority.read_pr_repair(WorkflowKey("o/r", PullRequestRef(100)))
    files._workflow_authority.consume_blank_pr_reply(selected.binding, "2026-10-01T00:00:00Z", " ")
    captured = []
    original = store.reserve
    def interrupted(request):
        reserved = original(request)
        assert reserved.reservation_id is not None
        captured.append(request)
        raise RuntimeError("lost after reservation")
    with monkeypatch.context() as patch:
        patch.setattr(store, "reserve", interrupted)
        with pytest.raises(RuntimeError, match="lost after reservation"):
            asyncio.run(workflow.resume(5))
    request = captured[0]
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='pr-reply-noop'")
    with pytest.raises(WorkflowAuthorityError, match="blank PR reply"):
        if entrypoint == "register_process_lease":
            store.register_process_lease(request.run_id, expected_owner_id=request.owner_id,
                                         pid=123, pgid=123, process_start_ticks=456)
        else:
            now = datetime.now(timezone.utc)
            store.register_lease(Lease(run_id=request.run_id, dispatch_id=request.dispatch_id, owner_id=request.owner_id,
                                      pid=123, pgid=123, started_at=now, process_start_ticks=456))
    assert inner.tasks == []
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0


def test_linked_stale_return_settles_money_without_new_generation_overwrite(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key, returned = interrupted_linked_reply(monkeypatch)
    with files._workflow_db.transaction() as db:
        for selected_key in (WorkflowKey("o/r", IssueRef(5)), key):
            current = load_review_workflow_row(db, selected_key) if selected_key == key else load_issue_aggregate(db, selected_key)
            persist_workflow_snapshot(db, replace(current, revision=WorkflowRevision(
                replace(current.revision.fence, generation=current.revision.fence.generation+1), current.revision.version+1)))
    before_issue = issue_ci_state(files)
    before_pr = files._workflow_authority.read_pr_repair(key)
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files) == before_issue and files._workflow_authority.read_pr_repair(key) == before_pr
    files._workflow_db.settle_pr_repair_result(returned, files._workflow_authority.writer)
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_rebase_charges_sql_allowance_and_exact_retry_preserves_sibling_debt():
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="rebase-continuation"))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    workflow.cfg = replace(workflow.cfg, max_rebase_retries=1)
    issue_key = WorkflowKey("o/r", IssueRef(5))
    with files._workflow_db.transaction() as db:
        aggregate = load_issue_aggregate(db, issue_key)
        siblings = replace(aggregate.issue_recovery.state, ci_failures=2,
            review_failures=cc.ReviewRetryState(count=3), resumes=4, infra_failures=5, self_clears=6)
        persist_workflow_snapshot(db, replace(aggregate, issue_recovery=replace(aggregate.issue_recovery, state=siblings)))
    asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 1
    after = issue_ci_state(files)
    assert replace(after, infra_episode=siblings.infra_episode) == replace(siblings, rebase_failures=1, infra_failures=0)
    assert files.sessions == {} and files.branches == {} and files.models == {}
    with files._workflow_db.transaction() as db:
        returned = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    files._workflow_db.settle_pr_repair_result(returned, files._workflow_authority.writer)
    assert issue_ci_state(files) == after
    asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_rebase_reopened_process_recovers_captured_result_once(monkeypatch):
    import json
    import subprocess
    import sys
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="captured-rebase"))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    def crash(*args, **kwargs):
        raise RuntimeError("crash after durable rebase result")
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "settle_pr_repair", crash)
        with pytest.raises(RuntimeError, match="crash after durable rebase result"):
            asyncio.run(workflow.rebase(100))
    assert issue_ci_state(files).rebase_failures == 0 and issue_ci_state(files).pending_rebase is not None
    child = subprocess.run([sys.executable, "-c", '''
import json, sys
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.workflow_identity import WorkflowWriter
store=SQLiteControlStore(sys.argv[1]); store.initialize()
control=SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"), resolve_ceiling=0)
print(json.dumps([control.reconcile_pr_repair_returns(), control.reconcile_pr_repair_returns()]))
''', files._workflow_db.path], check=False, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == [1, 0]
    assert issue_ci_state(files).rebase_failures == 1 and issue_ci_state(files).pending_rebase is None
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def interrupted_rebase(monkeypatch, *, prior_attempt=False):
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="captured-rebase"))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    if prior_attempt:
        asyncio.run(workflow.rebase(100))
    def crash(*args, **kwargs):
        raise RuntimeError("interrupted rebase settlement")
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "settle_pr_repair", crash)
        with pytest.raises(RuntimeError, match="interrupted rebase settlement"):
            asyncio.run(workflow.rebase(100))
    with files._workflow_db.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result' ORDER BY claimed_at DESC LIMIT 1").fetchone()[0]
    return workflow, files, inner, key, decode_workflow(raw, cc.PRRepairResult)


def test_rebase_selected_reset_preserves_sibling_debt_and_all_holds():
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain.workflow import WorkflowSnapshot
    from tests.domain.test_continuation_components import hold
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    from tests.fakes.accounted_workflow import seed_infra_outcome
    seed_infra_outcome(files._workflow_db, 5, cc.FixInfraOutcome.RETURNED_INFRA)
    assert issue_ci_state(files).infra_failures == 1
    with files._workflow_db.transaction() as db:
        aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(5)))
        assert isinstance(aggregate, WorkflowSnapshot)
        assert isinstance(aggregate.issue_recovery, cc.ActiveIssueRecovery)
        siblings = replace(aggregate.issue_recovery.state, ci_failures=2, review_failures=cc.ReviewRetryState(count=3),
            resumes=4, self_clears=6)
        retained = replace(aggregate, gates=(hold(),), issue_recovery=replace(aggregate.issue_recovery, state=siblings, gates=(hold(),)))
        persist_workflow_snapshot(db, retained)
    control = files._workflow_authority
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    before_pr = control.read_pr_repair(key)
    source = cc.RebaseAllowanceReset(selected.binding, selected.revision, before_pr.revision.fence,
        "fix/5-remote")
    assert control.reset_rebase_allowance(source)
    assert issue_ci_state(files) == replace(siblings, rebase_failures=0)
    with files._workflow_db.transaction() as db:
        after = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(5)))
    assert after.gates == retained.gates and after.issue_recovery.gates == retained.issue_recovery.gates
    assert after.episode == retained.episode and control.read_pr_repair(key) == before_pr
    assert not control.reset_rebase_allowance(source)
    asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 1 and issue_ci_state(files).rebase_failures == 0


def test_rebase_pending_refuses_competing_purchase_and_selected_reset(monkeypatch):
    workflow, files, inner, key, returned = interrupted_rebase(monkeypatch, prior_attempt=True)
    control = files._workflow_authority
    before = issue_ci_state(files)
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.RebaseAllowanceReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence,
        "fix/5-remote")
    with pytest.raises(WorkflowAuthorityError, match="selected admitted attempt"):
        control.reset_rebase_allowance(source)
    with monkeypatch.context() as patch:
        patch.setattr(control, "reconcile_pr_repair_returns", lambda: 0)
        asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 2 and issue_ci_state(files) == before
    assert control.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).rebase_failures == 2 and issue_ci_state(files).pending_rebase is None


def test_concurrent_rebase_admissions_purchase_only_one_current_attempt(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import json
    from issue_agent.adapters.control_store import AccountingDisposition, ReservationResult
    from issue_agent.domain.contracts import ReservationId, RunId
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    store, requests = files._workflow_db, []
    original = store.reserve
    def capture(request):
        requests.append(request)
        return ReservationResult(AccountingDisposition.UNAVAILABLE, None, request.cap, request.cap, request.cap, "captured before admission")
    with monkeypatch.context() as patch:
        patch.setattr(store, "reserve", capture)
        asyncio.run(workflow.rebase(100))
    selected = requests[0]
    dispatch = json.loads(selected.dispatch_id)
    dispatch["leg_id"] = "leg:competing-rebase"
    competing = replace(selected, run_id=RunId("competing-rebase"), reservation_id=ReservationId("competing-rebase"),
        dispatch_id=json.dumps(dispatch), continuations=tuple(replace(purchase,
            capture=replace(purchase.capture, leg_id=dispatch["leg_id"])) for purchase in selected.continuations))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(original, (selected, competing)))
    assert sum(result.admitted for result in outcomes) == 1
    assert inner.tasks == [] and issue_ci_state(files).rebase_failures == 0
    assert issue_ci_state(files).pending_rebase is not None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0


def test_rebase_stale_return_settles_money_without_current_generation_overwrite(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key, returned = interrupted_rebase(monkeypatch)
    with files._workflow_db.transaction() as db:
        issue = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(5)))
        current = replace(issue, revision=WorkflowRevision(replace(issue.revision.fence, generation=issue.revision.fence.generation+1), issue.revision.version+1),
            issue_recovery=replace(issue.issue_recovery, state=replace(issue.issue_recovery.state, rebase_failures=7, ci_failures=4)))
        persist_workflow_snapshot(db, current)
        pr = load_review_workflow_row(db, key)
        persist_workflow_snapshot(db, replace(pr, revision=WorkflowRevision(replace(pr.revision.fence, generation=pr.revision.fence.generation+1), pr.revision.version+1)))
    control = files._workflow_authority
    before = issue_ci_state(files)
    assert control.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files) == before and control.read_pr_repair(key).component.state.session is None
    workflow.cfg = replace(workflow.cfg, max_rebase_retries=8)
    inner.result = replace(inner.result, session_id="current-rebase")
    asyncio.run(workflow.rebase(100))
    assert issue_ci_state(files).rebase_failures == 8 and issue_ci_state(files).ci_failures == 4
    current = control.read_pr_repair(key)
    files._workflow_db.settle_pr_repair_result(returned, control.writer)
    assert control.read_pr_repair(key) == current and issue_ci_state(files).rebase_failures == 8
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_rebase_budget_refusal_preserves_every_selected_input(monkeypatch):
    from issue_agent.domain.contracts import Money
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    control = files._workflow_authority
    before_issue, before_pr = issue_ci_state(files), control.read_pr_repair(key)
    reserve = files._workflow_db.reserve
    monkeypatch.setattr(files._workflow_db, "reserve", lambda request: reserve(replace(request, cap=Money("USD", 0))))
    asyncio.run(workflow.rebase(100))
    assert inner.tasks == [] and issue_ci_state(files) == before_issue
    assert control.read_pr_repair(key) == before_pr
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("reason,count", [("budget_exhausted", 0), ("transport_death", 0), ("agent_error", 1)])
def test_rebase_classifies_returned_charge_without_erasing_siblings(reason, count):
    from issue_agent.domain.outcome import ExitReason
    workflow, files, _, _ = active_ci_workflow(AgentResult(is_error=True, exit_reason=ExitReason(reason), cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    before = issue_ci_state(files)
    asyncio.run(workflow.rebase(100))
    after = issue_ci_state(files)
    assert replace(after, infra_episode=before.infra_episode) == replace(before, rebase_failures=count,
        infra_failures=1 if reason == "transport_death" else 0)


def test_rebase_proven_unlaunched_release_preserves_debt_and_allows_retry(monkeypatch):
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="eventual"))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    before = issue_ci_state(files)
    async def failed_spawn(task):
        raise FileNotFoundError("provider executable missing")
    with monkeypatch.context() as patch:
        patch.setattr(inner, "run", failed_spawn)
        with pytest.raises(FileNotFoundError, match="provider executable"):
            asyncio.run(workflow.rebase(100))
    assert issue_ci_state(files) == before
    assert files._workflow_authority.read_pr_repair(key).component.state.session is None
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0
    asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 1 and issue_ci_state(files).rebase_failures == 1


@pytest.mark.parametrize("namespace", ["continuation-admission", "continuation-return"])
def test_rebase_exact_retry_refuses_missing_selected_issue_receipt(namespace):
    workflow, files, _, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    store = files._workflow_db
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
        returned = decode_workflow(raw, cc.PRRepairResult)
        db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key=?", (namespace,
            store._continuation_receipt_key(returned.admission.attempt.run_id, returned.rebase.admission.attempt.binding.authority.family)))
    with pytest.raises(WorkflowAuthorityError, match="selected|admission"):
        store.settle_pr_repair_result(returned, files._workflow_authority.writer)
    assert issue_ci_state(files).rebase_failures == 1
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_rebase_reset_caught_receipt_failure_rolls_back_grant(monkeypatch):
    workflow, files, _, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    control, store = files._workflow_authority, files._workflow_db
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.RebaseAllowanceReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "fix/5-remote")
    with store.transaction() as db:
        before = tuple(db.iterdump())
        db.execute("CREATE TRIGGER fail_reset BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='rebase-allowance-reset' BEGIN SELECT RAISE(ABORT,'reset receipt failure'); END")
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(Exception, match="reset receipt failure"):
                control.reset_rebase_allowance(source)
        db.execute("DROP TRIGGER fail_reset")
        assert tuple(db.iterdump()) == before
    assert issue_ci_state(files).rebase_failures == 1


@pytest.mark.parametrize("mutation", ["missing_receipt", "corrupt_receipt", "wrong_branch", "stale_generation", "stale_revision"])
def test_rebase_reset_refuses_unverified_or_changed_selected_source(mutation):
    workflow, files, _, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    control, store = files._workflow_authority, files._workflow_db
    issue, repair = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))), control.read_pr_repair(key)
    source = cc.RebaseAllowanceReset(issue.binding, issue.revision, repair.revision.fence, "fix/5-remote")
    if mutation in {"missing_receipt", "corrupt_receipt"}:
        with store.transaction() as db:
            receipt = store._continuation_receipt_key(repair.component.state.actual_model.source.attempt.run_id, repair.binding.authority.family)
            if mutation == "missing_receipt":
                db.execute("DELETE FROM dedupe_keys WHERE namespace='continuation-return' AND key=?", (receipt,))
            else:
                db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='continuation-return' AND key=?", (receipt,))
    elif mutation == "wrong_branch":
        source = replace(source, branch="fix/5-changed")
    elif mutation == "stale_generation":
        source = replace(source, pr_fence=replace(source.pr_fence, generation=source.pr_fence.generation+1))
    else:
        source = replace(source, revision=replace(source.revision, version=source.revision.version+1))
    with store.transaction() as db:
        before = tuple(db.iterdump())
    with pytest.raises((WorkflowAuthorityError, ValueError)):
        control.reset_rebase_allowance(source)
    with store.transaction() as db:
        assert tuple(db.iterdump()) == before
    assert issue_ci_state(files).rebase_failures == 1


def test_rebase_reset_cannot_select_another_issues_pr():
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.fakes.workflow_authority import resolve_snapshot, with_issue_recovery
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    workflow.gateway.prs[101] = PRView(101, head_ref_name="fix/6-other", head_ref_oid="b" * 40, closing_issue=6)
    install_workflow(files._workflow_db, with_issue_recovery(resolve_snapshot(6)))
    install_pr_repair_authority(workflow.gateway, files)
    asyncio.run(workflow.rebase(101))
    assert len(inner.tasks) == 2
    control = files._workflow_authority
    issue = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    other = control.read_pr_repair(WorkflowKey("o/r", PullRequestRef(101)))
    source = cc.RebaseAllowanceReset(issue.binding, issue.revision, other.revision.fence, "fix/6-other")
    with files._workflow_db.transaction() as db:
        before = tuple(db.iterdump())
    with pytest.raises(WorkflowAuthorityError, match="another issue"):
        control.reset_rebase_allowance(source)
    with files._workflow_db.transaction() as db:
        assert tuple(db.iterdump()) == before
    assert issue_ci_state(files).rebase_failures == 1 and issue_ci_state(files, 6).rebase_failures == 1


def test_rebase_original_reset_retry_does_not_erase_later_attempt():
    workflow, files, _, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.rebase(100))
    control = files._workflow_authority
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.RebaseAllowanceReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "fix/5-remote")
    assert control.reset_rebase_allowance(source)
    asyncio.run(workflow.rebase(100))
    before = issue_ci_state(files), control.read_pr_repair(key)
    assert issue_ci_state(files).rebase_failures == 1
    assert not control.reset_rebase_allowance(source)
    assert (issue_ci_state(files), control.read_pr_repair(key)) == before


def test_review_repair_charges_same_timestamp_until_ceiling_then_newer_review_only_resets():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="review-continuation"))
    workflow.cfg = replace(workflow.cfg, max_review_retries=2)
    first = "2026-10-02T00:00:01Z"
    second = "2026-10-02T00:00:02Z"
    gateway = workflow.gateway
    gateway.prs[100] = replace(gateway.prs[100], head_ref_oid="a"*40,
        reviews=(Review("CHANGES_REQUESTED", "reviewer", first, body="fix it"),))
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 1
    assert (issue_ci_state(files).review_failures.review_at, issue_ci_state(files).review_failures.count) == (first, 1)
    assert issue_ci_state(files).review_failures.pending is None
    asyncio.run(workflow.address_review(100))
    assert issue_ci_state(files).review_failures.count == 2
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 2
    gateway.prs[100] = replace(gateway.prs[100], head_ref_oid="b"*40)
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 2
    gateway.prs[100] = replace(gateway.prs[100], reviews=(Review("CHANGES_REQUESTED", "reviewer", second),))
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 3
    assert (issue_ci_state(files).review_failures.review_at, issue_ci_state(files).review_failures.count) == (second, 1)
    gateway.prs[100] = replace(gateway.prs[100], reviews=(Review("CHANGES_REQUESTED", "reviewer", first),))
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 3
    assert (issue_ci_state(files).review_failures.review_at, issue_ci_state(files).review_failures.count) == (second, 1)
    assert files.sessions == {} and files.branches == {}


def review_workflow(result):
    workflow, files, inner, key = active_ci_workflow(result)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a"*40,
        reviews=(Review("CHANGES_REQUESTED", "reviewer", "2026-10-02T00:00:01Z", body="fix it"),))
    return workflow, files, inner, key


def test_review_repair_preserves_primary_review_and_sibling_issue_allowances():
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    with files._workflow_db.transaction() as db:
        before = load_review_workflow_row(db, key)
    issue_before = issue_ci_state(files)
    asyncio.run(workflow.address_review(100))
    with files._workflow_db.transaction() as db:
        after = load_review_workflow_row(db, key)
    assert (after.episode, after.execution, after.intent, after.gates) == (before.episode, before.execution, before.intent, before.gates)
    issue_after = issue_ci_state(files)
    assert replace(issue_after, review_failures=issue_before.review_failures,
                   infra_episode=issue_before.infra_episode) == issue_before
    assert issue_after.infra_failures == 0


def test_unknown_legacy_review_episode_refuses_without_purchasing_or_resetting():
    from tests.fakes.accounted_workflow import seed_review_retry
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    seed_review_retry(files, "", 3)
    before = issue_ci_state(files)
    asyncio.run(workflow.address_review(100))
    assert inner.tasks == [] and issue_ci_state(files) == before
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_disabled_review_ceiling_preserves_existing_unbounded_policy():
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.cfg = replace(workflow.cfg, max_review_retries=0)
    for _ in range(4):
        asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 4 and issue_ci_state(files).review_failures.count == 4


def test_review_budget_refusal_preserves_timestamp_continuation_and_fork_inputs(monkeypatch):
    from issue_agent.domain.contracts import Money
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    before_issue = issue_ci_state(files)
    before_pr = files._workflow_authority.read_pr_repair(key)
    reserve = files._workflow_db.reserve
    monkeypatch.setattr(files._workflow_db, "reserve", lambda request: reserve(replace(request, cap=Money("USD", 0))))
    asyncio.run(workflow.address_review(100))
    assert inner.tasks == [] and issue_ci_state(files) == before_issue
    assert files._workflow_authority.read_pr_repair(key) == before_pr
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("namespace", ["continuation-admission", "continuation-return"])
def test_review_exact_retry_refuses_missing_selected_issue_receipt(namespace):
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.address_review(100))
    store = files._workflow_db
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
        returned = decode_workflow(raw, cc.PRRepairResult)
        db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key=?", (namespace,
            store._continuation_receipt_key(returned.admission.attempt.run_id, returned.review.admission.attempt.binding.authority.family)))
    with pytest.raises(WorkflowAuthorityError, match="selected|admission"):
        store.settle_pr_repair_result(returned, files._workflow_authority.writer)
    assert issue_ci_state(files).review_failures.count == 1
    with store.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_review_proven_unlaunched_release_keeps_debt_and_continuation_unspent(monkeypatch):
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="eventual"))
    run = inner.run
    async def failed_spawn(task):
        raise FileNotFoundError("provider executable missing")
    with monkeypatch.context() as patch:
        patch.setattr(inner, "run", failed_spawn)
        with pytest.raises(FileNotFoundError, match="provider executable"):
            asyncio.run(workflow.address_review(100))
    debt = issue_ci_state(files).review_failures
    assert debt.count == 0 and debt.pending is None
    assert files._workflow_authority.read_pr_repair(key).component.state.session is None
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0
    asyncio.run(workflow.address_review(100))
    assert issue_ci_state(files).review_failures.count == 1
    assert files._workflow_authority.read_pr_repair(key).component.state.session.handle == "eventual"


def interrupted_review(monkeypatch):
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False,
        status="pushed", session_id="durable-review", cost_usd=.01))
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "settle_pr_repair", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")))
        with pytest.raises(RuntimeError, match="interrupted"):
            asyncio.run(workflow.address_review(100))
    return workflow, files, inner, key


@pytest.mark.parametrize("reason,status,count", [("killed_externally", "timeout", 0), ("budget_exhausted", "", 0),
                                                  ("ok", "blocked", 0), ("ok", "pushed", 1)])
def test_review_return_preserves_attempt_classification(reason, status, count):
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, key = review_workflow(AgentResult(
        is_error=reason != "ok", exit_reason=ExitReason(reason), status=status, cost_usd=.01))
    asyncio.run(workflow.address_review(100))
    assert issue_ci_state(files).review_failures.count == count
    assert issue_ci_state(files).review_failures.pending is None


def test_review_restart_recovers_durable_output_once_and_preserves_sibling_reply(monkeypatch):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from tests.adapters.test_workflow_admission import request
    workflow, files, inner, key = interrupted_review(monkeypatch)
    selected = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    pending = selected.component.state.review_failures.pending
    purchase = cc.IssueRecoveryPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:newer-reply"),
        "2026-10-02T10:00:00+00:00", "fix/5-newer", 20)
    requested = replace(request("newer-review-reply", binding=selected.binding), owner_id=files._workflow_authority.writer.owner_id)
    assert files._workflow_db.reserve_issue_recovery(requested, purchase).admitted
    restarted = SQLiteControlStore(files._workflow_db.path)
    restarted.initialize()
    recovered = SQLiteWorkflowControl(restarted, files._workflow_authority.writer, resolve_ceiling=20)
    assert recovered.reconcile_pr_repair_returns() == 1
    state = recovered.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state
    assert state.review_failures.count == 1 and state.review_failures.pending is None
    assert state.remembered_branch == "fix/5-newer" and state.consumed_reply_at == purchase.reply_at
    assert recovered.read_pr_repair(key).component.state.session.handle == "durable-review"
    assert recovered.reconcile_pr_repair_returns() == 0
    assert recovered.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state == state
    with restarted.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_review_stale_issue_result_settles_money_without_erasing_new_episode(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key = interrupted_review(monkeypatch)
    issue_key = WorkflowKey("o/r", IssueRef(5))
    with files._workflow_db.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
        returned = decode_workflow(raw, cc.PRRepairResult)
        current = load_issue_aggregate(db, issue_key)
        changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=current.revision.fence.generation+1), current.revision.version+1))
        persist_workflow_snapshot(db, changed)
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).review_failures.count == 0
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], reviews=(Review("CHANGES_REQUESTED", "reviewer", "2026-10-02T00:00:02Z"),))
    asyncio.run(workflow.address_review(100))
    state = issue_ci_state(files)
    assert (state.review_failures.review_at, state.review_failures.count) == ("2026-10-02T00:00:02Z", 1)
    assert files._workflow_db.settle_pr_repair_result(returned, files._workflow_authority.writer).idempotent
    assert issue_ci_state(files) == state
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_review_relinking_and_ambiguous_current_prs_refuse_without_purchase():
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.address_review(100))
    workflow.gateway.prs[101] = replace(workflow.gateway.prs[100], number=101,
        reviews=(Review("CHANGES_REQUESTED", "reviewer", "2026-10-02T00:00:02Z"),))
    install_pr_repair_authority(workflow.gateway, files)
    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(workflow.address_review(101))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], state="CLOSED")
    asyncio.run(workflow.address_review(101))
    assert len(inner.tasks) == 1
    assert issue_ci_state(files).review_failures.count == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_review_distinct_reviewer_filter_does_not_rekey_to_own_or_nonqualifying_review():
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.cfg = replace(workflow.cfg, fix_bot="fixer")
    first = workflow.gateway.prs[100].reviews[0]
    own = Review("CHANGES_REQUESTED", workflow.cfg.fix_bot, "2026-10-02T00:00:02Z")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], reviews=(first, own))
    asyncio.run(workflow.address_review(100))
    assert issue_ci_state(files).review_failures.review_at == first.submitted_at
    approved = Review("APPROVED", "reviewer", "2026-10-02T00:00:03Z")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], reviews=(first, own, approved))
    asyncio.run(workflow.address_review(100))
    assert len(inner.tasks) == 1 and issue_ci_state(files).review_failures.count == 1


@pytest.mark.parametrize("mutation", ["drop_issue", "drop_all", "kind", "issue_float", "pr_float"])
@pytest.mark.parametrize("lane", ["review", "rebase"])
def test_repair_common_admission_refuses_missing_purchase_or_changed_actual_context(monkeypatch, mutation, lane):
    import json
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    invoke = workflow.address_review if lane == "review" else workflow.rebase
    original = files._workflow_db.reserve
    requests = []
    def capture(request):
        requests.append(request)
        from issue_agent.adapters.control_store import ReservationResult, AccountingDisposition
        return ReservationResult(AccountingDisposition.UNAVAILABLE, None, request.cap, request.cap, request.cap, "captured before admission")
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "reserve", capture)
        asyncio.run(invoke(100))
    request = requests[0]
    if mutation == "drop_all":
        request = replace(request, continuations=(), workflow=None)
    elif mutation == "drop_issue":
        request = replace(request, continuations=tuple(p for p in request.continuations if isinstance(p, cc.PRRepairPurchase)))
    else:
        dispatch = json.loads(request.dispatch_id)
        dispatch.update({"kind": "FIX_CI"} if mutation == "kind" else {"issue": 5.0} if mutation == "issue_float" else {"number": 100.0})
        request = replace(request, dispatch_id=json.dumps(dispatch))
    assert not original(request).admitted
    assert inner.tasks == []
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_ci_attempt_counts_in_issue_authority_and_ceiling_prevents_purchase():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01))
    workflow.cfg = replace(workflow.cfg, max_ci_retries=2)
    asyncio.run(workflow.fix_ci(100))
    asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).ci_failures == 2
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 2
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 2


def test_ci_attempt_moves_issue_authority_before_returning_to_workflow():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).ci_failures == 1


@pytest.mark.parametrize("reason,count", [
    ("budget_exhausted", 0), ("transport_death", 0), ("agent_error", 1),
])
def test_ci_return_classification_preserves_existing_policy(reason, count):
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=True, exit_reason=ExitReason(reason), cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    state = issue_ci_state(files)
    assert state.ci_failures == count and state.pending_ci is None
    assert len(inner.tasks) == 1


def test_ci_green_reset_is_exact_and_preserves_sibling_allowances():
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    issue_key = WorkflowKey("o/r", IssueRef(5))
    with files._workflow_db.transaction() as db:
        current = load_issue_aggregate(db, issue_key)
        siblings = replace(current.issue_recovery.state, resumes=2, review_failures=cc.ReviewRetryState(count=3), rebase_failures=4,
                           infra_failures=5, self_clears=6)
        persist_workflow_snapshot(db, replace(current, issue_recovery=replace(current.issue_recovery, state=siblings)))
    asyncio.run(workflow.fix_ci(100))
    control = files._workflow_authority
    selected = control.read_issue_recovery(issue_key)
    before_primary = control.load_workflow(issue_key).episode
    source = cc.CIEpisodeReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "a" * 40)
    assert control.reset_ci_episode(source)
    after_reset = issue_ci_state(files)
    assert replace(after_reset, infra_episode=siblings.infra_episode) == replace(siblings, infra_failures=0)
    assert control.load_workflow(issue_key).episode == before_primary
    asyncio.run(workflow.fix_ci(100))
    after = issue_ci_state(files)
    assert after.ci_failures == 1
    assert not control.reset_ci_episode(source)
    assert issue_ci_state(files) == after


@pytest.mark.parametrize("namespace", ["continuation-admission", "continuation-return"])
def test_ci_exact_retry_refuses_missing_selected_issue_receipt(namespace):
    from issue_agent.domain.workflow_identity import WorkflowFamily
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    store = files._workflow_db
    with store.transaction() as db:
        returned = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
        db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND key=?", (namespace,
            store._continuation_receipt_key(returned.settlement.run_id, WorkflowFamily.ISSUE_RECOVERY)))
    with pytest.raises(WorkflowAuthorityError, match="CI output lacks|CI exact retry lost"):
        store.settle_pr_repair_result(returned, files._workflow_authority.writer)
    assert issue_ci_state(files).ci_failures == 1


def test_fix_ci_commits_remote_branch_and_session_with_money():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="first-session", cost_usd=.01, model="actual-ci"))
    files.sessions[5] = "legacy-session"
    files.models[5] = "legacy-model"
    files.branches[5] = "fix/5-obsolete"
    asyncio.run(workflow.fix_ci(100))
    selected = files._workflow_authority.read_pr_repair(key)
    assert len(inner.tasks) == 1
    assert not inner.tasks[0].resume
    assert selected.component.state.branch == "fix/5-remote"
    assert selected.component.state.session.handle == "first-session"
    assert selected.component.state.actual_model.model == "actual-ci"
    assert files.sessions == {5: "legacy-session"}
    assert files.models == {5: "legacy-model"}
    assert files.branches == {5: "fix/5-obsolete"}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT sum(amount_micros) FROM charges").fetchone()[0] == 10000
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 1


def test_repair_restart_reads_durable_result_without_provider_replay(monkeypatch):
    import json
    import subprocess
    import sys
    import pytest
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="durable-session", cost_usd=.01))
    def interrupted(*args, **kwargs):
        raise RuntimeError("process lost before money/continuity commit")
    monkeypatch.setattr(files._workflow_db, "settle_pr_repair", interrupted)
    with pytest.raises(RuntimeError, match="process lost"):
        asyncio.run(workflow.fix_ci(100))
    before = files._workflow_authority.read_pr_repair(key)
    assert before.component.state.session is None
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
    code = '''
import json,sys
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.workflow_identity import WorkflowWriter,WorkflowKey,PullRequestRef
store=SQLiteControlStore(sys.argv[1]); store.initialize()
control=SQLiteWorkflowControl(store,WorkflowWriter("o/r","fix","o/r:fix:fixture"),resolve_ceiling=20)
first=control.reconcile_pr_repair_returns(); second=control.reconcile_pr_repair_returns()
selected=control.read_pr_repair(WorkflowKey("o/r",PullRequestRef(100)))
with store.transaction() as db:
    charges=db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()
print(json.dumps([first,second,selected.component.state.session.handle,list(charges)]))
'''
    resumed = subprocess.run([sys.executable, "-c", code, files._workflow_db.path], text=True,
                             capture_output=True, check=True)
    assert json.loads(resumed.stdout) == [1, 0, "durable-session", [1, 10000]]
    assert len(inner.tasks) == 1


def test_ci_compatible_session_then_sessionless_output_clears_without_revival():
    from dataclasses import replace
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="compatible", cost_usd=.01, model="actual-ci"))
    workflow.cfg = replace(workflow.cfg, fix=replace(workflow.cfg.fix, model=None))
    asyncio.run(workflow.fix_ci(100))
    assert files._workflow_authority.read_pr_repair(key).component.state.actual_model.model == "actual-ci"
    inner.result = replace(inner.result, session_id="")
    asyncio.run(workflow.fix_ci(100))
    assert inner.tasks[-1].resume == "compatible"
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.session is None and state.actual_model.model == "actual-ci"
    asyncio.run(workflow.fix_ci(100))
    assert not inner.tasks[-1].resume
    assert inner.tasks[-1].model == "actual-ci"


def test_ci_runtime_switch_starts_fresh_and_accepts_its_new_output():
    from dataclasses import replace
    from issue_agent.domain.agent_task import RuntimeName
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="claude-session", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    workflow.cfg = replace(workflow.cfg, fixer_runtime=RuntimeName.CODEX)
    inner.result = replace(inner.result, session_id="codex-session")
    asyncio.run(workflow.fix_ci(100))
    assert not inner.tasks[-1].resume
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.session.handle == "codex-session"
    assert state.session.runtime is RuntimeName.CODEX


def test_ci_stale_durable_result_settles_without_overwriting_new_generation(monkeypatch):
    from dataclasses import replace
    import pytest
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="old-session", cost_usd=.01))
    settle = files._workflow_db.settle_pr_repair
    monkeypatch.setattr(files._workflow_db, "settle_pr_repair", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        current = load_review_workflow_row(db, key)
        changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=1), current.revision.version + 1))
        persist_workflow_snapshot(db, changed)
    monkeypatch.setattr(files._workflow_db, "settle_pr_repair", settle)
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert files._workflow_authority.read_pr_repair(key).component.state.session is None
    inner.result = replace(inner.result, session_id="current-session")
    asyncio.run(workflow.fix_ci(100))
    before = files._workflow_authority.read_pr_repair(key)
    assert before.component.state.session.handle == "current-session"
    assert files._workflow_authority.reconcile_pr_repair_returns() == 0
    assert files._workflow_authority.read_pr_repair(key) == before
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_ci_known_issue_session_without_return_receipt_refuses_without_purchase():
    from dataclasses import replace
    import pytest
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot, WorkflowAuthorityError
    from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
    from issue_agent.domain.workflow import FreshResolve, ResolveSession
    from issue_agent.domain.workflow_identity import IssueRef
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed"))
    issue_key = WorkflowKey("o/r", IssueRef(5))
    state = files._workflow_authority.load_workflow(issue_key)
    session = ResolveSession(WorkflowAttemptIdentity(state.binding, RunId("unverified"), "unverified-leg"),
                             "unverified-handle", "claude", "m")
    changed = replace(state, revision=replace(state.revision, version=1),
        episode=replace(state.episode, continuation=FreshResolve(session, 1)))
    with files._workflow_db.transaction() as db:
        persist_workflow_snapshot(db, changed)
    with pytest.raises(WorkflowAuthorityError, match="exact returned resolve receipt"):
        asyncio.run(workflow.fix_ci(100))
    assert inner.tasks == []
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_ci_verified_resolve_handoff_selects_exact_session_and_branch():
    from dataclasses import replace
    from issue_agent.domain.world import IssueView
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="resolved", cost_usd=.01))
    workflow.gateway.issues[5] = IssueView(5, title="remote", body="issue", updated_at="revision")
    workflow.gateway._pr_for_issue[5] = 100
    asyncio.run(workflow.resolve(5))
    issue = files._workflow_authority.load_workflow(WorkflowKey("o/r", IssueRef(5)))
    assert issue.episode.continuation.session.session_id == "resolved"
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
    with files._workflow_db.transaction() as db:
        updated = replace(issue, issue_recovery=replace(issue.issue_recovery, state=replace(issue.issue_recovery.state, resumes=3)))
        persist_workflow_snapshot(db, updated)
    inner.result = replace(inner.result, session_id="repaired")
    asyncio.run(workflow.fix_ci(100))
    assert inner.tasks[-1].resume == "resolved"
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.session.handle == "repaired" and state.resumes == 3


def test_closed_unmerged_requeue_preserves_debt_and_terminal_merge_clears_it():
    from dataclasses import replace
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="closed-session", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        state = load_review_workflow_row(db, key)
        state = replace(state, pr_repair=replace(state.pr_repair, state=replace(
            state.pr_repair.state, resumes=4, ci_failures=3, infra_failures=2)))
        persist_workflow_snapshot(db, state)
    files._workflow_authority.cleanup_pr_repair(key, False)
    retained = files._workflow_authority.read_pr_repair(key).component.state
    assert retained.session is None and retained.resumes == 4
    assert retained.branch == "fix/5-remote" and retained.actual_model.model == "m"
    assert retained.ci_failures == 3 and retained.infra_failures == 2
    files._workflow_authority.cleanup_pr_repair(key, True)
    retired = files._workflow_authority.read_pr_repair(key).component.state
    assert retired.session is None and retired.actual_model is None and retired.branch == ""
    assert retired.resumes == 0 and retired.ci_failures == 0 and retired.infra_failures == 0


def test_ci_never_launched_release_preserves_inputs_for_next_admission(monkeypatch):
    import pytest
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="later-success", cost_usd=.01))
    run = inner.run
    async def failed_spawn(task):
        raise FileNotFoundError("provider executable missing")
    monkeypatch.setattr(inner, "run", failed_spawn)
    with pytest.raises(FileNotFoundError, match="provider executable"):
        asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0
        assert db.execute("SELECT state,outcome FROM runs").fetchone()[:] == ("cancelled", "unlaunched_released")
    monkeypatch.setattr(inner, "run", run)
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    assert files._workflow_authority.read_pr_repair(key).component.state.session.handle == "later-success"


def test_ci_missing_selected_output_receipt_refuses_without_new_money():
    import pytest
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="selected", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='continuation-return'")
    with pytest.raises(WorkflowAuthorityError, match="exact applied output receipt"):
        asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_ci_missing_resolve_activation_with_retained_authority_refuses_without_purchase():
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed"))
    with files._workflow_db.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs WHERE family='resolve'")
    with pytest.raises(WorkflowAuthorityError, match="activation is missing despite retained"):
        asyncio.run(workflow.fix_ci(100))
    assert inner.tasks == []
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def captured_interrupted_ci_result(monkeypatch):
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False,status='pushed',session_id='durable',cost_usd=.01, model='real-provider-model'))
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db,'settle_pr_repair',lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('interrupted')))
        with pytest.raises(RuntimeError,match='interrupted'):
            asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    return workflow,files,inner,key,decode_workflow(raw,cc.PRRepairResult)


def test_generic_issue_settlement_cannot_bypass_captured_ci_classification(monkeypatch):
    _, files, _, _, returned = captured_interrupted_ci_result(monkeypatch)
    with pytest.raises(WorkflowAuthorityError, match="captured paired repair result"):
        files._workflow_db.settle_issue_recovery(returned.settlement, returned.ci.admission, cc.IssueRecoveryState())
    assert issue_ci_state(files).ci_failures == 0
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).ci_failures == 1


@pytest.mark.parametrize("lane", ["ci", "rebase"])
def test_newer_issue_reply_cannot_erase_pending_repair_outcome(monkeypatch, lane):
    from tests.adapters.test_workflow_admission import request
    _, files, _, _, returned = (captured_interrupted_ci_result(monkeypatch) if lane == "ci" else interrupted_rebase(monkeypatch))
    selected = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    purchase = cc.IssueRecoveryPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:newer-reply"),
        "2026-10-02T10:00:00+00:00", "fix/5-newer", 20)
    requested = replace(request("newer-reply", binding=selected.binding), owner_id=files._workflow_authority.writer.owner_id)
    assert files._workflow_db.reserve_issue_recovery(requested, purchase).admitted
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    state = issue_ci_state(files)
    assert getattr(state, "ci_failures" if lane == "ci" else "rebase_failures") == 1
    assert state.resumes == 1 and getattr(state, "pending_" + lane) is None
    assert state.remembered_branch == "fix/5-newer" and state.consumed_reply_at == purchase.reply_at
    assert files._workflow_db.settle_pr_repair_result(returned, files._workflow_authority.writer).idempotent
    assert issue_ci_state(files) == state


def test_old_issue_generation_ci_result_settles_without_changing_current_debt(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key, returned = captured_interrupted_ci_result(monkeypatch)
    issue_key = WorkflowKey("o/r", IssueRef(5))
    with files._workflow_db.transaction() as db:
        current = load_issue_aggregate(db, issue_key)
        changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=current.revision.fence.generation+1), current.revision.version+1))
        persist_workflow_snapshot(db, changed)
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).ci_failures == 0
    asyncio.run(workflow.fix_ci(100))
    after = issue_ci_state(files)
    assert after.ci_failures == 1 and after.pending_ci is None
    assert files._workflow_db.settle_pr_repair_result(returned, files._workflow_authority.writer).idempotent
    assert issue_ci_state(files) == after
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_ci_reset_caught_receipt_failure_rolls_back_the_grant(monkeypatch):
    workflow, files, _, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    control, store = files._workflow_authority, files._workflow_db
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.CIEpisodeReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "a" * 40)
    with store.transaction() as db:
        before = tuple(db.iterdump())
        db.execute("CREATE TRIGGER fail_reset BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='ci-episode-reset' BEGIN SELECT RAISE(ABORT,'reset receipt failure'); END")
        @contextmanager
        def borrowed():
            yield db
        with monkeypatch.context() as patch:
            patch.setattr(store, "transaction", borrowed)
            with pytest.raises(Exception, match="reset receipt failure"):
                control.reset_ci_episode(source)
        db.execute("DROP TRIGGER fail_reset")
        assert tuple(db.iterdump()) == before
    assert issue_ci_state(files).ci_failures == 1


def test_pending_ci_serializes_two_prs_for_the_same_issue(monkeypatch):
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    workflow, files, inner, key, returned = captured_interrupted_ci_result(monkeypatch)
    workflow.gateway.prs[101] = replace(workflow.gateway.prs[100], number=101)
    install_pr_repair_authority(workflow.gateway, files)
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_authority, "reconcile_pr_repair_returns", lambda: 0)
        asyncio.run(workflow.fix_ci(101))
    assert len(inner.tasks) == 1
    assert issue_ci_state(files).pending_ci == returned.ci.admission
    control = files._workflow_authority
    selected = control.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    source = cc.CIEpisodeReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "a" * 40)
    before = issue_ci_state(files)
    with pytest.raises(ValueError, match="selected admitted attempt"):
        control.reset_ci_episode(source)
    assert issue_ci_state(files) == before
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 1
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    asyncio.run(workflow.fix_ci(101))
    assert len(inner.tasks) == 2 and issue_ci_state(files).ci_failures == 2


def test_ci_pure_consumption_refuses_another_leg():
    from issue_agent.domain.contracts import ContinuationAdmission, RunId, WorkflowAttemptIdentity
    workflow, files, _, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed"))
    selected = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    purchase = cc.IssueCIPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "captured-leg"),
                                PullRequestRef(100), "a" * 40, 3)
    wrong = ContinuationAdmission(WorkflowAttemptIdentity(selected.binding, RunId("captured-run"), "other-leg"),
                                  selected.revision.version + 1)
    with pytest.raises(ValueError, match="differs from its selected authority"):
        cc.consume_issue_ci(selected, purchase, wrong)


def test_ci_paired_result_refuses_another_repository(monkeypatch):
    _, _, _, _, returned = captured_interrupted_ci_result(monkeypatch)
    attempt = returned.ci.admission.attempt
    foreign = replace(attempt.binding, fence=replace(attempt.binding.fence, key=WorkflowKey("other/repo", IssueRef(5))))
    ci = replace(returned.ci, admission=replace(returned.ci.admission, attempt=replace(attempt, binding=foreign)))
    with pytest.raises(ValueError, match="same launched run and leg"):
        replace(returned, ci=ci)

@pytest.mark.parametrize("lane", ["ci", "rebase"])
def test_adopted_pending_result_is_recovered(monkeypatch, lane):
    _,files,_,key,returned = captured_interrupted_ci_result(monkeypatch) if lane == "ci" else interrupted_rebase(monkeypatch)
    store = files._workflow_db
    store.adopt_legacy_owner('o/r','fix','o/r:fix:successor',prior_owner_ids=('o/r:fix:fixture',))
    writer = SQLiteWorkflowControl(store,WorkflowWriter('o/r','fix','o/r:fix:successor'),resolve_ceiling=20)
    assert writer.reconcile_pr_repair_returns() == 1
    assert writer.read_pr_repair(key).component.state.session.handle == ('durable' if lane == 'ci' else 'captured-rebase')

def test_direct_settle_cannot_bypass_captured_branch(monkeypatch):
    _,files,_,key,returned = captured_interrupted_ci_result(monkeypatch)
    with pytest.raises((ValueError,WorkflowAuthorityError)):
        files._workflow_db.settle_pr_repair(returned.settlement,returned.admission,replace(returned.state,branch='fix/99-unselected'), writer=files._workflow_authority.writer)

@pytest.mark.parametrize('writer',[
    WorkflowWriter('other/repo','fix','other/repo:fix:fixture'),
    WorkflowWriter('o/r','review','o/r:review:fixture'),
    WorkflowWriter('o/r','fix','o/r:fix:unrelated'),
])
def test_wrong_writer_cannot_capture_or_settle(monkeypatch, writer):
    _,files,_,_,returned = captured_interrupted_ci_result(monkeypatch)
    store=files._workflow_db
    with store.transaction() as db:
        before=list(db.iterdump())
    for action in (lambda: store.capture_pr_repair_result(returned,writer),
                   lambda: store.settle_pr_repair(returned.settlement,returned.admission,returned.state,writer=writer)):
        with pytest.raises((ValueError,WorkflowAuthorityError)):
            action()
    with store.transaction() as db:
        assert list(db.iterdump()) == before

def test_captured_runtime_cannot_supply_other_runtime_session(monkeypatch):
    workflow,files,inner,key=active_ci_workflow(AgentResult(is_error=False,status='pushed',session_id='real-claude',cost_usd=.01))
    store=files._workflow_db
    capture=store.capture_pr_repair_result
    def substitute(returned,writer):
        wrong=replace(returned,state=replace(returned.state,
                    session=cc.RepairSession(returned.admission,'unlaunched-codex',RuntimeName.CODEX,'unlaunched-model')))
        return capture(wrong,writer)
    monkeypatch.setattr(store,'capture_pr_repair_result',substitute)
    with pytest.raises((ValueError,WorkflowAuthorityError)):
        asyncio.run(workflow.fix_ci(100))
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0

def test_never_launched_claim_refusal_does_not_strand_component(monkeypatch):
    from issue_agent.adapters.accounting_runtime import AccountingRuntime
    workflow,files,inner,key=active_ci_workflow(AgentResult(is_error=False,status='pushed',session_id='real',cost_usd=.01))
    original=AccountingRuntime.run
    async def run(self,task,**kwargs):
        old=self.require_dispatch_claim
        object.__setattr__(self,'require_dispatch_claim',True)
        try:
            return await original(self,task,**kwargs)
        finally:
            object.__setattr__(self,'require_dispatch_claim',old)
    monkeypatch.setattr(AccountingRuntime,'run',run)
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db,'bind_dispatch_claim',lambda *args,**kwargs:False)
        asyncio.run(workflow.fix_ci(100))
    assert inner.tasks == []
    with files._workflow_db.transaction() as db:
        assert db.execute('SELECT count(*) FROM charges').fetchone()[0] == 0
        assert db.execute('SELECT state FROM budget_reservations').fetchone()[0] == 'released'
    monkeypatch.setattr(AccountingRuntime,'run',original)
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1

@pytest.mark.parametrize("lane", ["ci", "review", "rebase", "linked_reply"])
def test_caught_paid_settlement_failure_rolls_back_money_and_continuity(monkeypatch, lane):
    import issue_agent.adapters.workflow_rows as rows
    if lane == "ci":
        _,files,_,key,returned=captured_interrupted_ci_result(monkeypatch)
    elif lane == "rebase":
        _, files, _, key, returned = interrupted_rebase(monkeypatch)
    elif lane == "linked_reply":
        _, files, _, key, returned = interrupted_linked_reply(monkeypatch)
    else:
        _,files,_,key=interrupted_review(monkeypatch)
        with files._workflow_db.transaction() as db:
            raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
        returned = decode_workflow(raw, cc.PRRepairResult)
    store=files._workflow_db
    before=files._workflow_authority.read_pr_repair(key)
    before_issue = issue_ci_state(files)
    original=rows.persist_workflow_snapshot
    def fail(db,state):
        original(db,state)
        raise RuntimeError('after component write')
    monkeypatch.setattr(rows,'persist_workflow_snapshot',fail)
    with store.transaction() as db:
        @contextmanager
        def borrowed():
            yield db
        monkeypatch.setattr(store,'transaction',borrowed)
        with pytest.raises(RuntimeError,match='component write'):
            store.settle_pr_repair_result(returned,files._workflow_authority.writer)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('scratch','o/r','outer','0','{}')")
    monkeypatch.undo()
    assert files._workflow_authority.read_pr_repair(key) == before
    assert issue_ci_state(files) == before_issue
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM charges').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-return'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='scratch'").fetchone()[0] == 1

def test_caught_unlaunched_failure_rolls_back_release_and_progress(monkeypatch):
    from issue_agent.adapters.accounting_runtime import AccountingRuntime
    workflow,files,inner,key=active_ci_workflow(AgentResult(is_error=False,status='pushed',session_id='unused',cost_usd=.01))
    store=files._workflow_db
    async def failed(task):
        raise RuntimeError('lost before spawn')
    with monkeypatch.context() as patch:
        patch.setattr(inner,'run',failed)
        patch.setattr(AccountingRuntime,'_recover_or_release',lambda *args:None)
        with pytest.raises(RuntimeError,match='before spawn'):
            asyncio.run(workflow.fix_ci(100))
    before=files._workflow_authority.read_pr_repair(key)
    with store.transaction() as db:
        run_id=db.execute('SELECT run_id FROM runs').fetchone()[0]
        assert db.execute('SELECT state FROM budget_reservations').fetchone()[0] == 'active'
    original=store._finish_unlaunched_pr_repair
    def fail(db,run):
        original(db,run)
        raise RuntimeError('after progress')
    with monkeypatch.context() as patch:
        patch.setattr(store,'_finish_unlaunched_pr_repair',fail)
        with store.transaction() as db:
            @contextmanager
            def borrowed():
                yield db
            patch.setattr(store,'transaction',borrowed)
            with pytest.raises(RuntimeError,match='after progress'):
                store.release_unlaunched(run_id,'spawn-refused')
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('scratch','o/r','outer','0','{}')")
    assert files._workflow_authority.read_pr_repair(key) == before
    with store.transaction() as db:
        assert db.execute('SELECT state FROM budget_reservations').fetchone()[0] == 'active'
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-return'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='scratch'").fetchone()[0] == 1
    assert store.release_unlaunched(run_id,'spawn-refused').disposition.value == 'available'
    after=files._workflow_authority.read_pr_repair(key)
    assert after.component.watermarks.last_applied == after.component.watermarks.last_admitted
    assert after.component.state == before.component.state

def test_issue_generation_change_before_handoff_purchase_refuses(monkeypatch):
    from issue_agent.domain.world import IssueView
    from issue_agent.adapters.workflow_rows import load_resolve_workflow_row,persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowRevision
    workflow,files,inner,key=active_ci_workflow(AgentResult(is_error=False,status='pushed',session_id='resolved',cost_usd=.01))
    workflow.gateway.issues[5]=IssueView(5,title='remote',body='issue',updated_at='revision')
    workflow.gateway._pr_for_issue[5]=100
    asyncio.run(workflow.resolve(5))
    original=workflow.worktrees.ensure
    def changed_issue_generation(issue,branch):
        with files._workflow_db.transaction() as db:
            current=load_resolve_workflow_row(db,WorkflowKey('o/r',IssueRef(5)))
            persist_workflow_snapshot(db,replace(current,revision=WorkflowRevision(replace(current.revision.fence,generation=current.revision.fence.generation+1),current.revision.version+1)))
        return original(issue,branch)
    monkeypatch.setattr(workflow.worktrees,'ensure',changed_issue_generation)
    try:
        asyncio.run(workflow.fix_ci(100))
    except WorkflowAuthorityError:
        pass
    assert len(inner.tasks) == 1


def test_unlaunched_branch_change_does_not_reclassify_old_session(monkeypatch):
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="old-branch-session", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    prior = files._workflow_authority.read_pr_repair(key).component.state
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_name="fix/5-new-remote")
    async def refused(task):
        assert not task.resume
        raise FileNotFoundError("never started on new branch")
    with monkeypatch.context() as patch:
        patch.setattr(inner, "run", refused)
        with pytest.raises(FileNotFoundError):
            asyncio.run(workflow.fix_ci(100))
    assert files._workflow_authority.read_pr_repair(key).component.state == prior
    inner.result = replace(inner.result, session_id="new-branch-session")
    asyncio.run(workflow.fix_ci(100))
    assert not inner.tasks[-1].resume
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.branch == "fix/5-new-remote" and state.session.handle == "new-branch-session"


@pytest.mark.parametrize("recovery_first", [False, True])
def test_fix_deterministic_launched_failure_uses_captured_ceiling_in_both_orders(monkeypatch, recovery_first):
    from issue_agent.domain.contracts import RunId
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    object.__setattr__(workflow, "_fixture_infra_ceiling", 5)
    store = files._workflow_db
    async def deterministic_failure(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        store.register_process_lease(run_id, expected_owner_id="o/r:fix:fixture",
            pid=123, pgid=123, process_start_ticks=99)
        if recovery_first:
            assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == (run_id,)
            assert issue_ci_state(files).infra_failures == 1
        object.__setattr__(workflow, "_fixture_infra_ceiling", 99)
        raise RuntimeError("exceeded maximum buffer size")
    monkeypatch.setattr(inner, "run", deterministic_failure)
    with pytest.raises(RuntimeError, match="exceeded maximum buffer size"):
        asyncio.run(workflow.fix_ci(100))
    assert issue_ci_state(files).infra_failures == 5
    with store.transaction() as db:
        receipt = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0], cc.FixInfraReceipt)
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
    assert receipt.charge == 5 and receipt.outcome is cc.FixInfraOutcome.DETERMINISTIC_DEATH
    store.record_fix_infra_return(receipt.admission.attempt.run_id, cc.FixInfraOutcome.DETERMINISTIC_DEATH, files._workflow_authority.writer)
    assert issue_ci_state(files).infra_failures == 5


@pytest.mark.parametrize("late", [cc.FixInfraOutcome.RETURNED_INFRA, cc.FixInfraOutcome.RETURNED_NORMAL, cc.FixInfraOutcome.DETERMINISTIC_DEATH])
@pytest.mark.parametrize("generation_change", [False, True])
def test_fix_late_old_death_or_reply_cannot_erase_or_revive_newer_episode(late, generation_change):
    from tests.fakes.accounted_workflow import seed_infra_outcome
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    _, files, _, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    old_run = seed_infra_outcome(store, 5, cc.FixInfraOutcome.RECLAIMED)
    seed_infra_outcome(store, 5, cc.FixInfraOutcome.RETURNED_NORMAL)
    assert issue_ci_state(files).infra_failures == 0
    if generation_change:
        with store.transaction() as db:
            current = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(5)))
            persist_workflow_snapshot(db, replace(current, revision=WorkflowRevision(
                replace(current.revision.fence, generation=current.revision.fence.generation + 1), current.revision.version + 1)))
    seed_infra_outcome(store, 5, cc.FixInfraOutcome.RETURNED_INFRA)
    before = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    store.record_fix_infra_return(old_run, late, files._workflow_authority.writer)
    store.record_fix_infra_return(old_run, late, files._workflow_authority.writer)
    assert files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))) == before
    assert before.component.state.infra_failures == 1


def test_selected_fix_infra_impossible_contribution_is_refused_before_purchase():
    import json
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=True, cost_usd=.01, exit_reason=ExitReason.TRANSPORT_DEATH))
    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        row = db.execute("SELECT key,payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()
        wire = json.loads(row[1])
        wire["value"]["fields"]["charge"] = 999
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='fix-infra-outcome' AND key=?", (json.dumps(wire), row[0]))
    with pytest.raises((ValueError, WorkflowAuthorityError), match="charge|receipt|failures"):
        files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    with pytest.raises((ValueError, WorkflowAuthorityError)):
        asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1


def test_actual_normal_return_resets_episode_without_releasing_unreadable_spend():
    from tests.fakes.accounted_workflow import seed_infra_outcome
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed",
        usage={"input_tokens":"malformed"}))
    seed_infra_outcome(files._workflow_db, 5, cc.FixInfraOutcome.RETURNED_INFRA)
    assert issue_ci_state(files).infra_failures == 1
    with pytest.raises(ValueError):
        asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    assert issue_ci_state(files).infra_failures == 0
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges WHERE amount_micros>0").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0


def test_machine_retirement_is_selected_and_preserves_budget_hold(tmp_path):
    import pytest
    from issue_agent.adapters.store import Store
    from issue_agent.domain.self_clear import BudgetBlock
    from issue_agent.domain.actions import Target
    files = Store(str(tmp_path / "state"))
    budget = BudgetBlock(5, Target.PR, 100, 10000, "issue", 100000)
    files.set_budget_block(budget)
    workflow, files, _, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "checks"}, cost_usd=.01), files)
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.fix_ci(100))
    control = files._workflow_db
    key = WorkflowKey("o/r", IssueRef(5))
    selected = control.read_issue_recovery(key)
    with pytest.raises(ValueError, match="cause/head"):
        control.retire_machine_block(selected.revision, blocker="publication", blocked_head="a" * 40)
    assert control.read_issue_recovery(key) == selected
    control.retire_machine_block(selected.revision, blocker="checks", blocked_head="a" * 40)
    assert control.read_issue_recovery(key).component.state.machine_block is None
    assert files.get_budget_block(5) == budget
    with pytest.raises(ValueError, match="stale"):
        control.retire_machine_block(selected.revision, blocker="checks", blocked_head="a" * 40)
    assert files.get_self_clear_count(5) == 0


def test_stale_pr_machine_return_settles_without_installing_old_head(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", structured={"blocker": "checks"}, cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="old-head")
    control = files._workflow_db
    settle = control.settle_pr_repair
    monkeypatch.setattr(control, "settle_pr_repair", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(workflow.fix_ci(100))
    with control.transaction() as db:
        current = load_review_workflow_row(db, key)
        persist_workflow_snapshot(db, replace(current, revision=WorkflowRevision(
            replace(current.revision.fence, generation=1), current.revision.version + 1)))
    monkeypatch.setattr(control, "settle_pr_repair", settle)
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    issue_key = WorkflowKey("o/r", IssueRef(5))
    assert control.read_issue_recovery(issue_key).component.state.machine_block is None
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="new-head")
    inner.result = replace(inner.result, structured={"blocker": "publication"})
    asyncio.run(workflow.fix_ci(100))
    newer = control.read_issue_recovery(issue_key)
    assert newer.component.state.machine_block.blocked_head == "new-head"
    assert newer.component.state.machine_block.blocker.value == "publication"
    assert files._workflow_authority.reconcile_pr_repair_returns() == 0
    assert control.read_issue_recovery(issue_key) == newer
    with control.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (2, 20000)
