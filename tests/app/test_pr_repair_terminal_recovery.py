"""Launched PR repairs that end without a captured return (issue #240).

Reproduces the Tauri cancelled-REBASE and Ballast accounting-unavailable
ADDRESS_REVIEW strands through the real control store, accounting runtime
and fix workflow; only the model runtime is fake.
"""
import asyncio
from dataclasses import replace

import pytest

from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import RunId
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Review
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
from tests.app.test_pr_repair_workflow import active_ci_workflow, issue_ci_state

OWNER = "o/r:fix:fixture"
ISSUE = WorkflowKey("o/r", IssueRef(5))


def _pending(state, lane):
    return {"rebase": state.pending_rebase, "review": state.review_failures.pending, "ci": state.pending_ci}[lane]


def _failures(state, lane):
    return {"rebase": state.rebase_failures, "review": state.review_failures.count, "ci": state.ci_failures}[lane]


def _workflow(lane):
    workflow, files, inner, key = active_ci_workflow(AgentResult(
        is_error=False, status="pushed", cost_usd=.01, session_id=f"next-{lane}"))
    pr = replace(workflow.gateway.prs[100], head_ref_oid="a" * 40)
    if lane == "review":
        pr = replace(pr, reviews=(Review("CHANGES_REQUESTED", "reviewer", "2026-10-02T00:00:01Z", body="fix it"),))
    workflow.gateway.prs[100] = pr
    return workflow, files, inner, key


def _repair(workflow, lane):
    method = {"rebase": workflow.rebase, "review": workflow.address_review, "ci": workflow.fix_ci}[lane]
    asyncio.run(method(100))


def _launch_and_lose_return(monkeypatch, workflow, files, inner, lane, during=None):
    """Launch paid work, then die before any repair result is captured."""
    store = files._workflow_db
    launched = []

    async def lost(task):
        run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
        launched.append(run_id)
        store.register_process_lease(run_id, expected_owner_id=OWNER, pid=123, pgid=123, process_start_ticks=99)
        if during is not None:
            during(run_id)
        raise RuntimeError("runtime cancelled after paid launch")

    with monkeypatch.context() as patch:
        patch.setattr(inner, "run", lost)
        with pytest.raises(RuntimeError, match="cancelled after paid launch"):
            _repair(workflow, lane)
    assert len(launched) == 1
    return launched[0]


def _dump(store):
    with store.transaction() as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize("lane", ["rebase", "review", "ci"])
def test_terminal_launched_repair_without_return_is_acknowledged_after_money_disposition(monkeypatch, lane):
    workflow, files, inner, key = _workflow(lane)
    store, control = files._workflow_db, files._workflow_authority
    before = issue_ci_state(files)
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, lane)
    stuck = issue_ci_state(files)
    assert _pending(stuck, lane) is not None and _pending(stuck, lane).attempt.run_id == run_id
    if lane == "review":
        # Ballast: launched return lost with billing unknown.
        assert store.record_accounting_unavailable(run_id, "provider usage ambiguous")

    # Money is still active: operational recovery must keep refusing.
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    _repair(workflow, lane)
    assert inner.tasks == [] and _pending(issue_ci_state(files), lane).attempt.run_id == run_id

    # Tauri: explicit disposition cancels the run and releases its money.
    assert store.release(run_id, "operator disposition").disposition.value == "available"
    with store.transaction() as db:
        run = db.execute("SELECT state,outcome FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        assert tuple(run) == ("cancelled", "released")
    infra = stuck.infra_failures

    assert control.reconcile_pr_repair_returns() == 1
    after = issue_ci_state(files)
    assert _pending(after, lane) is None
    # The launched attempt remains debt; no other history is rewritten.
    assert _failures(after, lane) == _failures(before, lane) + 1
    assert after.infra_failures == infra and after.infra_episode == stuck.infra_episode
    repair = control.read_pr_repair(key)
    assert repair.component.state.session is None and repair.component.state.actual_model is None
    assert repair.component.watermarks.last_applied == repair.component.watermarks.last_admitted
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM budget_reservations WHERE run_id=?", (str(run_id),)).fetchone()[0] == "released"
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND key=?",
            (store._continuation_receipt_key(run_id, repair.binding.authority.family),)).fetchone()[0]
    acknowledged = decode_workflow(receipt, tuple)
    assert isinstance(acknowledged[1], cc.PRRepairUnreturned) and acknowledged[1].admission == acknowledged[0]
    assert acknowledged[0].attempt.run_id == run_id

    # Duplicate recovery is a no-op.
    settled = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == settled

    # Ordinary policy now admits and completes the next repair.
    _repair(workflow, lane)
    assert len(inner.tasks) == 1
    final = issue_ci_state(files)
    assert _pending(final, lane) is None and _failures(final, lane) == _failures(before, lane) + 2
    assert control.read_pr_repair(key).component.state.session is not None


