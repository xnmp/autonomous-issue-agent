"""One transaction reconstructs review closure candidates from retained sources."""

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.review_rounds import (
    FinalizedReviewRound, ReviewChildNotAdmitted, ReviewPlanningOpen,
    ReviewRoundAwaitingEvidence, ReviewRoundWait, ReviewSubmissionConfirmed,
    ReviewSubmissionKind, ReviewSubmissionPending, ReviewSubmissionRefused,
    finalize_review_round,
)
from tests.adapters.test_review_execution_control import OWNER, current, rig
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for


def test_open_round_facts_wait_for_parent_planning(rig):
    _, _, _, selection, control = rig
    plan = control.preflight(selection)
    facts = control.read_round_facts(plan.round.round_id)
    assert facts.plan == plan and isinstance(facts.planning, ReviewPlanningOpen)
    assert facts.children == facts.submissions == ()
    assert finalize_review_round(facts) == ReviewRoundAwaitingEvidence(frozenset({ReviewRoundWait.PLANNING}))


def test_abandoned_unadmitted_child_has_exact_fence_and_finalizes_without_spend(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    planned = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    abandoned = control.seal_abandoned_planning(plan.round.round_id)
    facts = control.read_round_facts(plan.round.round_id)
    assert facts.plan == planned
    assert len(facts.children) == 1
    assert facts.children[0] == ReviewChildNotAdmitted(planned.children[0], abandoned.abandonment_receipt)
    assert isinstance(finalize_review_round(facts), FinalizedReviewRound)
    assert control.read_round_facts(plan.round.round_id) == facts


def test_abandoned_parent_does_not_turn_its_registered_pending_submission_into_refusal(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    facts = control.read_round_facts(plan.round.round_id)
    assert isinstance(facts.submissions[0].result, ReviewSubmissionPending)
    assert finalize_review_round(facts) == ReviewRoundAwaitingEvidence(frozenset({ReviewRoundWait.SUBMISSIONS}))


@pytest.mark.parametrize("status", [None, EffectStatus.PERMANENT_FAILURE, EffectStatus.SUCCEEDED])
def test_submission_facts_follow_exact_command_receipt(rig, status):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    if status is not None:
        store.begin_command(command.command_id)
        store.record_effect(EffectReceipt(
            command.command_id, OWNER, store.now(), store.now(), status,
            remote_object_id="review:copied" if status is EffectStatus.SUCCEEDED else None,
            error="head changed" if status is EffectStatus.PERMANENT_FAILURE else None,
        ))
    facts = control.read_round_facts(plan.round.round_id)
    result = facts.submissions[0].result
    assert isinstance(result, {
        None: ReviewSubmissionPending,
        EffectStatus.PERMANENT_FAILURE: ReviewSubmissionRefused,
        EffectStatus.SUCCEEDED: ReviewSubmissionConfirmed,
    }[status])


def test_succeeded_submission_without_remote_review_identity_is_not_confirmation(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED))
    with pytest.raises(WorkflowAuthorityError, match="lacks its effect receipt"):
        control.read_round_facts(plan.round.round_id)


@pytest.mark.parametrize("corruption", ["digest", "attempt_owner", "remote_review"])
def test_submission_receipt_corruption_cannot_be_laundered_into_round_facts(rig, corruption):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:copied"))
    with store.transaction() as db:
        if corruption == "digest":
            db.execute("UPDATE effect_receipts SET receipt_digest=? WHERE command_id=?",
                       ("0" * 64, str(command.command_id)))
        elif corruption == "attempt_owner":
            db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?",
                       ("different-owner", str(command.command_id)))
        else:
            db.execute("UPDATE effect_receipts SET remote_object_id=? WHERE command_id=?",
                       ("review:changed", str(command.command_id)))
    with pytest.raises(WorkflowAuthorityError, match="changed after settlement"):
        control.read_round_facts(plan.round.round_id)
