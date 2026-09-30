"""S3 construction paths share production transactions before the S4 reader cutover."""

from dataclasses import replace
from datetime import timedelta
from itertools import count
import sqlite3

import pytest

from issue_agent.adapters import review_execution_control, review_workflow_control, workflow_rows
from issue_agent.adapters.control_store import load_bound_review_submission
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row, persist_workflow_snapshot
from issue_agent.domain.events import ControlTransitionCommitted, EventEnvelope, Producer, new_event_id
from issue_agent.domain.review_execution import (
    CurrentReviewExecution, CopiedReviewProgress, PaidReviewProgress, ReviewExecutionGenerationBoundary,
    ReviewRoundFailure, ReviewInfrastructureDeath, ReviewUnlaunchedRound, ReviewExecutionOriginKind,
    ReviewRoundCancelled, ReviewCancellationReason,
)
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review_evidence import ReviewEvidenceKind, review_evidence_gate_kind
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.review_rounds import (
    CurrentStaleReviewProductivitySettlement, ReviewSubmissionKind, DeferredReviewPreparation,
)
from issue_agent.domain.review_workflow import CountingRule
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.review_workflow_policy import record_review
from issue_agent.domain.workflow import (
    WorkflowCommitted, WorkflowChanged, WorkflowRejected, RequestRearm, CancelRearm,
    ContinueGrantedRearm, KeepGuidance, OperatorActor,
)
from tests.adapters.test_review_execution_control import OWNER, rig, current
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_admission import request as admission_request
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_preparation_finalization import stopped_round
from tests.adapters.test_review_productive_settlement import confirmed_copy, confirmed_escalation, workflow_for


def bind_current_events(store, key, monkeypatch):
    with store.transaction() as db:
        current = workflow_rows.load_review_workflow_row(db, key)
    published = []
    sequence = count()
    producer = Producer("construction", "o/r", "review", 123, "boot")

    def factory(payload, correlation):
        index = next(sequence)
        return EventEnvelope(payload, new_event_id(store.now()), index, store.now(), store.now(),
                             index, producer, correlation)

    store.bind_event_outbox(factory, published.append)
    return current, published


@pytest.fixture(params=("copied", "paid"))
def current_verdict(rig, monkeypatch, request):
    if request.param == "copied":
        store, gateway, selection, control, plan, command = confirmed_copy(rig)
    else:
        store, _, gateway, selection, control = rig
        plan = control.preflight(selection)
        planned = stage(plan)
        control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
        proposed = admission_request(planned.children[0])
        assert store.reserve(proposed).admitted
        register(store, proposed)
        store.settle_review_return(settlement(proposed), returned(store, proposed))
        command = command_for(current(store, selection), patch_id="0" * 64)
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.PAID)
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
        store.begin_command(command.command_id)
        store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                          EffectStatus.SUCCEEDED, remote_object_id="review:current-paid"))
        plan = control.read_round_facts(plan.round.round_id).plan
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    return store, key, control, workflow_for(store, gateway), plan, command, before, published


def test_current_verdict_records_baseline_resets_allowance_and_retries_once(current_verdict):
    store, key, control, workflow, plan, command, before, published = current_verdict
    assert before.execution.state.failures == 3 and before.execution.state.pin_deferrals == 2
    result = workflow.record_review(command.command_id)
    assert isinstance(result, WorkflowCommitted)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.episode.continuation.source.command_id == command.command_id
        assert after.execution.state.failures == 0 and after.execution.state.pin_deferrals == 0
        assert after.execution.state.productive_sequence == plan.round.sequence
        assert after.execution.baseline.through_sequence == plan.round.sequence
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == int(bool(plan.children))
        if plan.children:
            assert db.execute("SELECT SUM(amount_micros) FROM charges").fetchone()[0] == 12
    events = tuple(published)
    assert isinstance(control.finalize_completed_round(plan.round.round_id).outcome.disposition,
                      PaidReviewProgress if plan.children else CopiedReviewProgress)
    assert workflow.record_review(command.command_id).idempotent
    assert tuple(published) == events
    assert sum(isinstance(event.payload, ControlTransitionCommitted)
               and event.payload.entity_id == plan.round.round_id for event in events) == 1


