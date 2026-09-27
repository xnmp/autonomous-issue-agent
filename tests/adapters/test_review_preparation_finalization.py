"""A stopped local runtime leaves one durable, bounded review preparation hold."""

from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import REVIEW_PREPARATION_NAMESPACE, WorkflowAuthorityError
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.review_evidence import ReviewEvidenceKind, review_evidence_gate_kind
from issue_agent.domain.review_execution import ReviewUnlaunchedRound
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.review_return import ReturnedReviewChild
from issue_agent.domain.review_rounds import DeferredReviewPreparation
from issue_agent.domain.workflow import EvidenceUnavailable, OpenGate
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_control import OWNER, Gateway, current, dump, rig
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_preflight_stop import stop


@pytest.fixture
def stopped_round(rig):
    store, _, gateway, selection, _ = rig
    clock = [store.now()]
    store._now = lambda: clock[0]
    probe = ["Codex executable is unavailable"]
    writer = WorkflowWriter("o/r", "review", OWNER)
    workflow = SQLiteReviewWorkflowControl(
        store, writer, gateway=gateway, ceiling=3, trigger_label=Label.AI_PR_OPEN,
        rule=CountingRule("review-bot[bot]"), runtime_preflight=lambda: probe[0],
    )
    control = SQLiteReviewExecutionControl(store, writer, gateway, preparation_control=workflow)
    selection = replace(selection, runtime=RuntimeName.CODEX)
    plan = control.preflight(selection)
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed)
    child = proposed.children[0]
    reservation = request(child)
    reservation = replace(reservation, dispatch_id=json.dumps(json.loads(reservation.dispatch_id) | {"runtime": "codex"}))
    assert store.reserve(reservation).admitted
    stopped = stop(store, reservation)
    returned = ReturnedReviewChild(child, str(reservation.run_id), OWNER, RuntimeName.CODEX,
                                   stopped.result, store.now())
    assert store.settle_review_preflight_stop(returned) is not None
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    return store, selection, control, workflow, child, clock, probe


def test_verified_runtime_stop_finalizes_without_strike_and_opens_visible_hold(stopped_round):
    store, selection, control, workflow, child, _, _ = stopped_round
    before_failures = current(store, selection).execution.state.failures

    result = control.finalize_completed_round(child.round.round_id)

    assert isinstance(result, DeferredReviewPreparation)
    assert isinstance(result.outcome.disposition, ReviewUnlaunchedRound)
    assert current(store, selection).execution.state.failures == before_failures
    gate = next(g for g in current(store, selection).gates
                if review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION)
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
    assert "Codex executable is unavailable" in gate.cause.last_error
    assert not workflow.evidence_due(selection.expected.fence.key, "b" * 40,
                                     ReviewEvidenceKind.RUNTIME_PREPARATION)
    facts = control.read_round_facts(child.round.round_id)
    with store.transaction() as db:
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND key=?",
                             (REVIEW_PREPARATION_NAMESPACE, child.round.round_id)).fetchone()
        assert receipt is not None
        assert decode_workflow(receipt[0], tuple) == (facts, result.failures)
    before = dump(store)
    assert control.finalize_completed_round(child.round.round_id) == result
    assert dump(store) == before


def test_departed_sealed_parent_preserves_the_same_preparation_hold(stopped_round):
    store, selection, control, _, child, _, _ = stopped_round
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(child.round.round_id)
    before_failures = current(store, selection).execution.state.failures

    result = control.finalize_abandoned_round(child.round.round_id)

    assert isinstance(result, DeferredReviewPreparation)
    assert current(store, selection).execution.state.failures == before_failures
    assert any(review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION
               for g in current(store, selection).gates)


def test_runtime_hold_releases_only_after_due_successful_local_preflight(stopped_round):
    store, selection, control, workflow, child, clock, probe = stopped_round
    control.finalize_completed_round(child.round.round_id)
    probe[0] = None

    assert workflow.reconcile_holds() == 0
    assert any(review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION
               for g in current(store, selection).gates)

    clock[0] += timedelta(minutes=2)
    assert workflow.reconcile_holds() >= 1
    assert all(review_evidence_gate_kind(g) is not ReviewEvidenceKind.RUNTIME_PREPARATION
               for g in current(store, selection).gates)


