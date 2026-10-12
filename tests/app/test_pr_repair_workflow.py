"""PR repair uses selected SQL continuity before buying a CI leg."""

from tests.domain.builders import observed_pr
from tests.fakes.workflow_authority import retained_issue_file, seed_retained_issue_file


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
from tests.fakes.accounted_workflow import machine_ends


def test_fix_ci_without_active_pr_component_refuses_without_purchase():
    gateway = FakeGateway(prs={100:observed_pr(100, head_ref_name="fix/5-remote", closing_issue=5,
        checks=(Check("tests", bucket="fail"),))})
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
    gateway = FakeGateway(prs={100: observed_pr(
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
    gateway = FakeGateway(prs={100: observed_pr(100, head_ref_name="fix/5-remote", closing_issue=5,
        head_ref_oid="a" * 40, mergeable="CONFLICTING")})
    inner = FakeRunner(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="unselected"))
    files = Store(str(tmp_path / "state"))
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    install_workflow(files._workflow_db, held(key=WorkflowKey("o/r", PullRequestRef(100))))

    asyncio.run(workflow.rebase(100))

    assert inner.tasks == []
    assert retained_issue_file(files, 5, "session") == "" and retained_issue_file(files, 5, "branch") == ""
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def active_ci_workflow(result, files=None):
    from dataclasses import replace
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from tests.adapters.test_continuation_admission import active, install_secondary

    gateway = FakeGateway(prs={100: observed_pr(100, head_ref_name="fix/5-remote", closing_issue=5,
        checks=(Check("tests", bucket="fail"),))})
    inner = FakeRunner(result)
    files = files if files is not None else FakeStore()
    workflow = _workflow(gateway, files, inner, pr_repair_authority=False)
    key = WorkflowKey("o/r", PullRequestRef(100))
    base = replace(held(key=key), gates=())
    install_workflow(files._workflow_db, base)
    component = active(key, WorkflowFamily.PR_REPAIR)
    install_secondary(files._workflow_db, replace(base, pr_repair=component), component)
    return workflow, files, inner, key


def test_settled_superseded_raw_repair_history_cannot_block_current_paid_work():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="current", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        retired = db.execute("SELECT key FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    asyncio.run(workflow.fix_ci(100))
    current = files._workflow_authority.read_pr_repair(key)
    with files._workflow_db.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='malformed retired output' "
                   "WHERE namespace='pr-repair-result' AND key=?", (retired,))
    assert files._workflow_authority.read_pr_repair(key) == current
    assert files._workflow_authority.reconcile_pr_repair_returns() == 0
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 3
    assert issue_ci_state(files).pending_ci is None
    assert issue_ci_state(files).ci_failures == 3
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (3, 30000)


def test_zero_owed_recovery_work_is_independent_of_retired_history(monkeypatch):
    workflow, files, _, _ = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="current", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    store = files._workflow_db
    connect = store._connect
    def recovery_steps():
        steps = [0]
        def counted_connect():
            db = connect()
            def count():
                steps[0] += 1
                return 0
            db.set_progress_handler(count, 1)
            return db
        with monkeypatch.context() as counted:
            counted.setattr(store, "_connect", counted_connect)
            assert files._workflow_authority.reconcile_pr_repair_returns() == 0
        return steps[0]
    before = recovery_steps()
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
        done = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND key LIKE '%pr_repair'").fetchone()[0]
        # Unreferenced retired receipts cannot become a current obligation.
        for number in range(2000):
            for namespace, payload in (("pr-repair-result", raw), ("continuation-return", done)):
                db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (namespace, "o/r", f"retired-{number}", "2026-10-01T00:00:00Z", payload))
    after = recovery_steps()
    assert after <= before + 64
    assert issue_ci_state(files).ci_failures == 1 and issue_ci_state(files).pending_ci is None


def issue_ci_state(files, issue=5):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate
    with files._workflow_db.transaction() as db:
        aggregate = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(issue)))
        return cc.select_issue_recovery(aggregate).component.state