@pytest.mark.parametrize("source", ["refused", "infrastructure", "no-progress"])
def test_current_completed_sources_preserve_distinct_allowance_and_real_spend(rig, monkeypatch, source):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    proposed_stage = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed_stage)
    proposed = admission_request(proposed_stage.children[0], cap=59 if source == "refused" else 100)
    if source == "refused":
        assert store.record_review_admission_refusal(proposed) is not None
    else:
        assert store.reserve(proposed).admitted
        register(store, proposed)
        reason = ExitReason.TRANSPORT_DEATH if source == "infrastructure" else ExitReason.AGENT_ERROR
        store.settle_review_return(settlement(proposed), returned(
            store, proposed, result=AgentResult(True, status="error", exit_reason=reason),
        ))
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    result = control.finalize_completed_round(plan.round.round_id)
    expected = {"refused": ReviewUnlaunchedRound, "infrastructure": ReviewInfrastructureDeath,
                "no-progress": ReviewRoundFailure}[source]
    assert isinstance(result.outcome.disposition, expected)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.execution.state.failures == before.execution.state.failures + int(source == "no-progress")
        assert after.execution.state.pin_deferrals == before.execution.state.pin_deferrals
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == int(source != "refused")
        if source != "refused":
            assert db.execute("SELECT SUM(amount_micros) FROM charges").fetchone()[0] == 12
    events = tuple(published)
    assert control.finalize_completed_round(plan.round.round_id) == result
    assert tuple(published) == events


def test_current_runtime_preparation_closure_and_visible_hold_commit_together(stopped_round, monkeypatch):
    store, selection, control, _, child, _, _ = stopped_round
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    result = control.finalize_completed_round(child.round.round_id)
    assert isinstance(result, DeferredReviewPreparation)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.execution.state.failures == before.execution.state.failures
        assert any(review_evidence_gate_kind(gate) is ReviewEvidenceKind.RUNTIME_PREPARATION
                   for gate in after.gates)
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-preparation'").fetchone()[0] == 1
    events = tuple(published)
    assert control.finalize_completed_round(child.round.round_id) == result
    assert tuple(published) == events


def test_current_unsupported_renewed_origin_remains_held_with_its_records_intact(current_verdict):
    store, key, _, _, _, _, before, _ = current_verdict
    with store.transaction() as db:
        unsupported = replace(before, execution=replace(before.execution, origin=replace(
            before.execution.origin, kind=ReviewExecutionOriginKind.RENEWED,
        )))
        persist_workflow_snapshot(db, unsupported)
        count_before = db.total_changes
        with pytest.raises(workflow_rows.WorkflowAuthorityError, match="origin"):
            load_review_workflow_row(db, key)
        assert db.total_changes == count_before
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds").fetchone()[0] == 1


@pytest.mark.parametrize("source", ["submission_marker", "effect", "outcome"])
def test_current_progress_requires_its_selected_exact_settlement_sources(current_verdict, source):
    store, key, _, workflow, plan, command, _, _ = current_verdict
    assert isinstance(workflow.record_review(command.command_id), WorkflowCommitted)
    with store.transaction() as db:
        if source == "effect":
            db.execute("DELETE FROM effect_receipts WHERE command_id=?", (str(command.command_id),))
        else:
            namespace, identity = ("review-recorded-submission", str(command.command_id)) if source == "submission_marker" else (
                "review-round-outcome", plan.round.round_id,
            )
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND scope='o/r' AND key=?", (namespace, identity))
        changed = db.total_changes
        with pytest.raises(workflow_rows.WorkflowAuthorityError):
            load_review_workflow_row(db, key)
        assert db.total_changes == changed


@pytest.mark.parametrize("kind", ["ceiling", "superseded", "verdict"])
def test_current_escalation_paths_settle_exact_marker_timestamps_and_effects(rig, monkeypatch, kind):
    store, gateway, selection, control, plan, command = confirmed_escalation(
        rig, cause="ceiling" if kind == "ceiling" else "verdict",
    )
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    now = store.now() + timedelta(microseconds=999)
    store._now = lambda: now
    if kind == "superseded":
        gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    workflow = review_workflow_control.SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    result = workflow.settle_submission(command.command_id)
    assert isinstance(result, WorkflowRejected if kind == "superseded" else WorkflowCommitted)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.execution.state.failures == 0 and after.execution.state.pin_deferrals == 0
        assert after.execution.state.productive_sequence == plan.round.sequence
        if kind in ("superseded", "ceiling"):
            assert after.episode.continuation == before.episode.continuation
        else:
            assert after.episode.continuation.source.command_id == command.command_id
        if kind == "ceiling":
            assert after.gates
    events = tuple(published)
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round
    assert workflow.settle_submission(command.command_id) is None
    assert tuple(published) == events


@pytest.mark.parametrize("planned_child", [False, True])
def test_current_unpaid_abandonment_closes_without_spend_or_allowance_changes(rig, monkeypatch, planned_child):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    if planned_child:
        control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    result = control.finalize_abandoned_round(plan.round.round_id)
    assert result.outcome.disposition == ReviewRoundCancelled(
        ReviewCancellationReason.UNPAID_ABANDONMENT, control.read_round_facts(plan.round.round_id).closure_receipt,
    )
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.execution.state == before.execution.state
        assert after.execution.baseline.through_sequence == plan.round.sequence
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
    events = tuple(published)
    assert control.finalize_abandoned_round(plan.round.round_id) == result
    assert tuple(published) == events


