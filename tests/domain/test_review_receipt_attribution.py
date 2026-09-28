"""Retained review receipt payloads identify typed owners without SQL guesses."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.review_execution import ReviewPreparationFailure, ReviewPreparationKind, ReviewPreparationDeferred
from issue_agent.domain.review_execution import CopiedReviewProgress
from issue_agent.domain.review_preflight import plan_review_preflight
from issue_agent.domain.review_receipt_attribution import (
    BoundReviewRoundReceipt, CommandReviewRoundReceipt, NamedReviewRoundReceipt,
    ReviewRoundReceiptNamespace as Namespace, classify_review_round_receipt,
)
from issue_agent.domain.review_rounds import (
    ReviewParentAbandoned, ReviewPlanningSealed,
    ReviewSubmissionKind, ReviewSubmissionRegistration, StaleReviewProductivitySettlement,
    finalize_review_round,
)
from issue_agent.domain.workflow_identity import WorkflowRevision
from tests.domain.test_review_preflight import NOW, active
from tests.domain.test_review_rounds import (
    confirmed, facts, finalized, plan, submission,
)


def test_preflight_submission_and_recorded_receipts_have_distinct_references():
    snapshot, selection = active()
    planned = plan_review_preflight(snapshot, selection, now=NOW)
    assert classify_review_round_receipt(
        Namespace.PREFLIGHT, planned.round.round_id,
        (selection, "owner:review", planned),
    ) == BoundReviewRoundReceipt(planned.round)
    command = submission(ReviewSubmissionKind.COPIED)
    registration = ReviewSubmissionRegistration(
        planned.round, command, selection.claim_occurrence_id, "owner:review",
        snapshot.revision, "a" * 64, "idempotency:review", "config:1",
    )
    assert classify_review_round_receipt(
        Namespace.SUBMISSION, command.command_id, registration,
    ) == BoundReviewRoundReceipt(planned.round)
    assert classify_review_round_receipt(
        Namespace.SUBMISSION_INDEX, "hashed:index", registration,
    ) == BoundReviewRoundReceipt(planned.round)
    assert classify_review_round_receipt(
        Namespace.RECORDED, command.command_id, snapshot.revision,
    ) == CommandReviewRoundReceipt(command.command_id)
    with pytest.raises(ValueError, match="changed its command"):
        classify_review_round_receipt(Namespace.SUBMISSION, "another-command", registration)


def test_finalized_outcome_stage_seal_and_abandonment_name_the_same_round():
    planned = plan()
    observed = facts(planned)
    outcome = finalized(observed)
    round_id = planned.round.round_id
    revision = WorkflowRevision(planned.round.binding.rejection.fence, 1)
    assert classify_review_round_receipt(
        Namespace.OUTCOME, round_id, (observed, outcome),
    ) == BoundReviewRoundReceipt(planned.round)
    assert classify_review_round_receipt(
        Namespace.STAGE, "hashed:stage", (round_id, planned.stages[0]),
    ) == BoundReviewRoundReceipt(planned.round)
    with pytest.raises(ValueError, match="child round"):
        classify_review_round_receipt(
            Namespace.STAGE, "hashed:stage", ("round:foreign", planned.stages[0]),
        )
    assert classify_review_round_receipt(
        Namespace.SEAL, round_id,
        (round_id, "owner:review", "claim:review", revision, planned, observed.planning),
    ) == BoundReviewRoundReceipt(planned.round)
    abandoned = ReviewParentAbandoned("claim:terminal", "fence:admission")
    assert classify_review_round_receipt(
        Namespace.ABANDONMENT, round_id,
        (round_id, "owner:review", "claim:review", planned, abandoned),
    ) == BoundReviewRoundReceipt(planned.round)
    with pytest.raises(ValueError, match="another round"):
        classify_review_round_receipt(
            Namespace.OUTCOME, round_id,
            (observed, replace(outcome, round=replace(outcome.round, round_id="foreign:round"))),
        )


def test_preparation_and_productivity_receipts_require_matching_dispositions():
    planned = plan(empty=True)
    failure = ReviewPreparationFailure(ReviewPreparationKind.RUNTIME, "runtime:unavailable")
    deferred = facts(
        planned,
        planning=ReviewPlanningSealed("seal:deferred", ReviewPreparationDeferred(failure)),
    )
    result = finalize_review_round(deferred)
    assert classify_review_round_receipt(
        Namespace.PREPARATION, planned.round.round_id, (deferred, result.failures),
    ) == BoundReviewRoundReceipt(planned.round)
    with pytest.raises(ValueError, match="foreign obligation"):
        classify_review_round_receipt(
            Namespace.PREPARATION, planned.round.round_id, (deferred, ()),
        )
    command = submission(ReviewSubmissionKind.COPIED)
    productive = facts(plan(empty=True, submissions=(command,)), submissions=(confirmed(command),))
    outcome = finalized(productive)
    assert isinstance(outcome.disposition, CopiedReviewProgress)
    prior_binding = productive.plan.round.binding
    later_fence = replace(prior_binding.rejection.fence, generation=1)
    later_binding = replace(prior_binding, rejection=replace(
        prior_binding.rejection, fence=later_fence,
    ))
    stale = StaleReviewProductivitySettlement(
        productive, outcome, WorkflowRevision(later_fence, 1), later_binding, "a" * 64,
    )
    assert classify_review_round_receipt(
        Namespace.STALE_PRODUCTIVITY, productive.plan.round.round_id, stale,
    ) == BoundReviewRoundReceipt(productive.plan.round)
    with pytest.raises(ValueError, match="disagrees with its round"):
        classify_review_round_receipt(
            Namespace.STALE_PRODUCTIVITY, productive.plan.round.round_id,
            replace(stale, outcome=replace(outcome, disposition=replace(
                outcome.disposition, submission_receipt="receipt:foreign",
            ))),
        )
    with pytest.raises(ValueError, match="generation crossing witness"):
        classify_review_round_receipt(
            Namespace.STALE_PRODUCTIVITY, productive.plan.round.round_id,
            (productive, outcome),
        )


def test_superseded_receipt_requires_an_aware_settlement_stamp():
    round_id = "round:superseded"
    revision = WorkflowRevision(plan().round.binding.rejection.fence, 1)
    stamp = datetime(2026, 9, 29, tzinfo=timezone.utc).isoformat()
    assert classify_review_round_receipt(
        Namespace.SUPERSEDED_PRODUCTIVITY, round_id,
        ("observation:1", revision, stamp),
    ) == NamedReviewRoundReceipt(round_id)
    with pytest.raises(ValueError, match="aware"):
        classify_review_round_receipt(
            Namespace.SUPERSEDED_PRODUCTIVITY, round_id,
            ("observation:1", revision, "2026-09-29T00:00:00"),
        )
