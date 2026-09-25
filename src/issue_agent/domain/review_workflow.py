"""Review-rejection workflow values: PR-owned episode, continuation and re-arm; ADR 0015.

Review intents carry no workspace handoff: generalizing resolve intents would
weaken resolve invariants. The continuation records when a fresh review is
owed; a new generation invalidates the old reviewed baseline, and old review
completion can never repopulate it. Values perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .contracts import CommandId
from .labels import Label
from .review_rejection import (
    MAX_COUNTED_REVIEWS, MAX_OBSERVED_REVIEWS, CountedReview, CountingRule, PullRequestState, ReviewRearmPreflight,
    ReviewRejectionCeiling, validate_commit_sha, validate_counted, validate_labels,
)
from .workflow import (
    GUIDANCE_MAX_CHARS, MAX_DETAIL_CHARS, MAX_WORKFLOW_GATES,
    AuthorizedOperatorCommand, ContinueGrantedRearm, OpenGate, RearmNeedsDecision, RearmingGate, RequestRearm,
    ResolveCeiling, StopReason, WorkflowAbsent, WorkflowChanged, WorkflowMigrationRequired, WorkflowMutation,
    WorkflowSnapshot, WorkflowUnavailable,
    _command_ids, _count, _items, _text, _time,
)
from .workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision,
)

__all__ = [
    "MAX_COUNTED_REVIEWS", "MAX_OBSERVED_REVIEWS", "CountedReview", "CountingRule", "ReviewRearmPreflight", "ReviewRejectionCeiling",
    "FreshReviewRequired", "BoundReviewSubmission", "ImportedBaseline", "ReviewedBaseline", "ReviewContinuation",
    "ReviewEpisode", "DismissCountedReview", "RequireFreshReview", "EnsureReviewProjection", "ReviewRearmOperation",
    "ReviewProjectionPlan",
    "PendingReviewRearm", "CompletedReviewRearm", "SupersededReviewRearm", "CancelledReviewRearm",
    "ReviewRearmIntent", "ReviewWorkflowSnapshot", "AnyWorkflowSnapshot", "ReviewWorkflowRead",
    "SubmittedReviewState", "SubmittedReviewFact", "CompleteReviews", "PartialReviews", "ReviewRead",
    "PullRequestState", "ObservedPullRequest", "PullRequestAbsent", "PullRequestUnavailable",
    "PullRequestObservation", "decisive_reviews", "validate_review_change",
]


def _pr(value: object, name: str) -> None:
    if not isinstance(value, PullRequestRef):
        raise ValueError(f"{name} requires a pull request identity")


def _patch_id(value: object) -> None:
    # `review.patch_signature` is a SHA-256 hex digest of the normalized diff.
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("reviewed patch identity must be a SHA-256 digest")


@dataclass(frozen=True)
class FreshReviewRequired:
    owed_since_generation: int

    def __post_init__(self) -> None:
        _count(self.owed_since_generation, "fresh review generation")


@dataclass(frozen=True)
class BoundReviewSubmission:
    """The generation-fenced SUBMIT_REVIEW command whose receipt established a baseline."""
    command_id: CommandId
    binding: WorkflowBinding

    def __post_init__(self) -> None:
        _text(self.command_id, "review submission command")
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.REVIEW_REJECTIONS:
            raise ValueError("review submission requires a review-family binding")


@dataclass(frozen=True)
class ImportedBaseline:
    """A legacy reviewed-head/patch file recorded as activation import evidence."""
    evidence_id: str

    def __post_init__(self) -> None:
        _text(self.evidence_id, "imported baseline evidence")


BaselineSource = BoundReviewSubmission | ImportedBaseline


@dataclass(frozen=True)
class ReviewedBaseline:
    head_sha: str
    patch_id: str
    reviewed_generation: int
    source: BaselineSource

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "reviewed head")
        _patch_id(self.patch_id)
        _count(self.reviewed_generation, "reviewed generation")
        if not isinstance(self.source, (BoundReviewSubmission, ImportedBaseline)):
            raise ValueError("reviewed baseline requires typed provenance")
        if isinstance(self.source, BoundReviewSubmission) and self.source.binding.fence.generation != self.reviewed_generation:
            raise ValueError("a bound review cannot establish another generation's baseline")


ReviewContinuation = FreshReviewRequired | ReviewedBaseline


@dataclass(frozen=True)
class ReviewEpisode:
    episode_id: str
    guidance: str = ""
    continuation: ReviewContinuation = FreshReviewRequired(0)

    def __post_init__(self) -> None:
        _text(self.episode_id, "episode")
        _text(self.guidance, "guidance", limit=GUIDANCE_MAX_CHARS, empty=True)
        if not isinstance(self.continuation, (FreshReviewRequired, ReviewedBaseline)):
            raise ValueError("review episode requires a validated continuation")
        source = self.continuation.source if isinstance(self.continuation, ReviewedBaseline) else None
        if isinstance(source, BoundReviewSubmission) and source.binding.episode_id != self.episode_id:
            raise ValueError("review continuation cannot inherit another episode")


@dataclass(frozen=True)
class DismissCountedReview:
    pr: PullRequestRef
    review: CountedReview

    def __post_init__(self) -> None:
        _pr(self.pr, "review dismissal")
        if not isinstance(self.review, CountedReview):
            raise ValueError("review dismissal requires a counted review")


@dataclass(frozen=True)
class RequireFreshReview:
    pr: PullRequestRef

    def __post_init__(self) -> None:
        _pr(self.pr, "fresh review obligation")


@dataclass(frozen=True)
class EnsureReviewProjection:
    pr: PullRequestRef

    def __post_init__(self) -> None:
        _pr(self.pr, "review projection")


ReviewRearmOperation = DismissCountedReview | RequireFreshReview | EnsureReviewProjection


@dataclass(frozen=True)
class ReviewProjectionPlan:
    """The relabel a review re-arm planned: release (remove the attention hold,
    add the trigger) or hold (keep and ensure the attention hold, add nothing).

    Recorded so a replacement can tell whether an uncertain earlier relabel
    would land the same outcome; only then does it cover that uncertainty.
    """
    command_id: CommandId
    remove: Label | None
    add: Label

    def __post_init__(self) -> None:
        _text(self.command_id, "projection command")
        if not isinstance(self.add, Label) or (self.remove is not None and not isinstance(self.remove, Label)):
            raise ValueError("review projection labels must be typed")
        if self.remove is None:
            if self.add is not Label.NEEDS_HUMAN_REVIEW:
                raise ValueError("a held review projection only ensures the attention hold")
        elif self.remove is not Label.NEEDS_HUMAN_REVIEW or self.add is Label.NEEDS_HUMAN_REVIEW:
            raise ValueError("a released review projection removes the attention hold and adds a trigger")

    @property
    def labels(self) -> tuple[Label | None, Label]:
        return (self.remove, self.add)
_OPERATIONS = (DismissCountedReview, RequireFreshReview, EnsureReviewProjection)


@dataclass(frozen=True)
class PendingReviewRearm:
    """`authorized` is the exact counted set the operator accepted; dismissal
    obligations may name only those reviews, and a stopped intent keeps them so
    continuation never recovers authority from audit or actor JSON. `rule` is
    the counting rule that selected them; later recounts use only it.
    `replaces` names the stopped decision's retired obligations this intent
    takes over: uncertainty over them lands the same outcome, so it is not an
    independent hold (ADR 0015). Dismissals are idempotent and always covered;
    the stopped relabel is covered only when its `projection` labels equal
    this intent's."""
    request_id: str
    gate_id: str
    from_generation: int
    to_generation: int
    operations: tuple[ReviewRearmOperation, ...]
    required_commands: tuple[CommandId, ...]
    authorized: tuple[CountedReview, ...]
    rule: CountingRule
    projection: ReviewProjectionPlan
    replaces: tuple[CommandId, ...] = ()

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _count(self.from_generation, "prior generation")
        _count(self.to_generation, "rearm generation", minimum=1)
        if self.to_generation != self.from_generation + 1:
            raise ValueError("a rearm advances generation exactly once")
        _items(self.operations, "operations", _OPERATIONS, MAX_COUNTED_REVIEWS + 2)
        if sum(isinstance(item, RequireFreshReview) for item in self.operations) != 1:
            raise ValueError("review rearm requires exactly one fresh review obligation")
        if sum(isinstance(item, EnsureReviewProjection) for item in self.operations) != 1:
            raise ValueError("review rearm requires exactly one review projection obligation")
        dismissed = tuple(item.review.review_id for item in self.operations if isinstance(item, DismissCountedReview))
        if len(set(dismissed)) != len(dismissed):
            raise ValueError("review rearm cannot dismiss one review twice")
        if len(dismissed) > MAX_COUNTED_REVIEWS:
            raise ValueError("review rearm dismissals exceed the counted-review bound")
        validate_counted(self.authorized, "authorized reviews")
        if not set(self.dismissals) <= set(self.authorized):
            raise ValueError("review rearm may dismiss only its authorized counted reviews")
        if len({item.pr for item in self.operations}) != 1:
            raise ValueError("review rearm obligations must belong to one pull request")
        _command_ids(self.required_commands, "required commands")
        if not self.required_commands:
            raise ValueError("pending rearm requires durable command obligations")
        _rule(self.rule)
        if not isinstance(self.projection, ReviewProjectionPlan) or self.projection.command_id not in self.required_commands:
            raise ValueError("a review rearm plans the projection among its obligations")
        _command_ids(self.replaces, "replaced obligations")
        if set(self.replaces) & set(self.required_commands):
            raise ValueError("a review rearm cannot replace its own obligations")

    @property
    def pr(self) -> PullRequestRef:
        return self.operations[0].pr

    @property
    def dismissals(self) -> tuple[CountedReview, ...]:
        return tuple(item.review for item in self.operations if isinstance(item, DismissCountedReview))


