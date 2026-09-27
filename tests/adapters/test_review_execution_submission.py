"""A review command becomes runnable only with its exact originating round."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.control_store import ControlStoreError
from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from issue_agent.domain.review_rounds import ReviewSubmissionKind, ReviewSubmissionRegistration
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_stage import stage


def command_for(snapshot, *, body="approved", patch_id="c" * 64):
    op = BoundReviewSubmissionOp(9, "APPROVE", body, "b" * 40, "review-bot", patch_id, None)
    command_id = bound_review_command_id(op, snapshot.binding)
    return LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                            "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                            workflow=snapshot.binding)


def test_copied_submission_plan_command_and_receipts_commit_together(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    before = current(store, selection)
    command = command_for(before)
    pending = control.register_submission(plan.round.round_id, before.revision, command,
                                          ReviewSubmissionKind.COPIED)
    after = current(store, selection)
    assert pending.command == command and pending.state == "pending"
    assert after.revision.version == before.revision.version + 1
    with store.transaction() as db:
        plan_row = db.execute("SELECT plan_json FROM review_execution_rounds").fetchone()
        persisted = decode_workflow(plan_row["plan_json"], type(plan))
        assert persisted.submissions[0].command_id == str(command.command_id)
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-submission'").fetchone()
        registration = decode_workflow(receipt["payload_json"], ReviewSubmissionRegistration)
        assert registration.round == plan.round
        assert registration.submission == persisted.submissions[0]
        assert registration.registered_revision == before.revision
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    replay_before = dump(store)
    assert control.register_submission(plan.round.round_id, before.revision, command,
                                       ReviewSubmissionKind.COPIED) == pending
    assert dump(store) == replay_before
    store.begin_command(command.command_id)
    assert store.latest_command(command.idempotency_key).state == "executing"


def test_paid_submission_requires_a_planned_review_stage(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection), patch_id="0" * 64)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="kind disagrees"):
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.PAID)
    assert dump(store) == before
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    before = current(store, selection)
    pending = control.register_submission(plan.round.round_id, before.revision, command,
                                          ReviewSubmissionKind.PAID)
    assert pending.command == command
    assert current(store, selection).revision.version == before.revision.version + 1


def test_refused_copied_command_allows_explicit_paid_fallback_stage(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    copied_revision = current(store, selection).revision
    copied_pending = control.register_submission(plan.round.round_id, copied_revision,
                                                 command, ReviewSubmissionKind.COPIED)
    before_refusal = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="definitive submission refusal"):
        control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert dump(store) == before_refusal
    store.begin_command(command.command_id)
    assert store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                            EffectStatus.PERMANENT_FAILURE, error="head changed"))
    updated = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert len(updated.stages) == 1 and len(updated.submissions) == 1
    paid = command_for(current(store, selection), body="fallback verdict")
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                paid, ReviewSubmissionKind.PAID)
    before_replay = dump(store)
    assert control.register_submission(plan.round.round_id, copied_revision,
                                       command, ReviewSubmissionKind.COPIED).command == copied_pending.command
    assert dump(store) == before_replay
    with store.transaction() as db:
        persisted = decode_workflow(db.execute("SELECT plan_json FROM review_execution_rounds").fetchone()[0],
                                    type(plan))
        assert tuple(item.kind for item in persisted.submissions) == (
            ReviewSubmissionKind.COPIED, ReviewSubmissionKind.PAID,
        )


def test_registration_receipt_failure_rolls_back_command_plan_and_revision(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_submission BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-submission' "
                   "BEGIN SELECT RAISE(ABORT,'registration refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="registration refused"):
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.COPIED)
    assert dump(store) == before


def test_second_submission_waits_for_first_command_refusal(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    second = command_for(current(store, selection), body="second copy")
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="definitive submission refusal"):
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    second, ReviewSubmissionKind.COPIED)
    assert dump(store) == before


def test_copied_submission_requires_canonical_same_generation_patch_coverage(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection), patch_id="0" * 64)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="same-generation covered patch"):
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.COPIED)
    assert dump(store) == before


def test_unregistered_or_changed_command_cannot_bypass_active_submission_guard(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    with pytest.raises(ControlStoreError, match="atomic round registration"):
        store.enqueue_command(command)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="conflicting round facts"):
        control.register_submission(plan.round.round_id, selection.expected,
                                    replace(command, payload={**command.payload, "body": "changed"}),
                                    ReviewSubmissionKind.COPIED)
    assert dump(store) == before


@pytest.mark.parametrize("corruption", ["command_receipt", "round_index", "command", "digest", "claim"])
def test_canonical_loader_refuses_incomplete_or_changed_submission_sources(rig, corruption):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    with store.transaction() as db:
        if corruption == "command_receipt":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-submission'")
        elif corruption == "round_index":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-submission-index'")
        elif corruption == "command":
            db.execute("DELETE FROM lifecycle_commands WHERE command_id=?", (str(command.command_id),))
        elif corruption == "digest":
            db.execute("UPDATE lifecycle_commands SET payload_digest=? WHERE command_id=?",
                       ("0" * 64, str(command.command_id)))
        else:
            row = db.execute("SELECT payload_json FROM dedupe_keys "
                             "WHERE namespace='review-round-submission-index'").fetchone()
            registration = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
            from issue_agent.adapters.workflow_codec import encode_workflow
            changed = encode_workflow(replace(registration, claim_occurrence_id="different-claim"))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace LIKE 'review-round-submission%'",
                       (changed,))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        current(store, selection)
    assert dump(store) == before


def test_unplanned_submission_index_is_visible_corruption(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        from issue_agent.adapters.workflow_rows import review_submission_receipt_key
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-round-submission-index", "o/r",
                    review_submission_receipt_key(plan.round.round_id, "orphan"),
                    store.now().isoformat(), "[]"))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="receipts disagree"):
        current(store, selection)
    assert dump(store) == before