_LEGACY_ISSUE_FIELDS = {
    "self_clears": 2, "self_clear_ceiling_reported": True,
    # Shapes as persisted by deployed daemons before self-clear was deleted.
    "machine_block": {"type": "RecordedMachineBlock", "fields": {
        "blocked_head": "d" * 40, "blocker": {"type": "MachineBlocker", "value": "checks"},
        "issue": 5, "number": 100, "pr": 100, "source": None, "target": {"type": "Target", "value": 2}}},
    "budget_block": {"type": "RefusedBudgetBlock", "fields": {"scope": "repository", "cap_micros": 1}},
}


def _inject_retired_self_clear_fields(store):
    import json

    retired = {"IssueRecoveryState": _LEGACY_ISSUE_FIELDS, "PRRepairState": {"self_clears": 1}}

    def legacy(node: object) -> object:
        if isinstance(node, list):
            return [legacy(value) for value in node]
        if not isinstance(node, dict):
            return node
        rewritten = {key: legacy(value) for key, value in node.items()}
        fields = rewritten.get("fields")
        extra = retired.get(str(node.get("type")))
        return {**rewritten, "fields": {**fields, **extra}} if extra and isinstance(fields, dict) else rewritten

    injected = 0
    with store.transaction() as db:
        for rowid, raw in db.execute("SELECT rowid, episode_json FROM workflows").fetchall():
            rewritten = json.dumps(legacy(json.loads(raw)), sort_keys=True, separators=(",", ":"))
            if rewritten != raw:
                db.execute("UPDATE workflows SET episode_json=? WHERE rowid=?", (rewritten, rowid))
                injected += 1
    return injected


def test_rows_persisted_with_retired_self_clear_state_load_and_the_issue_proceeds():
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="legacy", cost_usd=.01))
    before = issue_ci_state(files)
    assert _inject_retired_self_clear_fields(files._workflow_db) > 0

    assert issue_ci_state(files) == before
    assert files._workflow_authority.read_pr_repair(key) is not None
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1


def test_fix_infra_return_and_exact_recovery_record_one_machine_end_and_no_ci_debt():
    from issue_agent.domain.outcome import ExitReason
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=True, status="timeout", exit_reason=ExitReason.TRANSPORT_DEATH, cost_usd=.01))

    asyncio.run(workflow.fix_ci(100))
    with files._workflow_db.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    recorded = decode_workflow(raw, cc.PRRepairResult)
    files._workflow_db.settle_pr_repair_result(recorded, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"))

    state = issue_ci_state(files)
    assert machine_ends(files) == [True]
    assert state.ci_failures == 0 and state.pending_ci is None
    assert len(inner.tasks) == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)


def test_fix_infra_then_error_return_is_one_machine_end_and_one_ci_attempt_after_reopen():
    from issue_agent.domain.outcome import ExitReason
    from issue_agent.adapters.control_store import SQLiteControlStore
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=True, status="timeout", exit_reason=ExitReason.TRANSPORT_DEATH, cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    assert machine_ends(files) == [True]
    with files._workflow_db.transaction() as db:
        original = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    inner.result = AgentResult(is_error=True, status="error", exit_reason=ExitReason.AGENT_ERROR, cost_usd=.01)
    asyncio.run(workflow.fix_ci(100))

    reopened = SQLiteControlStore(files._workflow_db.path)
    selected = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    assert machine_ends(reopened) == [False, True] and selected.component.state.ci_failures == 1
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
    assert machine_ends(files) == [True]
    if not recovery_first:
        assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == ()
        # Exception retention already completed this run and removed its lease.
    assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == ()
    from issue_agent.adapters.control_store import SQLiteControlStore
    reopened = SQLiteControlStore(store.path)
    assert machine_ends(reopened) == [True]
    assert reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state.ci_failures == 0
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
        assert machine_ends(files) == [True]
        return await original(task)
    monkeypatch.setattr(inner, "run", reclaimed_reply)
    asyncio.run(workflow.fix_ci(100))
    assert machine_ends(files) == [not normal]  # the actual reply wins over the in-flight reclaim
    assert issue_ci_state(files).ci_failures == (1 if normal else 0)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone()[:] == (1, 10000)
        receipt = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0], cc.FixInfraReceipt)
    assert receipt.outcome is (cc.FixInfraOutcome.RETURNED_NORMAL if normal else cc.FixInfraOutcome.RETURNED_INFRA)
    store.record_fix_infra_return(receipt.admission.attempt.run_id, cc.FixInfraOutcome.RECLAIMED, files._workflow_authority.writer)
    assert machine_ends(files) == [not normal]


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
    assert machine_ends(files) == [True]