def _projection(value: object) -> None:
    if not isinstance(value, ReviewProjectionPlan):
        raise ValueError("a stopped review rearm keeps its planned projection")


def _rule(value: object) -> None:
    if not isinstance(value, CountingRule):
        raise ValueError("review rearm requires its captured counting rule")


def _disposition(verified: tuple[CommandId, ...], retired: tuple[CommandId, ...]) -> None:
    _command_ids(verified, "verified commands")
    _command_ids(retired, "retired obligations")
    if set(verified) & set(retired):
        raise ValueError("a verified obligation cannot also be retired")


@dataclass(frozen=True)
class CompletedReviewRearm:
    request_id: str
    gate_id: str
    to_generation: int
    verified_commands: tuple[CommandId, ...]

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _count(self.to_generation, "rearm generation", minimum=1)
        _command_ids(self.verified_commands, "verified commands")


@dataclass(frozen=True)
class SupersededReviewRearm:
    request_id: str
    gate_id: str
    to_generation: int
    reason: StopReason
    evidence_id: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]
    authorized: tuple[CountedReview, ...]
    rule: CountingRule
    projection: ReviewProjectionPlan

    def __post_init__(self) -> None:
        _projection(self.projection)
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _text(self.evidence_id, "supersession evidence")
        validate_counted(self.authorized, "authorized reviews")
        _rule(self.rule)
        _count(self.to_generation, "rearm generation", minimum=1)
        if not isinstance(self.reason, StopReason) or self.reason is StopReason.OPERATOR_CANCELLED:
            raise ValueError("supersession requires an observed stop reason")
        _disposition(self.verified_commands, self.retired_obligations)