@pytest.mark.parametrize("stage", ["receipt", "state", "event"])
def test_caught_inner_productive_failure_cannot_commit_the_rejection_half(current_verdict, stage):
    store, key, _, workflow, plan, command, before, published = current_verdict
    with store.transaction() as db:
        prior_events = db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        proposed = record_review(before, load_bound_review_submission(db, command.command_id))
        assert isinstance(proposed, WorkflowChanged)
        table, operation, condition = {
            "receipt": ("dedupe_keys", "INSERT", "WHEN NEW.namespace='review-round-outcome'"),
            "state": ("workflows", "UPDATE", f"WHEN NEW.version={before.revision.version + 2}"),
            "event": ("event_outbox", "INSERT", "WHEN NEW.event_json LIKE '%outcome_finalized%'"),
        }[stage]
        db.execute(f"CREATE TEMP TRIGGER fail_current_productivity BEFORE {operation} ON {table} "
                   f"{condition} BEGIN SELECT RAISE(ABORT,'productive inner failure'); END")
        db.execute("INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
                   ("test-outer", "o/r", "before", "{}", store.now().isoformat()))
        with pytest.raises(sqlite3.IntegrityError, match="productive inner failure"):
            workflow._commit(db, before.revision, (before.authority,), proposed)
        assert load_review_workflow_row(db, key) == before
        assert db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                          (plan.round.round_id,)).fetchone()[:] == (None, None)
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace IN "
                          "('review-recorded-submission','review-round-outcome','workflow-transition')").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0] == prior_events
        db.execute("INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
                   ("test-outer", "o/r", "after", "{}", store.now().isoformat()))
    with store.transaction() as db:
        assert load_review_workflow_row(db, key) == before
        assert db.execute("SELECT COUNT(*) FROM level_state WHERE namespace='test-outer'").fetchone()[0] == 2
    assert published == []
    assert isinstance(workflow.record_review(command.command_id), WorkflowCommitted)
    with store.transaction() as db:
        recovered = load_review_workflow_row(db, key)
        assert recovered.episode.continuation.source.command_id == command.command_id
        assert recovered.execution.state.failures == 0
        assert recovered.execution.state.productive_sequence == plan.round.sequence


def advance_four_generations(store, key, workflow, before):
    actor = OperatorActor("operator", "test")
    request_id = "rearm:current:0"
    assert isinstance(workflow.request_rearm(
        RequestRearm(request_id, before.revision, before.gates[0].gate_id,
                     KeepGuidance(), expected_binding=before.binding), actor,
    ), WorkflowCommitted)
    for index in range(1, 4):
        with store.transaction() as db:
            pending = load_review_workflow_row(db, key)
        assert isinstance(workflow.cancel_rearm(
            CancelRearm(f"cancel:current:{index}", pending.revision, request_id, "stop",
                        expected_binding=pending.binding), actor,
        ), WorkflowCommitted)
        with store.transaction() as db:
            stopped = load_review_workflow_row(db, key)
        next_request = f"rearm:current:{index}"
        assert isinstance(workflow.continue_rearm(
            ContinueGrantedRearm(next_request, stopped.revision, request_id,
                                  expected_binding=stopped.binding), actor,
        ), WorkflowCommitted)
        request_id = next_request


def test_current_stale_productivity_after_four_generations_uses_exact_witness_without_reset(rig, monkeypatch):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    later_selection = replace(selection, request_id="preflight:later", expected=current(store, selection).revision)
    _, _, _, _, productive, escalation = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    command = command_for(current(store, selection))
    control.register_submission(old.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(old.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:old-current"))
    workflow = review_workflow_control.SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(escalation.command_id), WorkflowCommitted)
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    advance_four_generations(store, key, workflow, before)
    with store.transaction() as db:
        advanced = load_review_workflow_row(db, key)
        assert advanced.revision.fence.generation == before.revision.fence.generation + 4
        boundaries = tuple(decode_workflow(row[0], ReviewExecutionGenerationBoundary) for row in db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation' AND scope='o/r'",
        ))
        boundaries = tuple(sorted(boundaries, key=lambda boundary: boundary.to_revision.fence.generation))
        assert len(boundaries) == 4
        assert boundaries[0].applied_sequences == (productive.round.sequence,)
        assert all(boundary.applied_sequences == () for boundary in boundaries[1:])
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    with store.transaction() as db:
        settled = load_review_workflow_row(db, key)
        assert settled.execution == advanced.execution
        assert settled.episode == advanced.episode
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                         "AND scope='o/r' AND key=?", (old.round.round_id,)).fetchone()
        witness = decode_workflow(row[0], CurrentStaleReviewProductivitySettlement)
        assert witness.receipt.outcome.round == old.round
        assert witness.command_id == str(command.command_id)
        assert witness.settled_binding == advanced.execution.binding
    events = tuple(published)
    assert control.finalize_completed_round(old.round.round_id).outcome.round == old.round
    assert workflow.settle_submission(command.command_id) is None
    assert tuple(published) == events


