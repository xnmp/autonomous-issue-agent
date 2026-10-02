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


def active_ci_workflow(result):
    from dataclasses import replace
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from tests.adapters.test_continuation_admission import active, install_secondary

    gateway = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-remote", closing_issue=5,
        checks=(Check("tests", "fail"),))})
    inner = FakeRunner(result)
    files = FakeStore()
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
def test_review_common_admission_refuses_missing_purchase_or_changed_actual_context(monkeypatch, mutation):
    import json
    workflow, files, inner, key = review_workflow(AgentResult(is_error=False, status="pushed", cost_usd=.01))
    original = files._workflow_db.reserve
    requests = []
    def capture(request):
        requests.append(request)
        from issue_agent.adapters.control_store import ReservationResult, AccountingDisposition
        return ReservationResult(AccountingDisposition.UNAVAILABLE, None, request.cap, request.cap, request.cap, "captured before admission")
    with monkeypatch.context() as patch:
        patch.setattr(files._workflow_db, "reserve", capture)
        asyncio.run(workflow.address_review(100))
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
    assert issue_ci_state(files) == siblings
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


def test_newer_issue_reply_cannot_erase_pending_ci_outcome(monkeypatch):
    from tests.adapters.test_workflow_admission import request
    _, files, _, _, returned = captured_interrupted_ci_result(monkeypatch)
    selected = files._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(5)))
    purchase = cc.IssueRecoveryPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:newer-reply"),
        "2026-10-02T10:00:00+00:00", "fix/5-newer", 20)
    requested = replace(request("newer-reply", binding=selected.binding), owner_id=files._workflow_authority.writer.owner_id)
    assert files._workflow_db.reserve_issue_recovery(requested, purchase).admitted
    assert files._workflow_authority.reconcile_pr_repair_returns() == 1
    state = issue_ci_state(files)
    assert state.ci_failures == 1 and state.resumes == 1 and state.pending_ci is None
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

def test_adopted_pending_result_is_recovered(monkeypatch):
    _,files,_,key,returned = captured_interrupted_ci_result(monkeypatch)
    store = files._workflow_db
    store.adopt_legacy_owner('o/r','fix','o/r:fix:successor',prior_owner_ids=('o/r:fix:fixture',))
    writer = SQLiteWorkflowControl(store,WorkflowWriter('o/r','fix','o/r:fix:successor'),resolve_ceiling=20)
    assert writer.reconcile_pr_repair_returns() == 1
    assert writer.read_pr_repair(key).component.state.session.handle == 'durable'

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

@pytest.mark.parametrize("lane", ["ci", "review"])
def test_caught_paid_settlement_failure_rolls_back_money_and_continuity(monkeypatch, lane):
    import issue_agent.adapters.workflow_rows as rows
    if lane == "ci":
        _,files,_,key,returned=captured_interrupted_ci_result(monkeypatch)
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