@dataclass(frozen=True)
class CancelledReviewRearm:
    request_id: str
    gate_id: str
    to_generation: int
    cancellation_id: str
    reason: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]
    authorized: tuple[CountedReview, ...]
    rule: CountingRule
    projection: ReviewProjectionPlan

    def __post_init__(self) -> None:
        _projection(self.projection)
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _text(self.cancellation_id, "cancellation")
        validate_counted(self.authorized, "authorized reviews")
        _rule(self.rule)
        _text(self.reason, "cancellation reason", limit=MAX_DETAIL_CHARS)
        _count(self.to_generation, "rearm generation", minimum=1)
        _disposition(self.verified_commands, self.retired_obligations)


ReviewRearmIntent = PendingReviewRearm | CompletedReviewRearm | SupersededReviewRearm | CancelledReviewRearm
_INTENTS = (PendingReviewRearm, CompletedReviewRearm, SupersededReviewRearm, CancelledReviewRearm)


@dataclass(frozen=True)
class ReviewWorkflowSnapshot:
    revision: WorkflowRevision
    authority: FamilyAuthority
    episode: ReviewEpisode
    gates: tuple[OpenGate | RearmingGate, ...] = ()
    intent: ReviewRearmIntent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, WorkflowRevision) or not isinstance(
            self.authority, FamilyAuthority
        ) or not isinstance(self.episode, ReviewEpisode):
            raise ValueError("snapshot requires validated revision, authority and episode")
        if self.authority.family is not WorkflowFamily.REVIEW_REJECTIONS:
            raise ValueError("this snapshot supports only review rejections")
        if not isinstance(self.revision.fence.key.target, PullRequestRef):
            raise ValueError("review rejection workflow requires a pull request")
        generation = self.revision.fence.generation
        continuation = self.episode.continuation
        if isinstance(continuation, ReviewedBaseline):
            if continuation.reviewed_generation != generation:
                raise ValueError("a reviewed baseline belongs only to the current generation")
            source = continuation.source
            if isinstance(source, BoundReviewSubmission) and (
                source.binding.fence.key != self.revision.fence.key or source.binding.authority != self.authority
            ):
                raise ValueError("continuation provenance disagrees with its workflow authority")
        elif continuation.owed_since_generation > generation:
            raise ValueError("a fresh review cannot be owed from a future generation")
        _items(self.gates, "gates", (OpenGate, RearmingGate), MAX_WORKFLOW_GATES)
        opened = tuple(g.gate if isinstance(g, RearmingGate) else g for g in self.gates)
        if len({g.gate_id for g in opened}) != len(opened):
            raise ValueError("gate identities must be unique")
        if any(g.opened_generation > generation for g in opened):
            raise ValueError("a gate cannot originate in a future generation")
        if any(isinstance(g.cause, ResolveCeiling) for g in opened):
            raise ValueError("a review workflow cannot hold a resolve ceiling cause")
        if self.intent is not None and not isinstance(self.intent, _INTENTS):
            raise ValueError("intent must be a validated review variant")
        rearming = tuple(g for g in self.gates if isinstance(g, RearmingGate))
        if isinstance(self.intent, PendingReviewRearm):
            if self.intent.to_generation != generation:
                raise ValueError("pending intent must own the current generation")
            if len(rearming) != 1 or (
                rearming[0].request_id != self.intent.request_id
                or rearming[0].gate.gate_id != self.intent.gate_id
            ):
                raise ValueError("pending intent must own exactly one rearming gate")
            if self.intent.pr != self.revision.fence.key.target:
                raise ValueError("review obligations must belong to this workflow")
            if continuation != FreshReviewRequired(self.intent.to_generation):
                raise ValueError("a pending review rearm owes a fresh review from its new generation")
            cause = rearming[0].gate.cause
            if isinstance(cause, ReviewRejectionCeiling):
                if not set(self.intent.authorized) <= set(cause.counted):
                    raise ValueError("a review rearm may authorize only the gate's counted reviews")
                if self.intent.rule != cause.rule:
                    raise ValueError("a review rearm counts under its gate's captured rule")
            elif not isinstance(cause, RearmNeedsDecision) or cause.stopped_request_id == self.intent.request_id:
                raise ValueError("a review rearm requires a rejection ceiling or an earlier stopped decision")
            elif not set(self.intent.replaces) <= set(cause.obligations):
                raise ValueError("a review continuation replaces only its stopped decision's obligations")
        elif rearming:
            raise ValueError("rearming gate requires a pending owning intent")
        # A stopped review re-arm is visible as exactly one open decision gate,
        # and no decision gate outlives the stop it records: otherwise a stop
        # could leave no operator path, or a stale decision could hold forever.
        decisions = tuple(g.cause.stopped_request_id for g in self.gates
                          if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision))
        stopped = self.intent if isinstance(self.intent, (SupersededReviewRearm, CancelledReviewRearm)) else None
        if decisions != ((stopped.request_id,) if stopped is not None else ()):
            raise ValueError("an open review decision gate exists exactly for the current stopped re-arm")
        if self.intent is not None and self.intent.to_generation > generation:
            raise ValueError("intent cannot belong to a future generation")

    @property
    def binding(self) -> WorkflowBinding:
        return WorkflowBinding(self.revision.fence, self.authority, self.episode.episode_id)