def pr_reply_workflow(*, establish_session=True, body="Use A."):
    from issue_agent.domain.world import Comment, IssueView
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id="selected-pr-session"))
    workflow.gateway.issues[5] = IssueView(5, title="Choose an option")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    if establish_session:
        asyncio.run(workflow.fix_ci(100))
        # Retained files are deliberately present; the selected SQL receipt owns continuity.
        seed_retained_issue_file(files, 5, "session", "selected-pr-session")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], comments=(
        Comment("alice", body, "2026-10-02T00:00:01Z"),))
    return workflow, files, inner, key


def test_result_naming_an_old_blocker_is_an_ordinary_question_held_until_a_human_reply():
    # The retired `blocker` field once minted a machine hold the harness released
    # by itself when CI went green. It is now ignored: the agent asked a question,
    # so only a qualifying human reply resumes the PR.
    from issue_agent.app.roles import _fix_state
    from issue_agent.domain.actions import AgentKind, Relabel, RunAgent
    from issue_agent.domain.fix import FixConfig, decide_fix
    from issue_agent.domain.labels import Label
    from issue_agent.domain.world import Comment, IssueView, WorldState
    workflow, files, inner, _ = active_ci_workflow(AgentResult(
        is_error=False, status="blocked", needs="Which CI permissions?", structured={"blocker": "checks"},
        session_id="selected-pr-session", cost_usd=.01))
    workflow.gateway.issues[5] = IssueView(5, title="Choose an option")
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    asyncio.run(workflow.fix_ci(100))
    seed_retained_issue_file(files, 5, "session", "selected-pr-session")
    assert ("relabel", "pr", 100, None, Label.AI_BLOCKED.value) in [
        call[:5] for call in workflow.gateway.calls if call[0] == "relabel"]
    question = next(call[3] for call in workflow.gateway.calls if call[:3] == ("comment", "pr", 100))
    green = observed_pr(100, head_ref_name="fix/5-remote", head_ref_oid="a" * 40, closing_issue=5,
        author="fix-bot", labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}),
        checks=(Check("tests", bucket="pass"),),
        comments=(Comment("fix-bot", question, "2026-10-02T00:00:00Z"),))
    cfg = FixConfig(fix_bot="fix-bot")

    def decide(pr, now):
        world = WorldState(prs=(pr,), now=now)
        state = _fix_state(files, world, workflow_control=files._workflow_authority,
            available_runtimes=frozenset({"claude"}))
        return decide_fix(world, cfg, state, fixer_busy=False)

    for tick in range(3):
        actions = decide(green, f"2026-10-02T0{tick + 1}:00:00Z")
        assert not any(isinstance(action, RunAgent) for action in actions)
        assert not any(isinstance(action, Relabel) and action.number == 100
                       and action.remove == Label.AI_BLOCKED for action in actions)

    replied = replace(green, comments=green.comments + (
        Comment("alice", "Grant the workflow write permission.", "2026-10-02T05:00:00Z"),))
    assert RunAgent(AgentKind.PR_RESUME, 100) in decide(replied, "2026-10-02T06:00:00Z")


