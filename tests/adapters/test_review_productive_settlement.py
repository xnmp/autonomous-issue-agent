"""Confirmed review submissions settle the baseline and outer round together."""

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, load_review_workflow_row, persist_workflow_snapshot,
)
from issue_agent.adapters.control_store import load_bound_review_submission
from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from issue_agent.domain.labels import Label
from issue_agent.domain.review_execution import CopiedReviewProgress, PaidReviewProgress
from issue_agent.domain.review_evidence import ReviewEvidenceKind
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.review_rounds import ReviewSubmissionKind
from issue_agent.domain.review_workflow import FreshReviewRequired
from issue_agent.domain.review_workflow_policy import record_review
from issue_agent.domain.workflow import WorkflowChanged, WorkflowCommitted, WorkflowRejected, WorkflowRejection
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_control_store import NOW
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for


def workflow_for(store, gateway):
    return SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=3,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )


def confirmed_copy(rig, *, seal=True):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    if seal:
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:9"))
    return store, gateway, selection, control, plan, command


def confirmed_escalation(rig, *, cause="verdict", seal=True):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    op = BoundReviewSubmissionOp(9, "COMMENT", "human review needed", "b" * 40,
                                 "review-bot", "c" * 64, cause)
    binding = current(store, selection).binding
    command_id = bound_review_command_id(op, binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                               "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                               workflow=binding)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    if seal:
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:escalated"))
    return store, gateway, selection, control, plan, command


def test_confirmed_copied_verdict_commits_baseline_and_execution_progress_together(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig)
    workflow = workflow_for(store, gateway)

    result = workflow.record_review(command.command_id)

    assert isinstance(result, WorkflowCommitted)
    state = current(store, selection)
    assert state.episode.continuation.source.command_id == command.command_id
    assert state.execution.state.productive_sequence == plan.round.sequence
    assert state.execution.state.failures == 0 and state.execution.state.pin_deferrals == 0
    assert state.execution.state.displayed_round == 0
    assert isinstance(control.finalize_completed_round(plan.round.round_id).outcome.disposition,
                      CopiedReviewProgress)
    before = dump(store)
    assert workflow.record_review(command.command_id).idempotent
    assert dump(store) == before


def test_productive_receipt_failure_rolls_back_baseline_and_execution(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig)
    workflow = workflow_for(store, gateway)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_outcome BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-outcome' "
                   "BEGIN SELECT RAISE(ABORT,'outcome refused'); END")
    before = dump(store)

    with pytest.raises(sqlite3.IntegrityError, match="outcome refused"):
        workflow.record_review(command.command_id)

    assert dump(store) == before
    assert current(store, selection).execution.state.productive_sequence == 0
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_outcome")
    assert isinstance(workflow.record_review(command.command_id), WorkflowCommitted)
    assert current(store, selection).execution.state.productive_sequence == plan.round.sequence


def test_productive_round_requires_its_recorded_baseline_receipt_on_reload(rig):
    store, gateway, selection, _, _, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-recorded-submission' "
                   "AND key=?", (str(command.command_id),))
    before = dump(store)

    with pytest.raises(WorkflowAuthorityError, match="one settlement authority"):
        current(store, selection)

    assert dump(store) == before


def test_confirmed_verdict_waits_for_parent_seal_without_recording_a_baseline(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig, seal=False)
    workflow = workflow_for(store, gateway)
    before = dump(store)

    with pytest.raises(WorkflowAuthorityError, match="sealed, settled productive round"):
        workflow.record_review(command.command_id)

    assert dump(store) == before
    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    assert isinstance(workflow.record_review(command.command_id), WorkflowCommitted)


def test_pending_copied_submission_does_not_establish_progress(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    workflow = workflow_for(store, gateway)
    before = dump(store)

    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before
    assert current(store, selection).execution.state.productive_sequence == 0


def test_confirmed_paid_verdict_resets_once_and_records_eligible_session(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    planned = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
    child = planned.children[0]
    proposed = request(child)
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(store, proposed))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))

    result = workflow_for(store, gateway).record_review(command.command_id)

    assert isinstance(result, WorkflowCommitted)
    state = current(store, selection)
    assert state.execution.state.failures == 0 and state.execution.state.pin_deferrals == 0
    assert state.execution.state.productive_sequence == plan.round.sequence
    assert state.execution.state.displayed_round == 1
    assert isinstance(control.finalize_completed_round(plan.round.round_id).outcome.disposition,
                      PaidReviewProgress)