AnyWorkflowSnapshot = WorkflowSnapshot | ReviewWorkflowSnapshot
ReviewWorkflowRead = ReviewWorkflowSnapshot | WorkflowAbsent | WorkflowMigrationRequired | WorkflowUnavailable


class SubmittedReviewState(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"


@dataclass(frozen=True)
class SubmittedReviewFact:
    """One submitted review from a complete read; `commit_id` None is an unresolved commit."""
    review_id: str
    author: str
    state: SubmittedReviewState
    commit_id: str | None
    submitted_at: datetime

    def __post_init__(self) -> None:
        _text(self.review_id, "review")
        _text(self.author, "review author")
        if not isinstance(self.state, SubmittedReviewState):
            raise ValueError("review state must be typed")
        if self.commit_id is not None:
            validate_commit_sha(self.commit_id, "review commit")
        _time(self.submitted_at, "review submission time")


@dataclass(frozen=True)
class CompleteReviews:
    """Every submitted review of the PR, chronologically; only this can decide a cause."""
    reviews: tuple[SubmittedReviewFact, ...]

    def __post_init__(self) -> None:
        _items(self.reviews, "complete reviews", (SubmittedReviewFact,), MAX_OBSERVED_REVIEWS)
        if len({review.review_id for review in self.reviews}) != len(self.reviews):
            raise ValueError("complete reviews contain duplicate identities")
        if any(later.submitted_at < earlier.submitted_at for earlier, later in zip(self.reviews, self.reviews[1:])):
            raise ValueError("complete reviews must be chronological")


@dataclass(frozen=True)
class PartialReviews:
    """A truncated or failed pagination: never disproves nor establishes a ceiling."""
    reason: str

    def __post_init__(self) -> None:
        _text(self.reason, "partial review read", limit=MAX_DETAIL_CHARS)


ReviewRead = CompleteReviews | PartialReviews


def _pr_key(key: object, name: str) -> None:
    if not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef):
        raise ValueError(f"{name} requires a queried pull request")