def test_duplicate_pr_reply_purchases_one_resume_and_keeps_issue_marker_independent():
    workflow, files, inner, key = pr_reply_workflow()
    before_issue = issue_ci_state(files)
    asyncio.run(workflow.resume_pr(100))
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 2  # One initial CI leg and one reply-driven resume.
    assert issue_ci_state(files) == before_issue
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.consumed_reply_at == "2026-10-02T00:00:01Z" and state.resumes == 1
    assert files.pr_resumed_at == {}
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "missing"])
def test_human_reply_resumes_blocked_pr_while_ci_waits(status):
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.domain.labels import Label
    from issue_agent.domain.world import Comment, WorldState
    from issue_agent.app.roles import _fix_state
    from issue_agent.domain.fix import FixConfig, decide_fix
    from issue_agent.domain.actions import AgentKind, RunAgent
    workflow, files, inner, key = pr_reply_workflow(body="Fix the workflow permissions so CI can run.")
    pr = workflow.gateway.prs[100]
    pr = replace(pr, author="fix-bot", labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}),
        ci=Observed(pr.head_ref_oid, pr.head_ref_oid,
            frozenset({RequiredCheck("tests")}) if status == "missing" else frozenset(),
            () if status == "missing" else (Check("tests", status, "pending", "2026-10-12T01:50:00Z"),)),
        comments=(Comment("fix-bot", "**Agent needs input:** Which CI permissions?", "2026-10-02T00:00:00Z"), *pr.comments))
    workflow.gateway.prs[100] = pr
    world = WorldState(prs=(pr,), now="2026-10-12T02:00:00Z")
    state = _fix_state(files, world, workflow_control=files._workflow_authority,
        available_runtimes=frozenset({"claude"}))
    assert RunAgent(AgentKind.PR_RESUME, 100) in decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    asyncio.run(workflow.resume_pr(100))
    asyncio.run(workflow.resume_pr(100))
    assert len(inner.tasks) == 2
    state = files._workflow_authority.read_pr_repair(key).component.state
    assert state.consumed_reply_at == "2026-10-02T00:00:01Z" and state.resumes == 1


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
    assert issue_ci_state(files, 6) == before_issue
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
    gateway.prs[100] = observed_pr(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
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
    gateway.prs[100] = observed_pr(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
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
    gateway.prs[100] = observed_pr(100, head_ref_name="fix/5-title", head_ref_oid="a" * 40, closing_issue=5)
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
    workflow.cfg = replace(workflow.cfg, max_rebase_retries=1)
    issue_key = WorkflowKey("o/r", IssueRef(5))
    with files._workflow_db.transaction() as db:
        aggregate = load_issue_aggregate(db, issue_key)
        siblings = replace(aggregate.issue_recovery.state, ci_failures=2,
            review_failures=cc.ReviewRetryState(count=3), resumes=4)
        persist_workflow_snapshot(db, replace(aggregate, issue_recovery=replace(aggregate.issue_recovery, state=siblings)))
    asyncio.run(workflow.rebase(100))
    assert len(inner.tasks) == 1
    after = issue_ci_state(files)
    assert after == replace(siblings, rebase_failures=1)
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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


def test_rebase_pending_refuses_competing_purchase(monkeypatch):
    workflow, files, inner, key, returned = interrupted_rebase(monkeypatch, prior_attempt=True)
    control = files._workflow_authority
    before = issue_ci_state(files)
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
    before = issue_ci_state(files)
    asyncio.run(workflow.rebase(100))
    after = issue_ci_state(files)
    assert after == replace(before, rebase_failures=count)
    assert machine_ends(files) == [reason == "transport_death"]


def test_rebase_proven_unlaunched_release_preserves_debt_and_allows_retry(monkeypatch):
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01, session_id="eventual"))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
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
    assert replace(issue_after, review_failures=issue_before.review_failures) == issue_before


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
    if lane == "rebase":
        workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], mergeable="CONFLICTING")
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
        siblings = replace(current.issue_recovery.state, resumes=2, review_failures=cc.ReviewRetryState(count=3), rebase_failures=4)
        persist_workflow_snapshot(db, replace(current, issue_recovery=replace(current.issue_recovery, state=siblings)))
    asyncio.run(workflow.fix_ci(100))
    control = files._workflow_authority
    selected = control.read_issue_recovery(issue_key)
    before_primary = control.load_workflow(issue_key).episode
    source = cc.CIEpisodeReset(selected.binding, selected.revision, control.read_pr_repair(key).revision.fence, "a" * 40)
    assert control.reset_ci_episode(source)
    after_reset = issue_ci_state(files)
    assert after_reset == siblings
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
    selected = files._workflow_authority.read_pr_repair(key)
    source = cc.PRRepairCleanup(selected.binding, selected.revision, "head-1", False)
    files._workflow_authority.cleanup_pr_repair(key, False, source)
    retained = files._workflow_authority.read_pr_repair(key).component.state
    assert retained.session is None and retained.resumes == 4
    assert retained.branch == "fix/5-remote" and retained.actual_model.model == "m"
    assert retained.ci_failures == 3 and retained.infra_failures == 2
    with pytest.raises(WorkflowAuthorityError, match="revision"):
        files._workflow_authority.cleanup_pr_repair(key, False, source)
    selected = files._workflow_authority.read_pr_repair(key)
    terminal = cc.PRRepairCleanup(selected.binding, selected.revision, "head-1", True)
    files._workflow_authority.cleanup_pr_repair(key, True, terminal)
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