def test_confirmed_escalated_verdict_projects_hold_and_resets_round_together(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig)

    result = workflow_for(store, gateway).record_review(command.command_id)

    assert isinstance(result, WorkflowCommitted)
    state = current(store, selection)
    assert state.episode.continuation.source.command_id == command.command_id
    assert state.execution.state.productive_sequence == plan.round.sequence
    with store.transaction() as db:
        projected = db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE kind=? AND state='pending'",
                               (CommandKind.RELABEL.value,)).fetchone()[0]
    assert projected == 1


def test_recovery_repairs_a_pre_joint_commit_with_a_recorded_baseline(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig)
    with store.transaction() as db:
        prior = load_review_workflow_row(db, selection.expected.fence.key)
        bound = load_bound_review_submission(db, command.command_id)
        proposal = record_review(prior, bound)
        assert isinstance(proposal, WorkflowChanged)
        persist_workflow_snapshot(db, proposal.snapshot)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-recorded-submission", "o/r", str(command.command_id),
                    store.now().isoformat(), encode_workflow(proposal.snapshot.revision)))
    assert current(store, selection).execution.state.productive_sequence == 0

    sweep = control.reconcile_review_rounds()

    assert plan.round.round_id in sweep.finalized
    assert current(store, selection).execution.state.productive_sequence == plan.round.sequence
    before = dump(store)
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round
    assert dump(store) == before


def _advance_generation_with_open_round(store, selection):
    with store.transaction() as db:
        prior = load_review_workflow_row(db, selection.expected.fence.key)
        fence = replace(prior.revision.fence, generation=prior.revision.fence.generation + 1)
        binding = replace(prior.execution.binding, rejection=replace(prior.binding, fence=fence))
        execution = replace(
            prior.execution,
            checkpoint=replace(prior.execution.checkpoint,
                               state=replace(prior.execution.checkpoint.state, binding=binding)),
            state=replace(prior.execution.checkpoint.state, binding=binding),
        )
        advanced = replace(
            prior, revision=replace(prior.revision, fence=fence, version=prior.revision.version + 1),
            episode=replace(prior.episode, continuation=FreshReviewRequired(fence.generation)),
            execution=execution,
        )
        persist_workflow_snapshot(db, advanced)


def test_stale_confirmed_verdict_closes_round_before_settled_marker_without_reset(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig)
    _advance_generation_with_open_round(store, selection)
    before_state = current(store, selection).execution.state

    result = workflow_for(store, gateway).settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    assert current(store, selection).execution.state == before_state
    assert plan.round.round_id not in control.reconcile_review_rounds().finalized
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                          "AND key=?", (plan.round.round_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 1
    before = dump(store)
    assert workflow_for(store, gateway).settle_submission(command.command_id) is None
    assert dump(store) == before


def test_stale_confirmed_verdict_waits_for_parent_seal_before_marker(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig, seal=False)
    _advance_generation_with_open_round(store, selection)
    workflow = workflow_for(store, gateway)

    assert workflow.settle_submission(command.command_id) is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 0
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    sweep = control.reconcile_review_rounds()
    assert plan.round.round_id in sweep.finalized
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)


def test_superseded_escalated_verdict_closes_productive_round_without_a_baseline(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig)
    gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    store._now = lambda: NOW + timedelta(microseconds=999)
    workflow = workflow_for(store, gateway)
    baseline_before = current(store, selection).episode.continuation

    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_settled BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-submission-settled' "
                   "BEGIN SELECT RAISE(ABORT,'settled refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="settled refused"):
        workflow.settle_submission(command.command_id)
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_settled")
    assert current(store, selection).execution.state.productive_sequence == plan.round.sequence
    assert current(store, selection).episode.continuation == baseline_before

    key = selection.expected.fence.key
    workflow.record_evidence(key, "b" * 40,
                             ReviewEvidenceKind.REVIEW_HISTORY, error="read unavailable")
    assert not workflow.evidence_due(key, "b" * 40, ReviewEvidenceKind.REVIEW_HISTORY)
    assert workflow.reconcile_submissions() == 0
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 1
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round
    assert current(store, selection).episode.continuation == baseline_before
    before = dump(store)
    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before
    _advance_generation_with_open_round(store, selection)
    assert current(store, selection).execution.state.productive_sequence == 0