@dataclass(frozen=True)
class ObservedPullRequest:
    """Head, state and labels survive a review pagination failure as facts.

    Only `CompleteReviews` may establish or disprove a rejection cause; read
    them through `decisive_reviews`, never by inspecting `reviews` directly.
    """
    key: WorkflowKey
    head_sha: str
    state: PullRequestState
    labels: frozenset[Label]
    reviews: ReviewRead
    observed_at: datetime

    def __post_init__(self) -> None:
        _pr_key(self.key, "pull request observation")
        validate_commit_sha(self.head_sha, "observed pull request head")
        if not isinstance(self.state, PullRequestState):
            raise ValueError("observed pull request state must be typed")
        validate_labels(self.labels, "observed pull request labels")
        if not isinstance(self.reviews, (CompleteReviews, PartialReviews)):
            raise ValueError("pull request observation requires a typed review read")
        _time(self.observed_at, "observation time")


def decisive_reviews(observation: ObservedPullRequest) -> CompleteReviews:
    """The only reviews that may establish or disprove a rejection ceiling cause.

    A partial read proves neither the presence nor the absence of a counted
    review, so callers must hold (evidence unavailable) instead of deciding.
    """
    if not isinstance(observation, ObservedPullRequest):
        raise ValueError("review decisions require a pull request observation")
    if not isinstance(observation.reviews, CompleteReviews):
        raise ValueError("a partial review read can neither establish nor disprove a rejection cause")
    return observation.reviews