@pytest.mark.parametrize("lane", ["ci", "review", "rebase"])
def test_money_settled_captured_repair_still_recovers_exact_pending_classification(monkeypatch, lane):
    if lane == "review":
        _, files, _, key = interrupted_review(monkeypatch)
        with files._workflow_db.transaction() as db:
            returned = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys "
                "WHERE namespace='pr-repair-result'").fetchone()[0], cc.PRRepairResult)
    else:
        _, files, _, key, returned = (captured_interrupted_ci_result(monkeypatch) if lane == "ci"
            else interrupted_rebase(monkeypatch))
    from issue_agent.adapters.control_store import AccountingDisposition
    assert files._workflow_db.settle(returned.settlement).disposition is AccountingDisposition.AVAILABLE
    pending = issue_ci_state(files)
    assert (pending.pending_ci if lane == "ci" else pending.review_failures.pending
            if lane == "review" else pending.pending_rebase) is not None
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert files._workflow_authority.reconcile_pr_repair_returns() == 0
    recovered = issue_ci_state(files)
    assert (recovered.pending_ci if lane == "ci" else recovered.review_failures.pending
            if lane == "review" else recovered.pending_rebase) is None
    assert (recovered.ci_failures if lane == "ci" else recovered.review_failures.count
            if lane == "review" else recovered.rebase_failures) == 1
    assert files._workflow_authority.read_pr_repair(key).component.state.session is not None
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, returned.settlement.amount.micros)


def test_public_review_release_and_newer_rebase_preserve_superseded_money_settled_ci(monkeypatch, tmp_path):
    from tests.fakes.persistent_github import PersistentGitHub
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.labels import Label
    from issue_agent.domain.world import IssueView
    from issue_agent.domain.contracts import CommandId
    from issue_agent.domain.workflow import WorkflowCommitted
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", session_id="durable", cost_usd=.01))
    store = files._workflow_db
    captured = []
    with monkeypatch.context() as pause:
        pause.setattr(store, "capture_pr_repair_result", lambda returned, writer: captured.append(returned))
        pause.setattr(store, "settle_pr_repair", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("paused")))
        with pytest.raises(RuntimeError, match="paused"):
            asyncio.run(workflow.fix_ci(100))
    old = captured[0]
    from issue_agent.adapters.control_store import AccountingDisposition
    assert store.settle(old.settlement).disposition is AccountingDisposition.AVAILABLE
    remote = PersistentGitHub(tmp_path / "release.db")
    remote.seed(IssueView(5, title="fixture", updated_at="2026-10-02T00:00:00Z"))
    remote.seed_pr(observed_pr(100, head_ref_oid="a" * 40, head_ref_name="fix/5-remote", closing_issue=5,
                         labels=frozenset({Label.AI_PR_OPEN})))
    for number in range(2):
        remote.seed_review(100, f"review-{number}", author="review-bot[bot]", commit="a" * 40,
                           submitted_at=f"2026-09-25T11:00:0{number}+00:00")
    writer = WorkflowWriter("o/r", "review", "o/r:review:fixture")
    review = SQLiteReviewWorkflowControl(store, writer, gateway=remote, ceiling=3,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"))
    effects = GitHubEffectExecutor(remote, store, repository="o/r", identity=writer.owner_id, log=lambda _: None)
    before = review.load_workflow(key)
    assert effects.submit_bound_review(100, "COMMENT", "ceiling", "a" * 40, workflow=before.binding,
        reviewer="review-bot[bot]", patch_id="c" * 64, escalate_cause="ceiling")
    with store.transaction() as db:
        command = CommandId(db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0])
    review.open_ceiling(command)
    disabled = SQLiteReviewWorkflowControl(store, writer, gateway=remote, ceiling=0,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"))
    released = disabled.reconcile_ceiling(key)
    assert isinstance(released, WorkflowCommitted)
    assert released.snapshot.revision.fence.generation == before.revision.fence.generation + 1
    effects.drain_pending_commands()
    workflow.gateway._pr_for_issue[5] = 100
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40, mergeable="CONFLICTING")
    reserve = store.reserve
    def late_capture(request):
        if request.kind == "REBASE":
            store.capture_pr_repair_result(old, workflow.workflow_control.writer)
        return reserve(request)
    with monkeypatch.context() as racing:
        racing.setattr(store, "reserve", late_capture)
        asyncio.run(workflow.rebase(100))
    newer = files._workflow_authority.read_pr_repair(key)
    assert len(inner.tasks) == 2
    assert issue_ci_state(files).pending_ci == old.ci.admission
    assert issue_ci_state(files).ci_failures == 0 and issue_ci_state(files).rebase_failures == 1
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).pending_ci is None and issue_ci_state(files).ci_failures == 1
    assert files._workflow_authority.read_pr_repair(key) == newer
    with store.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (2, 20000)