def test_due_runtime_failure_keeps_hold_and_backs_off(stopped_round):
    store, selection, control, workflow, child, clock, probe = stopped_round
    control.finalize_completed_round(child.round.round_id)
    clock[0] += timedelta(minutes=2)
    probe[0] = "Codex authentication is unavailable"

    assert workflow.reconcile_holds() == 0
    gate = next(g for g in current(store, selection).gates
                if review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION)
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
    assert gate.cause.failed_reads == 2
    assert gate.cause.last_error == probe[0]
    assert not workflow.evidence_due(selection.expected.fence.key, "b" * 40,
                                     ReviewEvidenceKind.RUNTIME_PREPARATION)


def test_gate_write_failure_rolls_back_closure_projection_and_preparation_receipt(stopped_round):
    store, selection, control, _, child, _, _ = stopped_round
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_preparation BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-runtime-preparation-evidence' "
                   "BEGIN SELECT RAISE(ABORT,'preparation refused'); END")
    before = dump(store)

    with pytest.raises(sqlite3.IntegrityError, match="preparation refused"):
        control.finalize_completed_round(child.round.round_id)

    assert dump(store) == before
    assert current(store, selection).execution.state.failures == 3


def test_preparation_requires_a_bounded_reconciliation_reader(stopped_round):
    store, selection, _, _, child, _, _ = stopped_round
    uncomposed = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", OWNER), Gateway())
    before = dump(store)

    with pytest.raises(WorkflowAuthorityError, match="bounded evidence controller"):
        uncomposed.finalize_completed_round(child.round.round_id)

    assert dump(store) == before
    assert current(store, selection).execution.state.failures == 3


def test_preparation_refuses_controller_without_a_local_reader(stopped_round):
    store, _, _, _, _, _, _ = stopped_round
    writer = WorkflowWriter("o/r", "review", OWNER)
    gateway = Gateway()
    workflow = SQLiteReviewWorkflowControl(
        store, writer, gateway=gateway, ceiling=3, trigger_label=Label.AI_PR_OPEN,
        rule=CountingRule("review-bot[bot]"),
    )

    with pytest.raises(ValueError, match="local reader"):
        SQLiteReviewExecutionControl(store, writer, gateway, preparation_control=workflow)


def test_stale_successful_probe_cannot_clear_newer_runtime_failure(stopped_round):
    store, selection, control, _, child, clock, _ = stopped_round
    control.finalize_completed_round(child.round.round_id)
    clock[0] += timedelta(minutes=2)
    writer = WorkflowWriter("o/r", "review", OWNER)
    gateway = Gateway()
    replacement = SQLiteReviewWorkflowControl(
        store, writer, gateway=gateway, ceiling=3, trigger_label=Label.AI_PR_OPEN,
        rule=CountingRule("review-bot[bot]"),
        runtime_preflight=lambda: None,
    )

    def raced_probe():
        replacement.record_evidence(selection.expected.fence.key, "b" * 40,
                                    ReviewEvidenceKind.RUNTIME_PREPARATION,
                                    error="newer verified runtime failure")
        return None

    workflow = SQLiteReviewWorkflowControl(
        store, writer, gateway=gateway, ceiling=3, trigger_label=Label.AI_PR_OPEN,
        rule=CountingRule("review-bot[bot]"), runtime_preflight=raced_probe,
    )

    assert workflow.reconcile_holds() == 0
    gate = next(g for g in current(store, selection).gates
                if review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION)
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
    assert gate.cause.last_error == "newer verified runtime failure"


def test_stale_head_read_cannot_clear_new_head_runtime_failure(stopped_round):
    store, selection, control, _, child, clock, _ = stopped_round
    control.finalize_completed_round(child.round.round_id)
    clock[0] += timedelta(minutes=2)
    writer = WorkflowWriter("o/r", "review", OWNER)
    gateway = Gateway()
    gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    workflow = SQLiteReviewWorkflowControl(
        store, writer, gateway=gateway, ceiling=3, trigger_label=Label.AI_PR_OPEN,
        rule=CountingRule("review-bot[bot]"), runtime_preflight=lambda: None,
    )
    old_state = current(store, selection)

    def raced_read():
        gateway.on_pr = None
        workflow.record_evidence(selection.expected.fence.key, "d" * 40,
                                 ReviewEvidenceKind.RUNTIME_PREPARATION,
                                 error="new head runtime failure")

    gateway.on_pr = raced_read
    assert workflow._reread_evidence(old_state, ReviewEvidenceKind.RUNTIME_PREPARATION) == (False, "")
    gate = next(g for g in current(store, selection).gates
                if review_evidence_gate_kind(g) is ReviewEvidenceKind.RUNTIME_PREPARATION)
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
    assert gate.cause.last_error == "new head runtime failure"