def test_current_late_paid_return_after_four_generations_preserves_one_charge_and_current_allowance(rig, monkeypatch):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    proposed_stage = stage(old)
    control.register_stage(old.round.round_id, current(store, selection).revision, proposed_stage)
    proposed = admission_request(proposed_stage.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    control.seal_completed_planning(old.round.round_id, current(store, selection).revision)
    payment = settlement(proposed)
    # The acceptance barrier requires settled budgets; delayed output can arrive after reconciliation.
    assert not store.settle(payment).idempotent
    assert store.returned_review_child("o/r", proposed.run_id) is None
    later_selection = replace(selection, request_id="preflight:after-recovery",
                              expected=current(store, selection).revision)
    _, _, _, _, _, escalation = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    workflow = review_workflow_control.SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(escalation.command_id), WorkflowCommitted)
    key = selection.expected.fence.key
    before, published = bind_current_events(store, key, monkeypatch)
    advance_four_generations(store, key, workflow, before)
    with store.transaction() as db:
        advanced = load_review_workflow_row(db, key)
    delayed = returned(store, proposed, result=AgentResult(True, status="error", exit_reason=ExitReason.AGENT_ERROR))
    assert store.settle_review_return(payment, delayed).idempotent
    assert store.returned_review_child("o/r", proposed.run_id) == delayed
    assert store.snapshot(proposed.scope).committed.micros == 12
    assert store.snapshot(proposed.scope).reserved.micros == 0
    with store.transaction() as db:
        assert load_review_workflow_row(db, key) == advanced
    result = control.finalize_completed_round(old.round.round_id)
    assert isinstance(result.outcome.disposition, ReviewRoundFailure)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after.execution == advanced.execution and after.episode == advanced.episode
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (1, 12)
    events = tuple(published)
    assert store.settle_review_return(payment, delayed).idempotent
    assert control.finalize_completed_round(old.round.round_id) == result
    assert tuple(published) == events
    assert store.snapshot(proposed.scope).committed.micros == 12


@pytest.mark.parametrize("missing_marker", [False, True])
@pytest.mark.parametrize("marker_revision", ["precondition", "joint", "future"])
def test_stopped_conversion_requires_legacy_live_productivity_joint_marker(current_verdict, missing_marker, marker_revision):
    from issue_agent.adapters.workflow_rows import convert_legacy_review_execution, read_review_outcome_receipt
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.review_execution import ActivatedReviewExecution
    from issue_agent.domain.review_rounds import CurrentReviewOutcomeReceipt

    store, key, _, workflow, plan, command, before, _ = current_verdict
    assert isinstance(workflow.record_review(command.command_id), WorkflowCommitted)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        receipt = read_review_outcome_receipt(db, key.repository, plan.round.round_id)
        assert isinstance(receipt, CurrentReviewOutcomeReceipt)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-outcome' AND scope=? AND key=?",
                   (encode_workflow((receipt.facts, receipt.outcome)), key.repository, plan.round.round_id))
        if marker_revision != "precondition":
            marker = after.revision if marker_revision == "joint" else replace(after.revision, version=after.revision.version + 1)
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-recorded-submission' AND key=?",
                       (encode_workflow(marker), str(command.command_id)))
        held = replace(after, execution=ActivatedReviewExecution(
            before.execution.origin, before.execution.baseline, after.execution.state, after.execution.last_sequence,
        ))
        persist_workflow_snapshot(db, held)
        if missing_marker:
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-recorded-submission' AND key=?",
                       (str(command.command_id),))
            retained = tuple(db.iterdump())
            with pytest.raises(WorkflowAuthorityError, match="joint settlement"):
                convert_legacy_review_execution(db, key)
            assert tuple(db.iterdump()) == retained
        elif marker_revision == "future":
            retained = tuple(db.iterdump())
            with pytest.raises(WorkflowAuthorityError, match="concurrency precondition"):
                convert_legacy_review_execution(db, key)
            assert tuple(db.iterdump()) == retained
        else:
            converted = convert_legacy_review_execution(db, key)
            assert converted.execution.state == after.execution.state
            assert load_review_workflow_row(db, key) == converted