def test_expired_supersession_observation_cannot_retire_a_verdict(rig):
    store, gateway, selection, _, _, command = confirmed_escalation(rig)
    gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    calls = 0

    def slow_second_read():
        nonlocal calls
        calls += 1
        if calls == 2:
            store._now = lambda: NOW + timedelta(hours=1)

    gateway.on_pr = slow_second_read

    result = workflow_for(store, gateway).settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    assert result.code is WorkflowRejection.OBSERVATION_UNAVAILABLE
    assert current(store, selection).execution.state.productive_sequence == 0
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 0


def test_confirmed_ceiling_escalation_commits_gate_and_productive_round_together(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    before_baseline = current(store, selection).episode.continuation

    result = workflow.settle_submission(command.command_id)

    assert isinstance(result, WorkflowCommitted)
    state = current(store, selection)
    assert state.episode.continuation == before_baseline
    assert state.execution.state.productive_sequence == plan.round.sequence
    assert len(state.gates) == 1
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round
    before = dump(store)
    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before


def test_refused_ceiling_still_closes_confirmed_productive_round(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause="ceiling")

    result = workflow_for(store, gateway).settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    assert result.code is WorkflowRejection.NOT_EXHAUSTED
    state = current(store, selection)
    assert state.gates == ()
    assert state.execution.state.productive_sequence == plan.round.sequence
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round


def test_ceiling_outcome_failure_rolls_back_gate_and_productivity(rig):
    store, gateway, selection, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_outcome BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-outcome' "
                   "BEGIN SELECT RAISE(ABORT,'outcome refused'); END")
    before = dump(store)

    with pytest.raises(sqlite3.IntegrityError, match="outcome refused"):
        workflow.settle_submission(command.command_id)

    assert dump(store) == before
    state = current(store, selection)
    assert state.gates == () and state.execution.state.productive_sequence == 0


def test_ceiling_marker_failure_replays_without_a_second_gate(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_settled BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-submission-settled' "
                   "BEGIN SELECT RAISE(ABORT,'settled refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="settled refused"):
        workflow.settle_submission(command.command_id)
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_settled")
    state = current(store, selection)
    assert len(state.gates) == 1
    assert state.execution.state.productive_sequence == plan.round.sequence

    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    assert len(current(store, selection).gates) == 1
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 1


def test_ceiling_waits_for_parent_seal_before_opening_gate(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause="ceiling", seal=False)
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    before = dump(store)

    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)


def test_stale_ceiling_result_closes_as_audit_without_opening_gate(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause="ceiling")
    _advance_generation_with_open_round(store, selection)
    before_state = current(store, selection).execution.state

    result = workflow_for(store, gateway).settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    state = current(store, selection)
    assert state.execution.state == before_state and state.gates == ()
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round


def test_refused_ceiling_replays_the_refusal_after_marker_failure(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = workflow_for(store, gateway)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_settled BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-submission-settled' "
                   "BEGIN SELECT RAISE(ABORT,'settled refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="settled refused"):
        workflow.settle_submission(command.command_id)
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_settled")
    assert current(store, selection).execution.state.productive_sequence == plan.round.sequence
    _advance_generation_with_open_round(store, selection)

    result = workflow.settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    assert result.code is WorkflowRejection.NOT_EXHAUSTED
    with store.transaction() as db:
        marker = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-submission-settled' "
                            "AND key=?", (str(command.command_id),)).fetchone()
    assert marker is not None and "not_exhausted" in marker["payload_json"]


def test_refused_ceiling_reload_requires_its_exact_observation(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    assert isinstance(workflow_for(store, gateway).settle_submission(command.command_id), WorkflowRejected)
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-ceiling-productivity' "
                         "AND key=?", (plan.round.round_id,)).fetchone()
        decision = decode_workflow(row["payload_json"], tuple)[0]
        db.execute("DELETE FROM workflow_evidence WHERE evidence_id=?",
                   (decision.observation_evidence_id,))

    with pytest.raises(WorkflowAuthorityError, match="exact observation"):
        current(store, selection)