def test_running_repair_holding_its_lease_is_never_acknowledged(monkeypatch):
    workflow, files, inner, _ = _workflow("rebase")
    store, control = files._workflow_db, files._workflow_authority
    observed = []

    def reconcile_while_running(run_id):
        held = _dump(store)
        observed.append(control.reconcile_pr_repair_returns())
        assert _dump(store) == held
        assert issue_ci_state(files).pending_rebase.attempt.run_id == run_id

    _launch_and_lose_return(monkeypatch, workflow, files, inner, "rebase", during=reconcile_while_running)
    assert observed == [0]


def test_abandoned_lease_with_retained_money_waits_for_disposition(monkeypatch):
    workflow, files, inner, _ = _workflow("rebase")
    store, control = files._workflow_db, files._workflow_authority

    def die(run_id):
        assert store.reclaim_dead_leases(OWNER, lambda *_: False) == (run_id,)

    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, "rebase", during=die)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM runs WHERE run_id=?", (str(run_id),)).fetchone()[0] == "abandoned"
    assert control.reconcile_pr_repair_returns() == 0
    assert issue_ci_state(files).pending_rebase is not None
    assert store.release(run_id, "operator disposition").disposition.value == "available"
    assert control.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).pending_rebase is None


def test_stale_unreturned_repair_cannot_rewrite_a_newer_generation(monkeypatch):
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow_identity import WorkflowRevision
    workflow, files, inner, key = _workflow("rebase")
    store, control = files._workflow_db, files._workflow_authority
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, "rebase")
    with store.transaction() as db:
        issue = load_issue_aggregate(db, ISSUE)
        persist_workflow_snapshot(db, replace(issue, revision=WorkflowRevision(
            replace(issue.revision.fence, generation=issue.revision.fence.generation + 1), issue.revision.version + 1),
            issue_recovery=replace(issue.issue_recovery, state=replace(issue.issue_recovery.state, rebase_failures=3))))
        pr = load_review_workflow_row(db, key)
        persist_workflow_snapshot(db, replace(pr, revision=WorkflowRevision(
            replace(pr.revision.fence, generation=pr.revision.fence.generation + 1), pr.revision.version + 1)))
    assert store.release(run_id, "operator disposition").disposition.value == "available"
    newer_issue, newer_repair = issue_ci_state(files), control.read_pr_repair(key)
    control.reconcile_pr_repair_returns()
    assert issue_ci_state(files) == newer_issue and control.read_pr_repair(key) == newer_repair


def test_acknowledged_run_refuses_a_late_real_result_and_keeps_newer_work(monkeypatch):
    workflow, files, inner, key = _workflow("rebase")
    store, control = files._workflow_db, files._workflow_authority
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, "rebase")
    assert store.release(run_id, "operator disposition").disposition.value == "available"
    assert control.reconcile_pr_repair_returns() == 1
    _repair(workflow, "rebase")
    newer_issue, newer_repair = issue_ci_state(files), control.read_pr_repair(key)
    assert newer_repair.component.state.session is not None
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    newer = decode_workflow(raw, cc.PRRepairResult)
    with store.transaction() as db:
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND key=?",
            (store._continuation_receipt_key(run_id, newer.admission.attempt.binding.authority.family),)).fetchone()[0]
    stale_admission = decode_workflow(receipt, tuple)[0]
    late = replace(newer, admission=stale_admission,
        settlement=replace(newer.settlement, run_id=run_id),
        state=replace(newer.state, session=replace(newer.state.session, source=stale_admission),
                      actual_model=replace(newer.state.actual_model, source=stale_admission)),
        rebase=replace(newer.rebase, admission=store.rebase_repair_admission(stale_admission.attempt)))
    with pytest.raises(WorkflowAuthorityError):
        store.settle_pr_repair_result(late, control.writer)
    assert issue_ci_state(files) == newer_issue and control.read_pr_repair(key) == newer_repair
    assert control.reconcile_pr_repair_returns() == 0
    assert issue_ci_state(files) == newer_issue and control.read_pr_repair(key) == newer_repair