def test_malformed_pending_repair_stays_held_without_suppressing_healthy_peer(monkeypatch):
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.fakes.workflow_authority import with_issue_recovery, resolve_snapshot
    workflow, files, inner, _, returned = captured_interrupted_ci_result(monkeypatch)
    key = files._workflow_db._continuation_receipt_key(returned.admission.attempt.run_id,
        returned.admission.attempt.binding.authority.family)
    held = issue_ci_state(files)
    with files._workflow_db.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='malformed current output' "
                   "WHERE namespace='pr-repair-result' AND key=?", (key,))
    assert files._workflow_authority.reconcile_pr_repair_returns() == 0
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1 and issue_ci_state(files) == held
    install_workflow(files._workflow_db, with_issue_recovery(resolve_snapshot(6)))
    workflow.gateway.prs[101] = replace(workflow.gateway.prs[100], number=101,
        head_ref_name="fix/6-peer", closing_issue=6)
    workflow.gateway._pr_for_issue[6] = 101
    install_pr_repair_authority(workflow.gateway, files)
    asyncio.run(workflow.fix_ci(101))
    assert len(inner.tasks) == 2
    assert issue_ci_state(files) == held
    assert issue_ci_state(files, 6).pending_ci is None and issue_ci_state(files, 6).ci_failures == 1
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, 10000)
        assert db.execute("SELECT count(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 1


def test_malformed_owed_prefix_cannot_starve_healthy_return_beyond_batch_boundary(monkeypatch):
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from tests.fakes.workflow_authority import with_issue_recovery, resolve_snapshot
    workflow, files, inner, _, _ = captured_interrupted_ci_result(monkeypatch)
    store = files._workflow_db
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='malformed current output' WHERE namespace='pr-repair-result'")
    for issue in range(6, 38):
        pr = issue + 95
        install_workflow(store, with_issue_recovery(resolve_snapshot(issue)))
        workflow.gateway.prs[pr] = replace(workflow.gateway.prs[100], number=pr,
            head_ref_name=f"fix/{issue}-peer", closing_issue=issue)
        workflow.gateway._pr_for_issue[issue] = pr
        install_pr_repair_authority(workflow.gateway, files)
        with monkeypatch.context() as pause:
            pause.setattr(store, "settle_pr_repair", lambda *args, **kwargs:
                (_ for _ in ()).throw(RuntimeError("interrupted")))
            with pytest.raises(RuntimeError, match="interrupted"):
                asyncio.run(workflow.fix_ci(pr))
        if issue < 37:
            with store.transaction() as db:
                db.execute("UPDATE dedupe_keys SET payload_json='malformed current output' WHERE namespace='pr-repair-result'")
    assert len(inner.tasks) == 33
    assert issue_ci_state(files, 37).pending_ci is not None
    assert workflow.workflow_control.reconcile_pr_repair_returns() == 1
    assert workflow.workflow_control.reconcile_pr_repair_returns() == 0
    assert issue_ci_state(files, 37).pending_ci is None and issue_ci_state(files, 37).ci_failures == 1
    for issue in range(5, 37):
        assert issue_ci_state(files, issue).pending_ci is not None
        assert issue_ci_state(files, issue).ci_failures == 0
    with store.transaction() as db:
        assert db.execute("SELECT count(*),sum(amount_micros) FROM charges").fetchone()[:] == (1, 10000)
        assert db.execute("SELECT count(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 32


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
def test_fix_deterministic_launched_failure_is_one_machine_end_in_both_orders(monkeypatch, recovery_first):
    from issue_agent.domain.contracts import RunId
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    async def deterministic_failure(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        store.register_process_lease(run_id, expected_owner_id="o/r:fix:fixture",
            pid=123, pgid=123, process_start_ticks=99)
        if recovery_first:
            assert store.reclaim_dead_leases("o/r:fix:fixture", lambda *_: False) == (run_id,)
        raise RuntimeError("exceeded maximum buffer size")
    monkeypatch.setattr(inner, "run", deterministic_failure)
    with pytest.raises(RuntimeError, match="exceeded maximum buffer size"):
        asyncio.run(workflow.fix_ci(100))
    assert machine_ends(files) == [True]
    assert issue_ci_state(files).ci_failures == 0
    with store.transaction() as db:
        receipt = decode_workflow(db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome'").fetchone()[0], cc.FixInfraReceipt)
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
    assert receipt.outcome is cc.FixInfraOutcome.RECLAIMED


_O = cc.FixInfraOutcome


@pytest.mark.parametrize("first,late,machine", [
    (_O.RECLAIMED, _O.RETURNED_NORMAL, False),
    (_O.RECLAIMED, _O.RETURNED_INFRA, True),
    (_O.RETURNED_NORMAL, _O.RECLAIMED, False),
    (_O.RETURNED_NORMAL, _O.RETURNED_INFRA, False),
    (_O.RETURNED_INFRA, _O.RECLAIMED, True),
    (_O.RETURNED_INFRA, _O.RETURNED_NORMAL, True),
])
def test_fix_leg_classification_actual_reply_wins_over_reclaim_and_a_return_is_final(first, late, machine):
    from tests.fakes.accounted_workflow import seed_infra_outcome
    _, files, _, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    run = seed_infra_outcome(store, 5, first)
    store.record_fix_infra_return(run, late, files._workflow_authority.writer)
    store.record_fix_infra_return(run, late, files._workflow_authority.writer)
    assert machine_ends(files) == [machine]


@pytest.mark.parametrize("payload", [None, "retired"])
def test_fix_leg_without_a_normal_return_receipt_is_a_machine_end(payload):
    """A run ended with no receipt never returned normally; a receipt written
    before the infra ceiling was deleted is read by its outcome value only."""
    import json
    from tests.fakes.accounted_workflow import seed_infra_outcome
    _, files, _, _ = active_ci_workflow(AgentResult(is_error=False, cost_usd=.01))
    store = files._workflow_db
    seed_infra_outcome(store, 5, _O.RETURNED_NORMAL)
    run = seed_infra_outcome(store, 5, _O.RETURNED_NORMAL)
    with store.transaction() as db:
        if payload is None:
            db.execute("DELETE FROM dedupe_keys WHERE namespace='fix-infra-outcome' AND key=?", (str(run),))
        else:
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='fix-infra-outcome' AND key=?", (str(run),)).fetchone()
            wire = json.loads(row[0])
            wire["value"]["fields"].update({"outcome": {"type": "FixInfraOutcome", "value": "deterministic_death"},
                                            "failures": 5, "episode": None, "charge": 5})
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='fix-infra-outcome' AND key=?", (json.dumps(wire), str(run)))
    assert machine_ends(files) == [False, True]
    store.record_fix_infra_return(run, _O.RETURNED_NORMAL, files._workflow_authority.writer)
    assert machine_ends(files) == ([False, False] if payload is None else [False, True])


def test_actual_normal_return_ends_the_machine_streak_and_settles_unreadable_spend_as_estimate():
    from tests.fakes.accounted_workflow import seed_infra_outcome
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed",
        usage={"input_tokens":"malformed"}))
    seed_infra_outcome(files._workflow_db, 5, cc.FixInfraOutcome.RETURNED_INFRA)
    asyncio.run(workflow.fix_ci(100))
    assert len(inner.tasks) == 1
    assert machine_ends(files) == [False, True]
    with files._workflow_db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges WHERE amount_micros>0").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 0
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges WHERE run_id=?",
                                (inner.tasks[0].env["ISSUE_AGENT_RUN_ID"],)).fetchone()) == (0, "estimate")
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 1