@dataclass(frozen=True)
class PullRequestAbsent:
    key: WorkflowKey
    observed_at: datetime

    def __post_init__(self) -> None:
        _pr_key(self.key, "pull request absence")
        _time(self.observed_at, "observation time")


@dataclass(frozen=True)
class PullRequestUnavailable:
    key: WorkflowKey
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _pr_key(self.key, "pull request unavailability")
        _time(self.observed_at, "observation time")
        _text(self.reason, "pull request observation failure", limit=MAX_DETAIL_CHARS)


PullRequestObservation = ObservedPullRequest | PullRequestAbsent | PullRequestUnavailable


def _ids(reviews: tuple[CountedReview, ...]) -> frozenset[str]:
    return frozenset(review.review_id for review in reviews)


def _validate_coverage(
    prior: ReviewWorkflowSnapshot, intent: PendingReviewRearm, obligations: tuple[CommandId, ...],
) -> None:
    """The replacement covers every retired dismissal, and the stopped relabel only in its own direction.

    An uncertain relabel that would land the opposite outcome is an
    unresolved conflicting effect: it stays an independent hold (ADR 0015).
    """
    stopped = prior.intent
    relabel = stopped.projection.command_id if isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm)) else None
    required = tuple(c for c in obligations if c != relabel)
    if intent.replaces not in (required, obligations):
        raise ValueError("a review rearm replaces exactly the retired dismissals, and the relabel only when agreed")
    if relabel in intent.replaces:
        assert isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm))
        if stopped.projection.labels != intent.projection.labels:
            raise ValueError("a review rearm covers the stopped relabel only in the same direction")


def _validate_release(prior: ReviewWorkflowSnapshot, after: ReviewWorkflowSnapshot) -> None:
    """A release removes this episode's disproven ceiling and owes a fresh review.

    The head the ceiling held must not stay covered by an older baseline, or
    the PR would be neither held nor reviewed (the keifu #173 dead-state class).
    """
    if after.episode.continuation != FreshReviewRequired(after.revision.fence.generation):
        raise ValueError("a released ceiling owes a fresh review from its new generation")
    if after.episode.guidance != prior.episode.guidance or after.intent != prior.intent:
        raise ValueError("a ceiling release changes only the ceiling gate and the continuation")
    released = tuple(g for g in prior.gates if isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling)
                     and g.cause.episode_id == prior.episode.episode_id)
    if not released:
        raise ValueError("a ceiling release requires an open rejection ceiling of this episode")
    kept = tuple(g for g in prior.gates if g not in released)
    if set(after.gates) != set(kept) or len(after.gates) != len(kept):
        raise ValueError("a ceiling release removes only the episode's rejection ceiling")


