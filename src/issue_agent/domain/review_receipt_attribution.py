"""Pure attribution of retained review receipt payloads to rounds or commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .review_execution import (
    CopiedReviewProgress, PaidReviewProgress, ReviewRoundIdentity, ReviewRoundOutcome,
)
from .review_preflight import ReviewPreflightSelection
from .review_rounds import (
    DeferredReviewPreparation, FinalizedReviewRound, ReviewParentAbandoned,
    ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan, ReviewStagePlan,
    ReviewSubmissionRegistration, StaleReviewProductivitySettlement,
    finalize_review_round,
)
from .review_workflow_policy import CeilingProductivityDecision
from .workflow_identity import WorkflowRevision, validate_workflow_identifier


class ReviewRoundReceiptNamespace(str, Enum):
    RECORDED = "review-recorded-submission"
    ABANDONMENT = "review-round-abandonment"
    CEILING_PRODUCTIVITY = "review-round-ceiling-productivity"
    OUTCOME = "review-round-outcome"
    PREFLIGHT = "review-round-preflight"
    PREPARATION = "review-round-preparation"
    SEAL = "review-round-seal"
    STAGE = "review-round-stage"
    STALE_PRODUCTIVITY = "review-round-stale-productivity"
    SUBMISSION = "review-round-submission"
    SUBMISSION_INDEX = "review-round-submission-index"
    SUPERSEDED_PRODUCTIVITY = "review-round-superseded-productivity"


@dataclass(frozen=True)
class BoundReviewRoundReceipt:
    round: ReviewRoundIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity):
            raise ValueError("bound review receipt requires a round identity")


@dataclass(frozen=True)
class NamedReviewRoundReceipt:
    round_id: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.round_id, "named review receipt round")


@dataclass(frozen=True)
class CommandReviewRoundReceipt:
    command_id: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "recorded review command")


ReviewRoundReceiptReference = (
    BoundReviewRoundReceipt | NamedReviewRoundReceipt | CommandReviewRoundReceipt
)


def _tuple(value: object, size: int) -> tuple[object, ...]:
    if not isinstance(value, tuple) or len(value) != size:
        raise ValueError("review receipt has an invalid payload shape")
    return value


def _stamp(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("review receipt requires a settlement timestamp")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("review receipt has an invalid settlement timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("review receipt requires an aware settlement timestamp")


def classify_review_round_receipt(
    namespace: ReviewRoundReceiptNamespace, key: str, value: object,
) -> ReviewRoundReceiptReference:
    """Validate local payload shape and identify its referenced round or command.

    This cannot prove external claim, command, observation or checkpoint sources.
    """
    if not isinstance(namespace, ReviewRoundReceiptNamespace):
        raise ValueError("review receipt requires a selected namespace")
    validate_workflow_identifier(key, "review receipt key")
    if namespace is ReviewRoundReceiptNamespace.PREFLIGHT:
        selection, owner, plan = _tuple(value, 3)
        if (not isinstance(selection, ReviewPreflightSelection)
                or not isinstance(owner, str) or not owner
                or not isinstance(plan, ReviewRoundPlan)
                or selection.request_id != key or plan.round.round_id != key
                or selection.binding != plan.round.binding):
            raise ValueError("review preflight receipt disagrees with its round")
        return BoundReviewRoundReceipt(plan.round)
    if namespace is ReviewRoundReceiptNamespace.STAGE:
        round_id, stage = _tuple(value, 2)
        if not isinstance(stage, ReviewStagePlan):
            raise ValueError("review stage receipt lacks a typed stage")
        if stage.children[0].round.round_id != round_id:
            raise ValueError("review stage receipt disagrees with its child round")
        return BoundReviewRoundReceipt(stage.children[0].round)
    if namespace in (
        ReviewRoundReceiptNamespace.SUBMISSION,
        ReviewRoundReceiptNamespace.SUBMISSION_INDEX,
    ):
        if not isinstance(value, ReviewSubmissionRegistration):
            raise ValueError("review submission receipt lacks a registration")
        if (namespace is ReviewRoundReceiptNamespace.SUBMISSION
                and value.submission.command_id != key):
            raise ValueError("review submission receipt changed its command")
        return BoundReviewRoundReceipt(value.round)
    if namespace is ReviewRoundReceiptNamespace.SEAL:
        round_id, owner, claim, revision, plan, planning = _tuple(value, 6)
        if (not isinstance(plan, ReviewRoundPlan)
                or not isinstance(planning, ReviewPlanningSealed)
                or not isinstance(revision, WorkflowRevision)
                or not isinstance(owner, str) or not owner
                or not isinstance(claim, str) or not claim
                or round_id != key or plan.round.round_id != key
                or revision.fence != plan.round.binding.rejection.fence):
            raise ValueError("review seal receipt disagrees with its round")
        return BoundReviewRoundReceipt(plan.round)
    if namespace is ReviewRoundReceiptNamespace.ABANDONMENT:
        round_id, owner, claim, plan, planning = _tuple(value, 5)
        if (not isinstance(plan, ReviewRoundPlan)
                or not isinstance(planning, ReviewParentAbandoned)
                or not isinstance(owner, str) or not owner
                or not isinstance(claim, str) or not claim
                or round_id != key or plan.round.round_id != key):
            raise ValueError("review abandonment receipt disagrees with its round")
        return BoundReviewRoundReceipt(plan.round)
    if namespace in (
        ReviewRoundReceiptNamespace.OUTCOME,
        ReviewRoundReceiptNamespace.PREPARATION,
    ):
        facts, recorded = _tuple(value, 2)
        if not isinstance(facts, ReviewRoundFacts) or facts.plan.round.round_id != key:
            raise ValueError("review outcome receipt disagrees with its round")
        result = finalize_review_round(facts)
        if namespace is ReviewRoundReceiptNamespace.OUTCOME:
            if (not isinstance(recorded, ReviewRoundOutcome)
                    or not isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation))
                    or result.outcome != recorded):
                raise ValueError("review outcome receipt has a foreign disposition")
        elif not isinstance(result, DeferredReviewPreparation) or recorded != result.failures:
            raise ValueError("review preparation receipt has a foreign obligation")
        return BoundReviewRoundReceipt(facts.plan.round)
    if namespace is ReviewRoundReceiptNamespace.STALE_PRODUCTIVITY:
        if not isinstance(value, StaleReviewProductivitySettlement):
            raise ValueError("stale review receipt lacks a generation crossing witness")
        facts, outcome = value.facts, value.outcome
        if (not isinstance(facts, ReviewRoundFacts)
                or not isinstance(outcome, ReviewRoundOutcome)
                or facts.plan.round.round_id != key
                or facts.plan.round != outcome.round
                or not isinstance(outcome.disposition, (PaidReviewProgress, CopiedReviewProgress))
                or finalize_review_round(facts) != FinalizedReviewRound(outcome)):
            raise ValueError("stale review receipt disagrees with its round")
        return BoundReviewRoundReceipt(facts.plan.round)
    if namespace is ReviewRoundReceiptNamespace.SUPERSEDED_PRODUCTIVITY:
        evidence, revision, stamp = _tuple(value, 3)
        if not isinstance(evidence, str) or not evidence or not isinstance(revision, WorkflowRevision):
            raise ValueError("superseded review receipt lacks its evidence")
        _stamp(stamp)
        return NamedReviewRoundReceipt(key)
    if namespace is ReviewRoundReceiptNamespace.CEILING_PRODUCTIVITY:
        decision, revision, stamp = _tuple(value, 3)
        if (not isinstance(decision, CeilingProductivityDecision)
                or not isinstance(revision, WorkflowRevision)):
            raise ValueError("ceiling review receipt lacks its decision")
        _stamp(stamp)
        return NamedReviewRoundReceipt(key)
    if namespace is ReviewRoundReceiptNamespace.RECORDED:
        if not isinstance(value, WorkflowRevision):
            raise ValueError("recorded review receipt lacks its revision")
        return CommandReviewRoundReceipt(key)
    raise ValueError("review receipt namespace is not classified")