@pytest.mark.parametrize("changed_head", [None, "", "another-head"])
def test_actual_ci_purchase_cannot_bypass_owed_remediation_with_paired_head(monkeypatch, changed_head):
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.adapters.control_store import ReservationResult, AccountingDisposition
    from tests.adapters.test_effects import Gateway
    from issue_agent.domain.actions import RerunFailedChecks
    from issue_agent.domain import continuation_components as cc
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    workflow.gateway.prs[100] = replace(workflow.gateway.prs[100], head_ref_oid="head-1")
    store = files._workflow_db
    gateway = Gateway()
    gateway.ci_state = "failure"
    effects = GitHubEffectExecutor(gateway, store, repository="o/r", identity="o/r:fix")
    store.enqueue_command(effects._command(RerunFailedChecks(100, "head-1", (42,))))
    requests = []
    original = store.reserve
    def capture(request):
        requests.append(request)
        return ReservationResult(AccountingDisposition.UNAVAILABLE, None, request.cap, request.cap, request.cap, "capture")
    with monkeypatch.context() as patch:
        patch.setattr(store, "reserve", capture)
        asyncio.run(workflow.fix_ci(100))
    (requested,) = requests
    if changed_head is not None:
        requested = replace(requested, continuations=tuple(replace(item, head=changed_head)
            if isinstance(item, cc.PRRepairPurchase) else item for item in requested.continuations))
    assert original(requested).disposition is AccountingDisposition.UNAVAILABLE
    assert inner.tasks == []
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_lifetime_issue_cap_refusal_escalates_for_a_cap_decision_without_paid_debit(monkeypatch, tmp_path):
    from issue_agent.adapters.store import Store
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.domain.contracts import Money
    from tests.fakes import accounted_workflow
    original = accounted_workflow.AccountingRuntime
    def limited(*args, **kwargs):
        return replace(original(*args, **kwargs), issue_cap=Money("USD", 0))
    monkeypatch.setattr(accounted_workflow, "AccountingRuntime", limited)
    files = Store(str(tmp_path / "state"))
    workflow, files, inner, _ = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01), files)
    before = files._workflow_db.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    asyncio.run(workflow.fix_ci(100))
    reopened = SQLiteControlStore(files._workflow_db.path)
    state = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(5))).component.state
    assert state.ci_failures == before.component.state.ci_failures
    assert state.consumed_reply_at == before.component.state.consumed_reply_at
    assert inner.tasks == []
    labels = {add for call in workflow.gateway.calls if call[:3] == ("relabel", "pr", 100) for add in call[4:] if add}
    assert {"ai-blocked", "needs-budget-review"} <= labels
    with reopened.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


def test_dashboard_recovery_projection_reads_same_selected_debt_as_admission():
    from issue_agent.app.dashweb.resolve_control import recovery_control
    workflow, files, inner, key = active_ci_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    asyncio.run(workflow.fix_ci(100))
    issue = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    view = recovery_control(issue).model_dump()
    assert view["status"] == "available" and view["family"] == "issue_recovery"
    assert view["ci_failures"] == issue.component.state.ci_failures == 1
    assert view["version"] == issue.revision.version
    pr = recovery_control(files._workflow_authority.read_pr_repair(key)).model_dump()
    assert pr["family"] == "pr_repair" and "ci_failures" not in pr
    assert pr["pending_effects"] == []
    inactive = recovery_control(cc.InactivePRRepair()).model_dump()
    assert inactive["status"] == "inactive" and "ci_failures" not in inactive