def validate_review_change(change: WorkflowChanged) -> None:
    """Review-family provenance that a single snapshot cannot express (ADR 0015).

    The prior snapshot is a witness the commit adapter must prove equal to the
    stored row. A continuation changes only by a recorded bound review or an
    accepted re-arm; a re-arm authorizes exactly the gate's observed counted
    set, and a continuation can only narrow the stopped intent's authorization.
    """
    after, prior = change.snapshot, change.prior
    if not isinstance(after, ReviewWorkflowSnapshot) or not isinstance(prior, ReviewWorkflowSnapshot):
        raise ValueError("review transition requires its exact prior review snapshot")
    if prior.revision != change.expected or prior.authority != after.authority:
        raise ValueError("prior review snapshot disagrees with the expected revision or authority")
    if prior.episode.episode_id != after.episode.episode_id:
        raise ValueError("a review transition cannot replace its episode")
    mutation = change.mutation
    if mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
        if after.episode.continuation != FreshReviewRequired(after.revision.fence.generation):
            raise ValueError("an accepted review rearm owes a fresh review from its new generation")
    elif mutation is WorkflowMutation.RELEASE_CEILING:
        _validate_release(prior, after)
    elif mutation is not WorkflowMutation.RECORD_REVIEW and after.episode.continuation != prior.episode.continuation:
        raise ValueError("only a recorded review, an accepted rearm or a ceiling release may change the review continuation")
    if mutation not in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
        return
    audit, intent = change.audit, after.intent
    if not isinstance(audit, AuthorizedOperatorCommand) or not isinstance(audit.preflight, ReviewRearmPreflight) or not isinstance(intent, PendingReviewRearm):
        raise ValueError("accepted review rearm requires its preflight and pending intent")
    preflight, dismissed = audit.preflight, _ids(intent.dismissals)
    if isinstance(audit.command, RequestRearm):
        gate = next((g for g in prior.gates if isinstance(g, OpenGate) and g.gate_id == audit.command.gate_id), None)
        if gate is None or not isinstance(gate.cause, ReviewRejectionCeiling):
            raise ValueError("a review allowance rearm requires an open review rejection ceiling")
        if not any(isinstance(g, RearmingGate) and g.gate == gate for g in after.gates):
            raise ValueError("the rearming gate must retain the exact accepted ceiling cause")
        cause = gate.cause
        # The operator's new decision supersedes any stopped one (every review
        # decision gate belongs to this PR's single episode); every other gate,
        # including retained uncertainty, stays exactly as it was.
        retained = {g for g in prior.gates if g != gate and not (
            isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision))}
        if set(after.gates) != retained | {RearmingGate(gate, audit.command.request_id)} or len(after.gates) != len(retained) + 1:
            raise ValueError("a review rearm may absorb only stopped decision gates")
        absorbed = tuple(c for g in prior.gates if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision)
                         for c in g.cause.obligations)
        _validate_coverage(prior, intent, absorbed)
        if intent.authorized != cause.counted:
            raise ValueError("a review rearm authorizes exactly the gate's observed counted set")
        if intent.rule != cause.rule:
            raise ValueError("a review rearm counts under its gate's captured rule")
        if preflight.head_sha != cause.head_sha:
            raise ValueError("the pull request head moved since the rejection ceiling opened")
        if not _ids(preflight.counted) <= _ids(cause.counted):
            raise ValueError("the preflight counts reviews outside the gate's observed set")
        if dismissed != _ids(preflight.counted):
            raise ValueError("dismissals must be exactly the freshly counted reviews")
    elif isinstance(audit.command, ContinueGrantedRearm):
        stopped = prior.intent
        if not isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm)) or stopped.request_id != audit.command.stopped_request_id:
            raise ValueError("a review continuation requires the current stopped review intent")
        rearming = next((g for g in after.gates if isinstance(g, RearmingGate) and g.gate.gate_id == intent.gate_id), None)
        if rearming is None or not isinstance(rearming.gate.cause, RearmNeedsDecision) or rearming.gate.cause.stopped_request_id != stopped.request_id:
            raise ValueError("a review continuation rearms the stopped request's decision gate")
        expected_gates = tuple(RearmingGate(rearming.gate, intent.request_id) if g == rearming.gate else g for g in prior.gates)
        if set(after.gates) != set(expected_gates) or len(after.gates) != len(expected_gates):
            raise ValueError("a review continuation rearms only the retained decision gate")
        if not set(intent.authorized) <= set(stopped.authorized):
            raise ValueError("a review continuation cannot widen the stopped authorization")
        if intent.rule != stopped.rule:
            raise ValueError("a review continuation counts under the stopped intent's rule")
        if not _ids(preflight.counted) <= _ids(stopped.authorized):
            raise ValueError("the preflight counts reviews outside the stopped authorization")
        if not dismissed <= _ids(preflight.counted):
            raise ValueError("continued dismissals must be freshly counted reviews")
        assert isinstance(rearming.gate.cause, RearmNeedsDecision)
        _validate_coverage(prior, intent, rearming.gate.cause.obligations)
    else:
        raise ValueError("accepted review rearm requires a request or continuation")
