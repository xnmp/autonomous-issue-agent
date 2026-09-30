"""Confirmed review submissions settle the baseline and outer round together."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, load_review_workflow_row, persist_workflow_snapshot,
    read_review_submission_registration,
)
from issue_agent.adapters.control_store import load_bound_review_submission
from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review_execution import CopiedReviewProgress, PaidReviewProgress, ReviewRoundFailure, ReviewExecutionGenerationBoundary, review_generation_receipt_id
from issue_agent.domain.review_evidence import ReviewEvidenceKind
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.review_rounds import ReviewRoundPlan, ReviewRoundFacts, ReviewSubmissionKind, ReviewSubmissionRegistration
from issue_agent.domain.review_workflow import FreshReviewRequired
from issue_agent.domain.review_workflow_policy import record_review
from issue_agent.domain.workflow import (
    CancelRearm, ContinueGrantedRearm, KeepGuidance, OperatorActor, RequestRearm,
    WorkflowChanged, WorkflowCommitted, WorkflowRejected, WorkflowRejection,
)
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



def current_boundary(store, snapshot):
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation' "
            "AND scope=? AND key=?",
            (snapshot.revision.fence.key.repository,
             review_generation_receipt_id(snapshot.execution.binding, snapshot.revision.fence.generation)),
        ).fetchone()
    assert row is not None
    return decode_workflow(row["payload_json"], ReviewExecutionGenerationBoundary)

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


def accepted_generation_with_open_submission(rig, *, seal=True, old_cause="verdict"):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    later_selection = replace(selection, request_id="preflight:later-ceiling",
                              expected=current(store, selection).revision)
    _, _, _, _, _, escalation = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    binding = current(store, selection).binding
    if old_cause == "ceiling":
        op = BoundReviewSubmissionOp(9, "COMMENT", "older human review", "b" * 40,
                                     "review-bot", "c" * 64, "ceiling")
        command_id = bound_review_command_id(op, binding)
        command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                                   "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                                   workflow=binding)
    else:
        command = command_for(current(store, selection))
    control.register_submission(old.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    if seal:
        control.seal_completed_planning(old.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:older"))
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(escalation.command_id), WorkflowCommitted)
    before = current(store, selection)
    request_rearm = RequestRearm("rearm:with-older-submission", before.revision,
                                 before.gates[0].gate_id, KeepGuidance(), expected_binding=before.binding)
    assert isinstance(workflow.request_rearm(request_rearm, OperatorActor("operator", "test")),
                      WorkflowCommitted)
    return store, gateway, selection, control, old, command, workflow


def _registered_round_facts(store, gateway, command_id):
    before = dump(store)
    with store.transaction() as db:
        registration = read_review_submission_registration(db, "o/r", command_id)
    if registration is None:
        raise WorkflowAuthorityError("review submission registration is missing")
    control = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", OWNER), gateway)
    facts = control.read_round_facts(registration.round.round_id)
    assert dump(store) == before
    return facts


def test_current_registered_review_facts_joins_real_round_and_command_sources(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)

    result = _registered_round_facts(store, gateway, command.command_id)

    assert isinstance(result, ReviewRoundFacts)
    assert result.plan.round == plan.round
    assert result.plan.submissions[0].command_id == command.command_id


def test_current_registered_review_facts_survives_recording_and_round_finalization(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)

    result = _registered_round_facts(store, gateway, command.command_id)

    assert isinstance(result, ReviewRoundFacts)
    assert result.plan.round == plan.round


def test_current_registered_paid_review_facts_requires_real_stage_receipts(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)

    assert isinstance(_registered_round_facts(store, gateway, command.command_id), ReviewRoundFacts)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stage' AND scope='o/r'")
    with pytest.raises((WorkflowAuthorityError, ValueError)):
        _registered_round_facts(store, gateway, command.command_id)


def test_current_registered_review_facts_keeps_refused_copy_with_later_paid_stage(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.PERMANENT_FAILURE, error="head changed"))
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))

    result = _registered_round_facts(store, gateway, command.command_id)

    assert isinstance(result, ReviewRoundFacts)
    assert result.plan.round == plan.round


def test_import_blocks_forged_copy_of_paid_stage_without_reconstructing_productivity(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    with store.transaction() as db:
        row = db.execute("SELECT plan_json FROM review_execution_rounds WHERE repository='o/r'").fetchone()
        persisted = decode_workflow(row["plan_json"], ReviewRoundPlan)
        copied = replace(persisted.submissions[0], kind=ReviewSubmissionKind.COPIED)
        db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE repository='o/r'",
                   (encode_workflow(replace(persisted, submissions=(copied,))),))
        row = db.execute("SELECT payload_json FROM dedupe_keys "
                         "WHERE namespace='review-round-submission' AND scope='o/r'").fetchone()
        registration = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace LIKE 'review-round-submission%' "
                   "AND scope='o/r'", (encode_workflow(replace(registration, submission=copied)),))

    from issue_agent.adapters.review_execution_activation import read_complete_legacy_review_rounds
    from issue_agent.domain.review_execution_history_import import classify_review_import_history, ReviewImportHistoryBlocked
    from tests.domain.test_review_execution_history_import import _sources
    mapping, deaths, commands, _ = _sources(prs=(9,))
    with store.transaction() as db:
        raw_rounds = read_complete_legacy_review_rounds(db, "o/r")
    blocked, = classify_review_import_history(mapping, deaths, commands, raw_rounds)
    assert blocked.pr == 9 and isinstance(blocked, ReviewImportHistoryBlocked)
    assert any(source.identity == plan.round.round_id for blocker in blocked.blockers for source in blocker.sources)


@pytest.mark.parametrize("oversized", ["command", "effect"])
def test_import_raw_source_blocks_changed_oversized_command_or_effect(rig, oversized):
    from issue_agent.adapters.review_execution_activation import read_complete_legacy_review_submissions
    from issue_agent.domain.review_execution_activation import BlockedLegacyReviewSubmissions
    store, _, _, _, _, command = confirmed_copy(rig)
    with store.transaction() as db:
        if oversized == "command":
            db.execute("UPDATE lifecycle_commands SET payload_json=? WHERE command_id=?",
                       ("x" * 65537, str(command.command_id)))
        else:
            db.execute("UPDATE effect_receipts SET error=? WHERE command_id=?",
                       ("x" * 65537, str(command.command_id)))
        before = db.total_changes
        result = read_complete_legacy_review_submissions(db, "o/r")
        assert db.total_changes == before
    assert isinstance(result, BlockedLegacyReviewSubmissions)
    assert "bound" in result.reason


@pytest.mark.parametrize("altered", ["command_id", "digest", "idempotency", "config", "owner"])
def test_current_registered_review_facts_requires_exact_command_registration(rig, altered):
    store, gateway, _, _, _, command = confirmed_copy(rig)
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys "
                         "WHERE namespace='review-round-submission' AND scope='o/r'").fetchone()
        registration = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
        if altered == "command_id":
            submission = replace(registration.submission, command_id="forged-command")
            registration = replace(registration, submission=submission)
            row = db.execute("SELECT plan_json FROM review_execution_rounds WHERE repository='o/r'").fetchone()
            plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
            db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE repository='o/r'",
                       (encode_workflow(replace(plan, submissions=(submission,))),))
        elif altered == "digest":
            registration = replace(registration, command_digest="0" * 64)
        elif altered == "idempotency":
            registration = replace(registration, idempotency_key="forged-key")
        elif altered == "config":
            registration = replace(registration, config_version="forged-version")
        else:
            registration = replace(registration, owner_id="forged-owner")
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace LIKE 'review-round-submission%' "
                   "AND scope='o/r'", (encode_workflow(registration),))

    with pytest.raises((WorkflowAuthorityError, ValueError)):
        _registered_round_facts(store, gateway, command.command_id)


@pytest.mark.parametrize("damage", ["index", "plan", "missing_registration", "command", "forged_paid_kind"])
def test_current_registered_review_facts_blocks_contradictory_sources(rig, damage):
    store, gateway, _, _, _, command = confirmed_copy(rig)
    with store.transaction() as db:
        if damage == "index":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='review-round-submission-index' AND scope='o/r'")
        elif damage == "plan":
            db.execute("UPDATE review_execution_rounds SET plan_json='{}' WHERE repository='o/r'")
        elif damage == "missing_registration":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-submission' AND scope='o/r'")
        elif damage == "forged_paid_kind":
            row = db.execute("SELECT plan_json FROM review_execution_rounds WHERE repository='o/r'").fetchone()
            plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
            paid = replace(plan.submissions[0], kind=ReviewSubmissionKind.PAID)
            db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE repository='o/r'",
                       (encode_workflow(replace(plan, submissions=(paid,))),))
            row = db.execute("SELECT payload_json FROM dedupe_keys "
                             "WHERE namespace='review-round-submission' AND scope='o/r'").fetchone()
            registration = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace LIKE 'review-round-submission%' "
                       "AND scope='o/r'", (encode_workflow(replace(registration, submission=paid)),))
        else:
            db.execute("UPDATE lifecycle_commands SET payload_digest=? WHERE command_id=?",
                       ("0" * 64, str(command.command_id)))

    with pytest.raises((WorkflowAuthorityError, ValueError)):
        _registered_round_facts(store, gateway, command.command_id)


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

    with pytest.raises(WorkflowAuthorityError, match="joint settlement receipt"):
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


def _forge_generation_with_open_round(store, selection):
    with store.transaction() as db:
        prior = load_review_workflow_row(db, selection.expected.fence.key)
        fence = replace(prior.revision.fence, generation=prior.revision.fence.generation + 1)
        binding = replace(prior.execution.binding, rejection=replace(prior.binding, fence=fence))
        execution = replace(
            prior.execution,
            baseline=replace(prior.execution.baseline,
                             state=replace(prior.execution.baseline.state, binding=binding)),
            state=replace(prior.execution.baseline.state, binding=binding),
            generation_start_sequence=prior.execution.last_sequence,
        )
        advanced = replace(
            prior, revision=replace(prior.revision, fence=fence, version=prior.revision.version + 1),
            episode=replace(prior.episode, continuation=FreshReviewRequired(fence.generation)),
            execution=execution,
        )
        persist_workflow_snapshot(db, advanced)


def test_stale_confirmed_verdict_closes_round_before_settled_marker_without_reset(rig):
    store, gateway, selection, control, plan, command, workflow = accepted_generation_with_open_submission(rig)
    before = current(store, selection)

    result = workflow.settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    assert current(store, selection).execution.state == before.execution.state
    assert plan.round.round_id not in control.reconcile_review_rounds().finalized
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                          "AND key=?", (plan.round.round_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 1
    before = dump(store)
    assert workflow.settle_submission(command.command_id) is None
    assert dump(store) == before


def test_stale_confirmed_verdict_waits_for_parent_seal_before_marker(rig):
    store, gateway, selection, control, plan, command, workflow = accepted_generation_with_open_submission(
        rig, seal=False,
    )

    assert workflow.settle_submission(command.command_id) is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-submission-settled' "
                          "AND key=?", (str(command.command_id),)).fetchone()[0] == 0
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    sweep = control.reconcile_review_rounds()
    assert plan.round.round_id in sweep.finalized
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)


def test_stale_generation_witness_and_round_closure_commit_atomically(rig):
    store, _, _, _, plan, command, workflow = accepted_generation_with_open_submission(rig)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_stale_witness BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-stale-productivity' "
                   "BEGIN SELECT RAISE(ABORT,'stale witness refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="stale witness refused"):
        workflow.settle_submission(command.command_id)
    with store.transaction() as db:
        outcome = db.execute("SELECT outcome_json FROM review_execution_rounds WHERE round_id=?",
                             (plan.round.round_id,)).fetchone()
        assert outcome["outcome_json"] is None
        assert db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                          "AND key=?", (plan.round.round_id,)).fetchone() is None
        db.execute("DROP TRIGGER refuse_stale_witness")
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


def test_rejection_rearm_preserves_active_execution_boundary_and_reloads(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    request = RequestRearm("rearm:active-execution", before.revision, before.gates[0].gate_id,
                           KeepGuidance(), expected_binding=before.binding)

    result = workflow.request_rearm(request, OperatorActor("operator", "test"))

    assert isinstance(result, WorkflowCommitted)
    after = current(store, selection)
    assert after.revision.fence.generation == before.revision.fence.generation + 1
    assert after.execution.state.failures == before.execution.state.failures
    assert after.execution.state.pin_deferrals == before.execution.state.pin_deferrals
    assert after.execution.binding.rejection == after.binding
    assert after.execution.state.productive_sequence == plan.round.sequence
    assert current_boundary(store, after).before == before.execution.state
    assert current_boundary(store, after).through_sequence == plan.round.sequence
    before_replay = dump(store)
    assert workflow.request_rearm(request, OperatorActor("operator", "test")).idempotent
    assert dump(store) == before_replay


def test_old_open_round_closes_as_audit_after_rearm(rig):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    later_selection = replace(selection, request_id="preflight:after-open",
                              expected=current(store, selection).revision)
    later_rig = (store, snapshot, gateway, later_selection, control)
    _, _, _, _, productive, command = confirmed_escalation(later_rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    request = RequestRearm("rearm:with-open-round", before.revision, before.gates[0].gate_id,
                           KeepGuidance(), expected_binding=before.binding)
    assert isinstance(workflow.request_rearm(request, OperatorActor("operator", "test")), WorkflowCommitted)
    advanced = current(store, selection)
    assert current_boundary(store, advanced).through_sequence == productive.round.sequence
    assert current_boundary(store, advanced).applied_sequences == (productive.round.sequence,)

    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(old.round.round_id)
    result = control.finalize_abandoned_round(old.round.round_id)

    assert result.outcome.round == old.round
    assert current(store, selection).execution.state == advanced.execution.state


def test_late_old_paid_failure_does_not_debit_rearmed_execution(rig):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    planned = stage(old)
    control.register_stage(old.round.round_id, current(store, selection).revision, planned)
    child = planned.children[0]
    control.seal_completed_planning(old.round.round_id, current(store, selection).revision)
    proposed = request(child)
    assert store.reserve(proposed).admitted
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(
        True, status="error", exit_reason=ExitReason.AGENT_ERROR,
    ))
    store.settle_review_return(settlement(proposed), raw)
    later_selection = replace(selection, request_id="preflight:after-paid-failure",
                              expected=current(store, selection).revision)
    _, _, _, _, productive, command = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    request_rearm = RequestRearm("rearm:late-failure", before.revision, before.gates[0].gate_id,
                                 KeepGuidance(), expected_binding=before.binding)
    assert isinstance(workflow.request_rearm(request_rearm, OperatorActor("operator", "test")),
                      WorkflowCommitted)
    advanced = current(store, selection)
    assert current_boundary(store, advanced).applied_sequences == (productive.round.sequence,)

    result = control.finalize_completed_round(old.round.round_id)

    assert isinstance(result.outcome.disposition, ReviewRoundFailure)
    assert current(store, selection).execution.state == advanced.execution.state


def test_boundary_captures_state_neutral_outcome_settled_before_rearm(rig):
    store, snapshot, gateway, selection, control = rig
    old = control.preflight(selection)
    planned = stage(old)
    control.register_stage(old.round.round_id, current(store, selection).revision, planned)
    assert store.record_review_admission_refusal(request(planned.children[0], cap=59)) is not None
    control.seal_completed_planning(old.round.round_id, current(store, selection).revision)
    control.finalize_completed_round(old.round.round_id)
    later_selection = replace(selection, request_id="preflight:after-neutral",
                              expected=current(store, selection).revision)
    _, _, _, _, productive, command = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    rearm = RequestRearm("rearm:after-neutral", before.revision, before.gates[0].gate_id,
                         KeepGuidance(), expected_binding=before.binding)

    assert isinstance(workflow.request_rearm(rearm, OperatorActor("operator", "test")), WorkflowCommitted)
    boundary = current_boundary(store, current(store, selection))
    assert boundary.before == before.execution.state
    assert boundary.through_sequence == productive.round.sequence
    assert before.execution.baseline.through_sequence == productive.round.sequence


def test_stopped_rearm_continuation_preserves_execution_across_second_advance(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.request_rearm(
        RequestRearm("rearm:first", first.revision, first.gates[0].gate_id,
                     KeepGuidance(), expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    pending = current(store, selection)
    cancelled = workflow.cancel_rearm(
        CancelRearm("cancel:first", pending.revision, "rearm:first", "stop",
                    expected_binding=pending.binding), actor,
    )
    assert isinstance(cancelled, WorkflowCommitted)
    stopped = current(store, selection)

    result = workflow.continue_rearm(
        ContinueGrantedRearm("rearm:second", stopped.revision, "rearm:first",
                              expected_binding=stopped.binding), actor,
    )

    assert isinstance(result, WorkflowCommitted)
    after = current(store, selection)
    assert after.revision.fence.generation == first.revision.fence.generation + 2
    assert current_boundary(store, after).applied_sequences == ()
    assert current_boundary(store, after).from_revision.fence.generation == first.revision.fence.generation + 1
    assert after.execution.state.productive_sequence == plan.round.sequence


def test_ceiling_release_advances_active_execution_with_verified_boundary(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    released = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=0,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )

    result = released.reconcile_ceiling(selection.expected.fence.key)

    assert isinstance(result, WorkflowCommitted)
    after = current(store, selection)
    assert after.revision.fence.generation == before.revision.fence.generation + 1
    assert after.execution.binding.rejection == after.binding
    assert after.execution.state.productive_sequence == plan.round.sequence
    assert current_boundary(store, after).before == before.execution.state
    assert current_boundary(store, after).through_sequence == plan.round.sequence


@pytest.mark.parametrize("corruption", ["boundary", "transition"])
def test_active_generation_boundary_requires_its_accepted_receipts(rig, corruption):
    store, gateway, selection, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    request = RequestRearm("rearm:receipt-check", before.revision, before.gates[0].gate_id,
                           KeepGuidance(), expected_binding=before.binding)
    assert isinstance(workflow.request_rearm(request, OperatorActor("operator", "test")), WorkflowCommitted)
    with store.transaction() as db:
        if corruption == "boundary":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation'")
        else:
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-request' AND key=?",
                       (request.request_id,))

    if corruption == "boundary":
        with pytest.raises(WorkflowAuthorityError, match="exact transition receipt"):
            current(store, selection)
    else:
        assert current(store, selection).execution.binding.rejection == current(store, selection).binding


def test_generation_receipt_failure_rolls_back_rearm_and_accepted_transition(rig):
    store, gateway, selection, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    rearm = RequestRearm("rearm:rollback", before.revision, before.gates[0].gate_id,
                         KeepGuidance(), expected_binding=before.binding)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_generation BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-generation' "
                   "BEGIN SELECT RAISE(ABORT,'boundary refused'); END")
    baseline = dump(store)

    with pytest.raises(sqlite3.IntegrityError, match="boundary refused"):
        workflow.request_rearm(rearm, OperatorActor("operator", "test"))

    assert dump(store) == baseline
    assert current(store, selection) == before


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
    store, gateway, selection, control, plan, command, workflow = accepted_generation_with_open_submission(
        rig, old_cause="ceiling",
    )
    before = current(store, selection)

    result = workflow.settle_submission(command.command_id)

    assert isinstance(result, WorkflowRejected)
    state = current(store, selection)
    assert state.execution.state == before.execution.state and state.gates == before.gates
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

    with pytest.raises(WorkflowAuthorityError, match="joint settlement receipt"):
        current(store, selection)
