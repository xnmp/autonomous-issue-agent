"""Pure screening of an existing PR for a zero-history execution import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json

from .labels import Label
from .review_activation import OpenPullRequestsListed
from .review_execution import (
    ExecutionNotActivated, ImportedReviewExecutionDecision,
    ReviewExecutionBinding, ReviewExecutionCheckpoint, ReviewExecutionOrigin,
    ReviewExecutionOriginKind, ReviewExecutionPolicy, ReviewExecutionState,
)
from .review_execution_counter_mapping import LegacyOpenPRCounterLink
from .review_execution_history_import import ReviewImportNoHistoryObserved
from .review_workflow import (
    CompleteReviews, FreshReviewRequired, PullRequestState, RecordedPullRequest,
    ReviewWorkflowSnapshot,
)
from .review_workflow_policy import OBSERVATION_MAX_AGE_SECONDS
from .workflow_identity import (
    PullRequestRef, canonical_workflow_repository, validate_workflow_integer,
    validate_workflow_text,
)


class ZeroHistoryImportBlockerKind(str, Enum):
    IDENTITY = "identity"
    ACTIVE_EXECUTION = "active_execution"
    REMOTE_UNAVAILABLE = "remote_unavailable"
    STALE_OBSERVATION = "stale_observation"
    CHANGED_HEAD = "changed_head"
    PRIOR_REVIEW = "prior_review"
    WORKFLOW_HISTORY = "workflow_history"
    REARM_OR_GATE = "rearm_or_gate"
    VERDICT_HOLD = "verdict_hold"
    EXTERNAL_HOLD = "external_hold"


@dataclass(frozen=True)
class BlockedZeroHistoryReviewImport:
    pr: int
    kind: ZeroHistoryImportBlockerKind

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "blocked execution import PR", minimum=1)
        if not isinstance(self.kind, ZeroHistoryImportBlockerKind):
            raise ValueError("zero-history import blocker requires a typed cause")


@dataclass(frozen=True)
class EligibleZeroHistoryReviewImport:
    """Source-bound observation; the activation writer must rederive it."""

    history: ReviewImportNoHistoryObserved
    workflow: ReviewWorkflowSnapshot
    observation: RecordedPullRequest
    link: LegacyOpenPRCounterLink
    review_bot: str
    checked_at: datetime
    source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.history, ReviewImportNoHistoryObserved)
                or not isinstance(self.workflow, ReviewWorkflowSnapshot)
                or not isinstance(self.observation, RecordedPullRequest)
                or not isinstance(self.link, LegacyOpenPRCounterLink)
                or not isinstance(self.review_bot, str)
                or not isinstance(self.checked_at, datetime) or self.checked_at.tzinfo is None
                or self.checked_at.utcoffset() is None
                or not isinstance(self.source_digest, str) or len(self.source_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.source_digest)):
            raise ValueError("zero-history import requires complete typed sources")
        if (_blocking_kind(self.history, self.workflow, self.observation, self.link,
                           self.review_bot, self.checked_at) is not None
                or self.source_digest != _source_digest(
                    self.history, self.workflow, self.observation, self.link, self.review_bot,
                )):
            raise ValueError("zero-history import disagrees with its source facts")

    @property
    def pr(self) -> int:
        return self.history.pr


def _source_digest(
    history: ReviewImportNoHistoryObserved, workflow: ReviewWorkflowSnapshot,
    observation: RecordedPullRequest, link: LegacyOpenPRCounterLink, review_bot: str,
) -> str:
    observed = observation.observation
    reviews = observed.reviews
    assert isinstance(reviews, CompleteReviews)
    continuation = workflow.episode.continuation
    assert isinstance(continuation, FreshReviewRequired)
    source = (
        history.source_digest,
        (workflow.revision.fence.key.repository, history.pr, workflow.revision.fence.generation,
         workflow.revision.version, workflow.authority.epoch, workflow.episode.episode_id,
         workflow.episode.guidance, continuation.owed_since_generation),
        (observed.key.repository, observed.head_sha, observed.state.value,
         tuple(sorted(label.value for label in observed.labels)),
         tuple(sorted((item.review_id, item.author, item.state.value, item.commit_id,
                       item.submitted_at.isoformat(), item.review_command)
                      for item in reviews.reviews)),
         observation.external_holds),
        (link.pr, link.head_sha, link.branch, link.closing_issue, link.linked_issue),
        review_bot.casefold(),
    )
    return hashlib.sha256(json.dumps(source, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _blocking_kind(
    history: ReviewImportNoHistoryObserved, workflow: ReviewWorkflowSnapshot,
    observation: RecordedPullRequest, link: LegacyOpenPRCounterLink, review_bot: str,
    now: datetime,
) -> ZeroHistoryImportBlockerKind | None:
    pr = history.pr
    key = workflow.revision.fence.key
    observed = observation.observation
    if (not isinstance(key.target, PullRequestRef) or key.target.number != pr
            or observed.key != key or link.pr != pr):
        return ZeroHistoryImportBlockerKind.IDENTITY
    if not isinstance(workflow.execution, ExecutionNotActivated):
        return ZeroHistoryImportBlockerKind.ACTIVE_EXECUTION
    if observed.state is not PullRequestState.OPEN or not isinstance(observed.reviews, CompleteReviews):
        return ZeroHistoryImportBlockerKind.REMOTE_UNAVAILABLE
    if not 0 <= (now - observed.observed_at).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS:
        return ZeroHistoryImportBlockerKind.STALE_OBSERVATION
    if link.head_sha != observed.head_sha:
        return ZeroHistoryImportBlockerKind.CHANGED_HEAD
    if any(item.author.casefold() == review_bot.casefold() or item.review_command is not None
           for item in observed.reviews.reviews):
        return ZeroHistoryImportBlockerKind.PRIOR_REVIEW
    if (workflow.revision.fence.generation != 0 or workflow.revision.version != 0
            or not isinstance(workflow.episode.continuation, FreshReviewRequired)):
        return ZeroHistoryImportBlockerKind.WORKFLOW_HISTORY
    if workflow.intent is not None or workflow.gates:
        return ZeroHistoryImportBlockerKind.REARM_OR_GATE
    if Label.NEEDS_HUMAN_REVIEW in observed.labels:
        return ZeroHistoryImportBlockerKind.VERDICT_HOLD
    if observation.external_holds:
        return ZeroHistoryImportBlockerKind.EXTERNAL_HOLD
    return None


def screen_zero_history_review_import(
    history: ReviewImportNoHistoryObserved, workflow: ReviewWorkflowSnapshot,
    observation: RecordedPullRequest, link: LegacyOpenPRCounterLink, review_bot: str,
    *, now: datetime,
) -> EligibleZeroHistoryReviewImport | BlockedZeroHistoryReviewImport:
    """Screen a complete empty-history observation without granting activation."""
    if (not isinstance(history, ReviewImportNoHistoryObserved)
            or not isinstance(workflow, ReviewWorkflowSnapshot)
            or not isinstance(observation, RecordedPullRequest)
            or not isinstance(link, LegacyOpenPRCounterLink)):
        raise ValueError("zero-history import screening requires typed observations")
    validate_workflow_text(review_bot, "review import bot", limit=256)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("zero-history import screening requires an aware time")
    kind = _blocking_kind(history, workflow, observation, link, review_bot, now)
    if kind is not None:
        return BlockedZeroHistoryReviewImport(history.pr, kind)
    return EligibleZeroHistoryReviewImport(
        history, workflow, observation, link, review_bot, now,
        _source_digest(history, workflow, observation, link, review_bot),
    )


def propose_zero_history_review_execution(
    eligible: EligibleZeroHistoryReviewImport, execution_epoch: int,
    activation_plan_digest: str, repository_source_digest: str,
    policy: ReviewExecutionPolicy = ReviewExecutionPolicy(),
    *, now: datetime,
) -> ImportedReviewExecutionDecision:
    """Derive a deterministic sequence-zero proposal from screened sources."""
    if not isinstance(eligible, EligibleZeroHistoryReviewImport):
        raise ValueError("zero-history execution proposal requires screened evidence")
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or _blocking_kind(eligible.history, eligible.workflow, eligible.observation,
                              eligible.link, eligible.review_bot, now) is not None):
        raise ValueError("zero-history execution proposal requires fresh current evidence")
    validate_workflow_integer(execution_epoch, "execution import epoch", minimum=1)
    if not isinstance(policy, ReviewExecutionPolicy):
        raise ValueError("execution import requires a typed policy")
    for name, value in (("activation plan", activation_plan_digest),
                        ("repository source", repository_source_digest)):
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"execution import {name} requires a SHA-256 digest")
    key = eligible.workflow.revision.fence.key
    episode_hash = hashlib.sha256(
        "\0".join((key.repository, str(eligible.pr), str(execution_epoch),
                    eligible.source_digest, repository_source_digest)).encode(),
    ).hexdigest()
    binding = ReviewExecutionBinding(
        eligible.workflow.binding, execution_epoch, f"review-execution-import:{episode_hash}",
    )
    checkpoint = ReviewExecutionCheckpoint(0, ReviewExecutionState(binding), policy)
    origin = ReviewExecutionOrigin(
        ReviewExecutionOriginKind.IMPORTED, f"review-execution-origin:{episode_hash}",
    )
    return ImportedReviewExecutionDecision(origin, checkpoint, activation_plan_digest,
                                           repository_source_digest)


def _digest(value: str, name: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"execution import {name} requires a SHA-256 digest")


@dataclass(frozen=True)
class EmptyReviewExecutionImportPlan:
    """Whole-repository zero-history plan; observational until a writer rechecks it."""

    repository: str
    listing: OpenPullRequestsListed
    execution_epoch: int
    source_set_digest: str
    retained_digest: str
    inventory_digest: str
    candidates: tuple[EligibleZeroHistoryReviewImport, ...]
    planned_at: datetime
    policy: ReviewExecutionPolicy = ReviewExecutionPolicy()

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.listing, OpenPullRequestsListed)
                or not isinstance(self.candidates, tuple)
                or any(not isinstance(item, EligibleZeroHistoryReviewImport) for item in self.candidates)
                or tuple(item.pr for item in self.candidates) != self.listing.numbers
                or any(item.workflow.revision.fence.key.repository != self.repository
                       or item.history.source_digest != self.source_set_digest
                       for item in self.candidates)
                or not isinstance(self.planned_at, datetime) or self.planned_at.tzinfo is None
                or self.planned_at.utcoffset() is None
                or any(_blocking_kind(item.history, item.workflow, item.observation,
                                      item.link, item.review_bot, self.planned_at) is not None
                       for item in self.candidates)
                or not isinstance(self.policy, ReviewExecutionPolicy)):
            raise ValueError("execution import plan requires every listed PR and matching typed sources")
        validate_workflow_integer(self.execution_epoch, "execution import epoch", minimum=1)
        for name, value in (("source set", self.source_set_digest),
                            ("retained census", self.retained_digest),
                            ("inventory", self.inventory_digest)):
            _digest(value, name)

    @property
    def source_digest(self) -> str:
        source = (
            self.source_set_digest, self.retained_digest,
            tuple(item.source_digest for item in self.candidates),
        )
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()

    @property
    def plan_digest(self) -> str:
        source = (
            self.repository, self.listing.numbers, self.execution_epoch,
            self.source_digest, self.inventory_digest, self.policy.free_pin_deferrals,
        )
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()

    def decisions_at(self, now: datetime) -> tuple[ImportedReviewExecutionDecision, ...]:
        """Recheck freshness when deriving decisions from a reviewed plan."""
        if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or now < self.planned_at):
            raise ValueError("execution import decisions require a current aware time")
        return tuple(propose_zero_history_review_execution(
            item, self.execution_epoch, self.plan_digest, self.source_digest, self.policy,
            now=now,
        ) for item in self.candidates)
