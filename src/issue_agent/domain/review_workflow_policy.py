"""Pure review-rejection transitions under the ADR 0015 workflow authority.

Mirrors the resolve policy in `workflow_policy` without workspace handoffs.
One selector decides which reviews count toward the ceiling, under a
`CountingRule` captured when the gate opens; the gate records that observed
set, and an operator re-arm authorizes dismissal of exactly it. Only a
complete review read may establish or disprove a cause; head and terminal
facts from any observation still drive supersession. Values and functions
here perform no I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .actions import Target
from .contracts import CommandId, CommandKind, LifecycleCommand, WorkflowAttemptIdentity
from .eligibility import ORDERING_LABELS
from .identity import login_eq
from .labels import Label
from .review import EscalationCause
from .review_rejection import validate_commit_sha
from .review_workflow import (
    MAX_COUNTED_REVIEWS, BoundReviewSubmission, CancelledReviewRearm, CompleteReviews, CompletedReviewRearm,
    CountedReview, CountingRule, DismissCountedReview, EnsureReviewProjection, FreshReviewRequired,
    ObservedPullRequest, PendingReviewRearm, PullRequestAbsent, PullRequestObservation, PullRequestState,
    PullRequestUnavailable, RequireFreshReview, ReviewedBaseline, ReviewEpisode, ReviewProjectionPlan, ReviewRearmPreflight,
    ReviewRejectionCeiling, ReviewWorkflowSnapshot, SubmittedReviewState, SupersededReviewRearm, decisive_reviews,
)
from .workflow import (
    MAX_WORKFLOW_GATES, AuthorizedOperatorCommand, CancelRearm, ClearGuidance, ContinueGrantedRearm,
    NoAuthorizedRemediation, ObservedTransition, OpenGate, OperatorActor, RearmingGate, RearmNeedsDecision,
    RemoteOutcomeUncertain, RequestRearm, RetryAuthorizationRequired, SetGuidance, StopReason, UnresolvedGate,
    WorkflowChanged, WorkflowMutation, WorkflowRejected, WorkflowRejection, WorkflowTransition, WorkflowUnchanged,
)
from .workflow_identity import (
    MAX_DETAIL_CHARS, FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
    validate_workflow_identifier, validate_workflow_text,
)

# Operator-path observations must be read under the acceptance barrier; the
# same bound as the resolve re-arm.
OBSERVATION_MAX_AGE_SECONDS = 300
DISMISSAL_MESSAGE = "An operator re-armed the review rejection ceiling; a fresh review is owed."


# -- counted-set selector ------------------------------------------------------

def counted_rejections(reviews: CompleteReviews, rule: CountingRule) -> tuple[CountedReview, ...]:
    """The review bot's CHANGES_REQUESTED reviews after its last resetting APPROVE, oldest first.

    Exactly `review.count_prior_rejections` with identities: an approval on a
    commit the rule proves unmergeable is not a reset. Commented and dismissed
    reviews never count. A rejection whose commit is unresolved still counts;
    counting and dismissal never need its commit.
    """
    if not isinstance(reviews, CompleteReviews):
        raise ValueError("only a complete review read can select counted rejections")
    if not isinstance(rule, CountingRule):
        raise ValueError("counting requires a typed counting rule")
    excluded = frozenset(rule.non_resetting_commits)
    mine = [review for review in reviews.reviews if login_eq(review.author, rule.review_bot)]
    last_reset = max(
        (index for index, review in enumerate(mine)
         if review.state is SubmittedReviewState.APPROVED and review.commit_id not in excluded),
        default=-1,
    )
    return tuple(
        CountedReview(review.review_id, review.commit_id)
        for review in mine[last_reset + 1:]
        if review.state is SubmittedReviewState.CHANGES_REQUESTED
    )


def _ceiling_value(ceiling: object) -> int:
    if type(ceiling) is not int or not -(2**63) <= ceiling <= 2**63 - 1:
        raise ValueError("review ceiling must be a bounded integer")
    return ceiling


def _reaches_ceiling(counted: tuple[CountedReview, ...], ceiling: int) -> bool:
    # The escalation replaces the `ceiling`-th rejection.
    return _ceiling_value(ceiling) > 0 and len(counted) + 1 >= ceiling


def review_ceiling_cause(
    pr: PullRequestObservation, *, episode_id: str, rule: CountingRule, ceiling: int,
) -> ReviewRejectionCeiling | None:
    """The cause the next bot rejection would escalate on, or None.

    `ceiling <= 0` disables the ceiling. Only a complete review read decides:
    absent, unavailable or partial observations raise instead of reading as
    "no cause".
    """
    if not isinstance(pr, ObservedPullRequest):
        raise ValueError("a rejection cause requires a present pull request observation")
    if _ceiling_value(ceiling) <= 0:
        return None
    counted = counted_rejections(decisive_reviews(pr), rule)
    if not _reaches_ceiling(counted, ceiling):
        return None
    return ReviewRejectionCeiling(episode_id, pr.head_sha, rule.review_bot, counted, ceiling,
                                  rule.non_resetting_commits)


def _ceiling_cleared(counted: tuple[CountedReview, ...], ceiling: int) -> bool:
    """Whether fresh complete reviews show no standing ceiling after a re-arm.

    An empty counted set is no standing rejection even at ceiling 1, where
    every rejection escalates by itself; otherwise ceiling 1 could never
    complete a re-arm and would be an invisible dead state.
    """
    return not counted or not _reaches_ceiling(counted, ceiling)


# -- identities ----------------------------------------------------------------

def _digest(*parts: object) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def _pr_key(key: object) -> WorkflowKey:
    if not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef):
        raise ValueError("review policy requires a pull request workflow key")
    return key


def review_ceiling_gate_id(key: WorkflowKey, cause: ReviewRejectionCeiling, opened: WorkflowRevision) -> str:
    """Deterministic from the revision the gate opens at, the episode, head and counted reviews.

    The opening generation and version keep a reopened identical cause (after
    a cancelled re-arm, a released ceiling, or a head that moved A->B->A in one
    generation) from rematching a gate ID in history (ADR 0015).
    """
    key = _pr_key(key)
    if not isinstance(cause, ReviewRejectionCeiling):
        raise ValueError("review ceiling gate requires its cause")
    if not isinstance(opened, WorkflowRevision) or opened.fence.key != key:
        raise ValueError("review ceiling gate requires the revision it opens at")
    ids = "\0".join(review.review_id for review in cause.counted)
    return "review-ceiling:" + _digest(key.repository, key.target.number, opened.fence.generation, opened.version,
                                       cause.episode_id, cause.head_sha, ids)


def review_projection_command_id(key: WorkflowKey, request_id: str) -> CommandId:
    key = _pr_key(key)
    validate_workflow_identifier(request_id, "request")
    return CommandId(_digest(key.repository, "PullRequestRef", key.target.number, request_id, "review_projection"))


def review_release_command_id(key: WorkflowKey, generation: int) -> CommandId:
    """One release per generation: a release is the only non-rearm generation advance."""
    key = _pr_key(key)
    if type(generation) is not int or generation < 1:
        raise ValueError("a ceiling release projection belongs to an advanced generation")
    return CommandId(_digest(key.repository, "PullRequestRef", key.target.number, generation, "release_ceiling"))


def review_dismissal_command_id(key: WorkflowKey, request_id: str, review_id: str) -> CommandId:
    """Stable per (workflow, request, review) so a replayed intent deduplicates."""
    key = _pr_key(key)
    validate_workflow_identifier(request_id, "request")
    validate_workflow_identifier(review_id, "review")
    return CommandId(_digest(key.repository, "PullRequestRef", key.target.number, request_id, review_id, "dismiss_review"))


def _identity(snapshot: ReviewWorkflowSnapshot, kind: str, detail: str) -> str:
    key = snapshot.revision.fence.key
    return f"{kind}:{_digest(key.repository, 'PullRequestRef', key.target.number, kind, detail)}"


def initial_review_workflow(key: WorkflowKey, authority: FamilyAuthority) -> ReviewWorkflowSnapshot:
    """Initial state for a proven-new PR target; never a substitute for a read."""
    key = _pr_key(key)
    if not isinstance(authority, FamilyAuthority) or authority.family is not WorkflowFamily.REVIEW_REJECTIONS:
        raise ValueError("review workflow discovery requires review-family authority")
    episode_id = "review-initial:" + _digest(key.repository, key.target.number, authority.family.value,
                                              authority.epoch, "initial")
    return ReviewWorkflowSnapshot(WorkflowRevision(WorkflowFence(key, 0), 0), authority, ReviewEpisode(episode_id))


# -- harness-owned submission facts ---------------------------------------------

def _patch_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a SHA-256 digest")


@dataclass(frozen=True)
class ReviewSubmissionReceipt:
    """A verified SUBMIT_REVIEW: its bound command, reviewed head/patch and receipt.

    Minted by the harness from its own command and effect receipt, never from
    agent output; the only input that may establish a reviewed baseline.
    """
    submission: BoundReviewSubmission
    head_sha: str
    patch_id: str
    receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.submission, BoundReviewSubmission):
            raise ValueError("review receipt requires its bound submission")
        validate_commit_sha(self.head_sha, "reviewed head")
        _patch_digest(self.patch_id, "reviewed patch identity")
        validate_workflow_identifier(self.receipt_id, "review receipt")


@dataclass(frozen=True)
class CeilingEscalation:
    """The SUBMIT_REVIEW receipt of an `EscalatedSubmission`; only cause "ceiling" opens a gate."""
    submission: BoundReviewSubmission
    head_sha: str
    receipt_id: str
    escalate_cause: EscalationCause

    def __post_init__(self) -> None:
        if not isinstance(self.submission, BoundReviewSubmission):
            raise ValueError("escalation requires its bound submission")
        validate_commit_sha(self.head_sha, "escalated head")
        validate_workflow_identifier(self.receipt_id, "escalation receipt")
        if self.escalate_cause not in ("verdict", "ceiling"):
            raise ValueError("escalation requires a recognized cause")


def _evidence(required: tuple[str, ...], extra: object) -> tuple[str, ...]:
    """Required evidence first; idempotent when re-applied to its own output."""
    if not isinstance(extra, tuple):
        raise ValueError("transition evidence must be a tuple")
    return tuple(dict.fromkeys(required + extra))


# -- shared helpers ------------------------------------------------------------

def _reject(code: WorkflowRejection, reason: str) -> WorkflowRejected:
    return WorkflowRejected(code, reason)


def _open(gate: UnresolvedGate) -> OpenGate:
    return gate.gate if isinstance(gate, RearmingGate) else gate


def _next(snapshot: ReviewWorkflowSnapshot, *, advance_generation: bool = False) -> WorkflowRevision:
    current = snapshot.revision
    return WorkflowRevision(
        WorkflowFence(current.fence.key, current.fence.generation + int(advance_generation)), current.version + 1,
    )


def _review_snapshot(snapshot: object) -> ReviewWorkflowSnapshot:
    if not isinstance(snapshot, ReviewWorkflowSnapshot):
        raise ValueError("review policy requires a review-rejection workflow snapshot")
    return snapshot


def _aware(now: object) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("review policy requires an aware current time")
    return now


def _ids(reviews: tuple[CountedReview, ...]) -> frozenset[str]:
    return frozenset(review.review_id for review in reviews)


def _holds(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_WORKFLOW_GATES:
        raise ValueError("external holds must be a bounded tuple of reasons")
    for hold in value:
        validate_workflow_text(hold, "external hold", limit=MAX_DETAIL_CHARS)
    return value


def _trigger(label: object) -> Label:
    if not isinstance(label, Label) or label is Label.NEEDS_HUMAN_REVIEW:
        raise ValueError("review trigger must be a label other than the attention hold")
    return label


def _observed(
    snapshot: ReviewWorkflowSnapshot, pr: PullRequestObservation, now: datetime, *, complete: bool,
) -> WorkflowRejected | None:
    """Scope, presence, freshness, open state and (optionally) review completeness."""
    _aware(now)
    if not isinstance(pr, (ObservedPullRequest, PullRequestAbsent, PullRequestUnavailable)):
        raise ValueError("review policy requires a typed pull request observation")
    if pr.key != snapshot.revision.fence.key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "pull request observation belongs to a different workflow")
    if isinstance(pr, PullRequestAbsent):
        return _reject(WorkflowRejection.INVALID_SCOPE, "the pull request is verified absent")
    if isinstance(pr, PullRequestUnavailable):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "a present pull request observation is required")
    if not 0 <= (now - pr.observed_at).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS:
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "pull request observation is stale or from the future")
    if pr.state is not PullRequestState.OPEN:
        return _reject(WorkflowRejection.INVALID_SCOPE, "the pull request is terminal")
    if complete and not isinstance(pr.reviews, CompleteReviews):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE,
                       "a partial review read can neither establish nor disprove a rejection cause")
    return None


def _active(active_reviews: object) -> WorkflowRejected | None:
    if not isinstance(active_reviews, tuple) or any(not isinstance(a, WorkflowAttemptIdentity) for a in active_reviews):
        raise ValueError("active review evidence must be typed")
    if active_reviews:
        return _reject(WorkflowRejection.OTHER_HOLD, "active review runs must drain before a re-arm")
    return None


def _episode_ceilings(snapshot: ReviewWorkflowSnapshot) -> tuple[OpenGate, ...]:
    return tuple(g for g in snapshot.gates if isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling)
                 and g.cause.episode_id == snapshot.episode.episode_id)


def _is_decision(gate: UnresolvedGate) -> bool:
    return isinstance(gate, OpenGate) and isinstance(gate.cause, RearmNeedsDecision)


def _ceiling_gate_for(snapshot: ReviewWorkflowSnapshot, cause: ReviewRejectionCeiling) -> OpenGate:
    opened = _next(snapshot)
    return OpenGate(review_ceiling_gate_id(snapshot.revision.fence.key, cause, opened), opened.fence.generation,
                    cause, NoAuthorizedRemediation("dismissing the counted reviews requires an operator decision"))


def _gate_change(snapshot: ReviewWorkflowSnapshot, gates: tuple[UnresolvedGate, ...],
                 evidence_ids: tuple[str, ...]) -> WorkflowTransition:
    if len(gates) > MAX_WORKFLOW_GATES:
        return _reject(WorkflowRejection.OTHER_HOLD, "the workflow cannot hold another gate")
    after = replace(snapshot, revision=_next(snapshot), gates=gates)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.OPEN_GATE, after,
                           ObservedTransition(evidence_ids), prior=snapshot)


# -- ceiling gates ---------------------------------------------------------------

def open_review_ceiling(
    snapshot: ReviewWorkflowSnapshot, pr: PullRequestObservation, *, now: datetime, rule: CountingRule,
    ceiling: int, escalation: CeilingEscalation, evidence_ids: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Open a NEW ceiling gate, proven by the harness's own ceiling escalation.

    `rule` is the configured counting rule, captured into the cause. An
    existing same-episode ceiling is refreshed only by `reconcile_review_ceiling`.
    """
    snapshot = _review_snapshot(snapshot)
    if not isinstance(escalation, CeilingEscalation):
        raise ValueError("a new review ceiling requires its escalation receipt")
    failure = _observed(snapshot, pr, now, complete=True)
    if failure is not None:
        return failure
    assert isinstance(pr, ObservedPullRequest)
    if isinstance(snapshot.intent, PendingReviewRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a review re-arm already owns this workflow")
    if escalation.escalate_cause != "ceiling":
        return _reject(WorkflowRejection.INVALID_SCOPE, "only a ceiling escalation opens a rejection ceiling")
    if escalation.submission.binding != snapshot.binding or escalation.head_sha != pr.head_sha:
        return _reject(WorkflowRejection.INVALID_SCOPE, "the escalation belongs to another generation, episode or head")
    cause = review_ceiling_cause(pr, episode_id=snapshot.episode.episode_id, rule=rule, ceiling=ceiling)
    if cause is None:
        return _reject(WorkflowRejection.NOT_EXHAUSTED, "fresh complete reviews do not reach the rejection ceiling")
    existing = _episode_ceilings(snapshot)
    if any(g.cause == cause for g in existing):
        return WorkflowUnchanged(snapshot)
    if existing:
        return _reject(WorkflowRejection.INVALID_SCOPE, "an existing ceiling is refreshed by reconciliation")
    required = (str(escalation.submission.command_id), escalation.receipt_id)
    return _gate_change(snapshot, snapshot.gates + (_ceiling_gate_for(snapshot, cause),),
                        _evidence(required, evidence_ids))


def _captured_rule(snapshot: ReviewWorkflowSnapshot, existing: tuple[OpenGate, ...]) -> CountingRule | None:
    """The rule of this episode's open ceiling, else of a stopped re-arm.

    A stopped re-arm always awaits a decision: the snapshot type keeps its
    decision gate open for exactly as long as it is the current intent.
    """
    if existing:
        cause = existing[-1].cause
        assert isinstance(cause, ReviewRejectionCeiling)
        return cause.rule
    stopped = snapshot.intent
    if isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm)):
        return stopped.rule
    return None


def reconcile_review_ceiling(
    snapshot: ReviewWorkflowSnapshot, pr: PullRequestObservation, *, now: datetime, ceiling: int,
    evidence_ids: tuple[str, ...], trigger_label: Label, executor_identity: str,
    external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Re-evaluate this episode's ceiling from fresh complete reviews under the captured rule.

    - An open ceiling gate whose cause is now false (count+1 < ceiling, or the
      ceiling is disabled) is released (RELEASE_CEILING): a human dismissing
      reviews or a lowered configuration must not leave an unreleasable hold.
      The release advances the generation, owes a fresh review from it and
      projects the PR labels; otherwise the old baseline would still cover the
      held head and the PR would be neither held nor reviewed (keifu #173).
    - An open ceiling gate whose cause changed but is still true (a moved head,
      a different counted set) is replaced in the same generation (OPEN_GATE).
    - A stopped re-arm's decision gate with a true fresh cause gains a fresh
      ceiling gate, so the operator can authorize the new counted set.
    A pending re-arm owns the workflow: its completion or stop reconciles
    first, even when a ceiling gate it did not select is still open.
    Nothing to reconcile is `WorkflowUnchanged`.
    """
    snapshot = _review_snapshot(snapshot)
    _trigger(trigger_label)
    holds = _holds(external_holds)
    if isinstance(snapshot.intent, PendingReviewRearm):
        return WorkflowUnchanged(snapshot)
    existing = _episode_ceilings(snapshot)
    rule = _captured_rule(snapshot, existing)
    if rule is None:
        return WorkflowUnchanged(snapshot)
    failure = _observed(snapshot, pr, now, complete=True)
    if failure is not None:
        return failure
    assert isinstance(pr, ObservedPullRequest)
    cause = review_ceiling_cause(pr, episode_id=snapshot.episode.episode_id, rule=rule, ceiling=ceiling)
    kept = tuple(g for g in snapshot.gates if g not in existing)
    if cause is None:
        if not existing:
            return WorkflowUnchanged(snapshot)
        return _release(snapshot, pr, kept, evidence_ids, holds,
                        trigger_label=trigger_label, executor_identity=executor_identity)
    if len(existing) == 1 and existing[0].cause == cause:
        return WorkflowUnchanged(snapshot)
    return _gate_change(snapshot, kept + (_ceiling_gate_for(snapshot, cause),), evidence_ids)


def _release(
    snapshot: ReviewWorkflowSnapshot, pr: ObservedPullRequest, kept: tuple[UnresolvedGate, ...],
    evidence_ids: tuple[str, ...], external_holds: tuple[str, ...], *, trigger_label: Label, executor_identity: str,
) -> WorkflowChanged:
    revision = _next(snapshot, advance_generation=True)
    episode = replace(snapshot.episode, continuation=FreshReviewRequired(revision.fence.generation))
    after = replace(snapshot, revision=revision, episode=episode, gates=kept)
    key = revision.fence.key
    plan = _plan(review_release_command_id(key, revision.fence.generation), bool(kept) or bool(external_holds),
                 trigger_label)
    projection = _relabel(after, plan, f"workflow:release:{revision.fence.generation}", pr.head_sha, pr.labels,
                          executor_identity=executor_identity)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.RELEASE_CEILING, after,
                           ObservedTransition(evidence_ids), (projection,), prior=snapshot)


# -- review completion and dispatch ---------------------------------------------

def record_review(snapshot: ReviewWorkflowSnapshot, receipt: ReviewSubmissionReceipt) -> WorkflowTransition:
    """A verified SUBMIT_REVIEW of the current generation becomes the reviewed baseline.

    A submission bound to an older generation or another episode cannot
    repopulate the baseline (ADR 0015); gates and guidance are untouched.
    """
    snapshot = _review_snapshot(snapshot)
    if not isinstance(receipt, ReviewSubmissionReceipt):
        raise ValueError("recorded review requires its harness-owned submission receipt")
    submission = receipt.submission
    if submission.binding != snapshot.binding:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the review was bound to another generation or episode and cannot establish the baseline")
    if isinstance(snapshot.intent, PendingReviewRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a pending review re-arm owes a fresh review first")
    baseline = ReviewedBaseline(receipt.head_sha, receipt.patch_id, snapshot.revision.fence.generation, submission)
    if snapshot.episode.continuation == baseline:
        return WorkflowUnchanged(snapshot)
    after = replace(snapshot, revision=_next(snapshot), episode=replace(snapshot.episode, continuation=baseline))
    return WorkflowChanged(snapshot.revision, WorkflowMutation.RECORD_REVIEW, after,
                           ObservedTransition(_evidence((str(submission.command_id), receipt.receipt_id), ())),
                           prior=snapshot)


def _patch(value: object) -> str | None:
    if value is None:
        return None
    _patch_digest(value, "patch identity")
    assert isinstance(value, str)
    return value


def review_owed(snapshot: ReviewWorkflowSnapshot, head_sha: str, patch_id: str | None) -> bool:
    """Whether the current head needs a paid review.

    A fresh-review obligation is never discharged by patch reuse. A
    same-generation baseline covers an unchanged head, or a moved head whose
    patch is unchanged; an unknown patch (None) never matches.
    """
    snapshot = _review_snapshot(snapshot)
    validate_commit_sha(head_sha, "pull request head")
    patch = _patch(patch_id)
    continuation = snapshot.episode.continuation
    if isinstance(continuation, FreshReviewRequired):
        return True
    if continuation.reviewed_generation != snapshot.revision.fence.generation:
        return True
    if continuation.head_sha == head_sha:
        return False
    return patch != continuation.patch_id


class ReviewKind(str, Enum):
    INITIAL = "initial"
    RE_REVIEW = "re_review"


@dataclass(frozen=True)
class ReviewHeld:
    gate_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReviewOwed:
    kind: ReviewKind


@dataclass(frozen=True)
class ReviewNotOwed:
    reason: str


ReviewDispatch = ReviewHeld | ReviewOwed | ReviewNotOwed


def review_dispatch_decision(
    snapshot: ReviewWorkflowSnapshot | None, pr: ObservedPullRequest, *, patch_id: str | None,
) -> ReviewDispatch:
    """Held beats owed. `None` is a proven-absent workflow (initial review owed).

    Caller contract, owed by increments 5-6 and not enforced here: after a
    RELEASE_CEILING, dispatch stays held until the release projection command
    is verified; and a refused or uncertain release projection must surface as
    a visible gate, never a silent label mismatch.
    """
    if not isinstance(pr, ObservedPullRequest):
        raise ValueError("review dispatch requires a present pull request observation")
    if snapshot is None:
        if pr.state is not PullRequestState.OPEN:
            return ReviewNotOwed("the pull request is terminal")
        return ReviewOwed(ReviewKind.INITIAL)
    snapshot = _review_snapshot(snapshot)
    if pr.key != snapshot.revision.fence.key:
        raise ValueError("review dispatch observation belongs to another workflow")
    if snapshot.gates:
        return ReviewHeld(tuple(_open(g).gate_id for g in snapshot.gates))
    if pr.state is not PullRequestState.OPEN:
        return ReviewNotOwed("the pull request is terminal")
    if not review_owed(snapshot, pr.head_sha, patch_id):
        return ReviewNotOwed("the reviewed baseline covers this head")
    initial = snapshot.episode.continuation == FreshReviewRequired(0)
    return ReviewOwed(ReviewKind.INITIAL if initial else ReviewKind.RE_REVIEW)


# -- commands -------------------------------------------------------------------

@dataclass(frozen=True)
class PullRequestHoldGuard:
    """ADR 0015 semantic fence for a PR hold projection, mirroring `IssueHoldGuard`.

    The open state (implied: a guard is minted only from an open PR), the head,
    and every hold/lifecycle label. Comments, title and body are not hold facts.
    Priority labels only order queued work and are excluded.
    """
    head_sha: str
    labels: frozenset[Label]

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "hold guard head")
        if not isinstance(self.labels, frozenset) or any(not isinstance(label, Label) for label in self.labels):
            raise ValueError("hold guard labels must be a frozenset of labels")
        if self.labels & ORDERING_LABELS:
            raise ValueError("hold guard labels exclude ordering-only labels")


def pull_request_hold_guard(pr: ObservedPullRequest) -> PullRequestHoldGuard:
    if not isinstance(pr, ObservedPullRequest) or pr.state is not PullRequestState.OPEN:
        raise ValueError("a hold projection guard requires an open observed pull request")
    return PullRequestHoldGuard(pr.head_sha, frozenset(pr.labels) - ORDERING_LABELS)


def pull_request_hold_guard_applies(
    guard: PullRequestHoldGuard, pr: ObservedPullRequest, *,
    removed: tuple[Label, ...] = (), added: tuple[Label, ...] = (),
) -> bool:
    """The accepted hold facts still hold, modulo this command's own label effects.

    A partial review read does not matter here: head, state and labels are
    facts of any present observation.
    """
    if not isinstance(guard, PullRequestHoldGuard) or not isinstance(pr, ObservedPullRequest):
        raise ValueError("hold guard evaluation requires a typed guard and observation")
    if pr.state is not PullRequestState.OPEN or pr.head_sha != guard.head_sha:
        return False
    current = frozenset(pr.labels) - ORDERING_LABELS
    return not (current - guard.labels - set(added) or guard.labels - current - set(removed))


def _pending(snapshot: ReviewWorkflowSnapshot) -> PendingReviewRearm:
    if not isinstance(snapshot.intent, PendingReviewRearm):
        raise ValueError("review command requires a pending review re-arm")
    return snapshot.intent


def _covered(gate: UnresolvedGate, covered: tuple[CommandId, ...]) -> bool:
    cause = _open(gate).cause
    return isinstance(cause, (RemoteOutcomeUncertain, RetryAuthorizationRequired)) and cause.command_id in covered


def _held(gates: tuple[UnresolvedGate, ...], selected_gate_id: str, covered: tuple[CommandId, ...],
          external_holds: tuple[str, ...]) -> bool:
    return bool(external_holds) or any(
        _open(g).gate_id != selected_gate_id and not _covered(g, covered) for g in gates
    )


def _retired_uncertainty(snapshot: ReviewWorkflowSnapshot, gate: UnresolvedGate) -> bool:
    """An uncertainty gate over an obligation this re-arm replaces is not an independent hold.

    Covers both a continuation of the stopped decision and a new request that
    absorbed it, when the prior effect would land the same outcome; the gate
    stays until reconciliation resolves that effect (ADR 0015).
    """
    return _covered(gate, _pending(snapshot).replaces)


def _remaining_holds(snapshot: ReviewWorkflowSnapshot, external_holds: tuple[str, ...]) -> bool:
    pending = _pending(snapshot)
    return _held(snapshot.gates, pending.gate_id, pending.replaces, external_holds)


def _plan(command_id: CommandId, held: bool, trigger_label: Label) -> ReviewProjectionPlan:
    trigger = _trigger(trigger_label)
    if held:
        return ReviewProjectionPlan(command_id, None, Label.NEEDS_HUMAN_REVIEW)
    return ReviewProjectionPlan(command_id, Label.NEEDS_HUMAN_REVIEW, trigger)


def _replacement(
    snapshot: ReviewWorkflowSnapshot, gates: tuple[UnresolvedGate, ...], selected_gate_id: str,
    obligations: tuple[CommandId, ...], projection_id: CommandId, external_holds: tuple[str, ...],
    trigger_label: Label,
) -> tuple[tuple[CommandId, ...], ReviewProjectionPlan]:
    """Which retired obligations the replacement covers, and the projection it plans.

    Retired dismissals are idempotent and always covered. The stopped relabel
    is covered only when it would land the replacement's own labels; an
    uncertain relabel in the other direction is an unresolved conflicting
    effect and stays an independent hold, so the replacement is held (ADR 0015).
    """
    holds = _holds(external_holds)
    intended = _plan(projection_id, _held(gates, selected_gate_id, obligations, holds), trigger_label)
    stopped = snapshot.intent
    if not isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm)):
        return obligations, intended
    relabel = stopped.projection.command_id
    if relabel not in obligations or stopped.projection.labels == intended.labels:
        return obligations, intended
    covered = tuple(c for c in obligations if c != relabel)
    return covered, _plan(projection_id, _held(gates, selected_gate_id, covered, holds), trigger_label)


def _projection(snapshot: ReviewWorkflowSnapshot, head_sha: str, labels: frozenset[Label], *,
                executor_identity: str) -> LifecycleCommand:
    pending = _pending(snapshot)
    return _relabel(snapshot, pending.projection, f"workflow:{pending.request_id}", head_sha, labels,
                    executor_identity=executor_identity)


def _relabel(
    snapshot: ReviewWorkflowSnapshot, plan: ReviewProjectionPlan, idempotency_key: str, head_sha: str,
    labels: frozenset[Label], *, executor_identity: str,
) -> LifecycleCommand:
    """Release the attention hold and add the trigger, or keep (and ensure) the hold, as planned."""
    guard = PullRequestHoldGuard(head_sha, frozenset(labels) - ORDERING_LABELS)
    key = snapshot.revision.fence.key
    payload = {
        "target": Target.PR.value, "number": key.target.number,
        "remove": plan.remove.value if plan.remove else None, "add": plan.add.value,
        "expected_revision": head_sha, "harness_signal": False, "removes": [], "completion_guard": None,
        "pull_request_hold_guard": {"head_sha": guard.head_sha, "labels": sorted(label.value for label in guard.labels)},
    }
    return LifecycleCommand(
        plan.command_id, idempotency_key, CommandKind.RELABEL, f"pr:{key.target.number}", head_sha, payload,
        repository=key.repository, executor_identity=executor_identity, workflow=snapshot.binding,
    )


def review_rearm_projection(
    snapshot: ReviewWorkflowSnapshot, pr: ObservedPullRequest, *, executor_identity: str,
) -> LifecycleCommand:
    """The pending re-arm's planned relabel, fenced by the semantic `PullRequestHoldGuard`.

    The labels were decided at acceptance and recorded on the intent: with
    remaining gates, external holds or a conflicting uncertain relabel,
    `needs-human-review` is kept (and ensured) and nothing else is added.
    """
    snapshot = _review_snapshot(snapshot)
    _pending(snapshot)
    if not isinstance(pr, ObservedPullRequest) or pr.key != snapshot.revision.fence.key:
        raise ValueError("review projection requires its accepted pull request observation")
    if pr.state is not PullRequestState.OPEN:
        raise ValueError("review projection requires an open pull request")
    return _projection(snapshot, pr.head_sha, pr.labels, executor_identity=executor_identity)


@dataclass(frozen=True)
class ReviewDismissalGuard:
    """A dismissal applies only to a present CHANGES_REQUESTED bot review at the authorized head."""
    review_id: str
    review_bot: str
    head_sha: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.review_id, "dismissed review")
        validate_workflow_identifier(self.review_bot, "review bot")
        validate_commit_sha(self.head_sha, "dismissal head")


class DismissalCheck(str, Enum):
    APPLIES = "applies"
    POSTCONDITION_PRESENT = "postcondition_present"
    REFUSED = "refused"


def dismissal_check(guard: ReviewDismissalGuard, pr: ObservedPullRequest) -> DismissalCheck:
    """Executor-side decision over a complete read.

    A missing review is a refusal, never success; a moved head or terminal PR
    is a changed guard and buys no write.
    """
    if not isinstance(guard, ReviewDismissalGuard) or not isinstance(pr, ObservedPullRequest):
        raise ValueError("dismissal applicability requires its guard and a present observation")
    reviews = decisive_reviews(pr)
    review = next((r for r in reviews.reviews if r.review_id == guard.review_id), None)
    if review is not None and review.state is SubmittedReviewState.DISMISSED and login_eq(review.author, guard.review_bot):
        return DismissalCheck.POSTCONDITION_PRESENT
    if pr.state is not PullRequestState.OPEN or pr.head_sha != guard.head_sha:
        return DismissalCheck.REFUSED
    if review is None or not login_eq(review.author, guard.review_bot):
        return DismissalCheck.REFUSED
    if review.state is SubmittedReviewState.CHANGES_REQUESTED:
        return DismissalCheck.APPLIES
    return DismissalCheck.REFUSED


def dismissal_command(
    snapshot: ReviewWorkflowSnapshot, review: CountedReview, *, head_sha: str, executor_identity: str,
) -> LifecycleCommand:
    """The workflow-owned DISMISS_REVIEW of one authorized obligation, targeting `pr:N`.

    `head_sha` is the head the dismissal was authorized against (the accepted
    preflight's); the bot identity comes from the intent's captured rule.
    """
    snapshot = _review_snapshot(snapshot)
    pending = _pending(snapshot)
    if not isinstance(review, CountedReview) or review not in pending.dismissals:
        raise ValueError("only a dismissal obligation of the pending re-arm can become a command")
    guard = ReviewDismissalGuard(review.review_id, pending.rule.review_bot, head_sha)
    key = snapshot.revision.fence.key
    payload = {
        "pr": key.target.number, "review_id": review.review_id, "message": DISMISSAL_MESSAGE,
        "guard": {"review_id": guard.review_id, "author": guard.review_bot, "head_sha": guard.head_sha,
                  "state": SubmittedReviewState.CHANGES_REQUESTED.value},
    }
    return LifecycleCommand(
        review_dismissal_command_id(key, pending.request_id, review.review_id),
        f"workflow:{pending.request_id}:dismiss:{review.review_id}", CommandKind.DISMISS_REVIEW,
        f"pr:{key.target.number}", head_sha, payload,
        repository=key.repository, executor_identity=executor_identity, workflow=snapshot.binding,
    )


def _rearm_commands(
    after: ReviewWorkflowSnapshot, preflight: ReviewRearmPreflight, *, executor_identity: str,
) -> tuple[LifecycleCommand, ...]:
    pending = _pending(after)
    dismissals = tuple(dismissal_command(after, review, head_sha=preflight.head_sha, executor_identity=executor_identity)
                       for review in pending.dismissals)
    projection = _projection(after, preflight.head_sha, preflight.labels, executor_identity=executor_identity)
    return dismissals + (projection,)


def _pending_intent(
    snapshot: ReviewWorkflowSnapshot, request_id: str, gate_id: str, dismissals: tuple[CountedReview, ...],
    authorized: tuple[CountedReview, ...], rule: CountingRule, projection: ReviewProjectionPlan,
    replaces: tuple[CommandId, ...],
) -> PendingReviewRearm:
    key = snapshot.revision.fence.key
    pr = key.target
    assert isinstance(pr, PullRequestRef)
    generation = snapshot.revision.fence.generation
    return PendingReviewRearm(
        request_id, gate_id, generation, generation + 1,
        tuple(DismissCountedReview(pr, review) for review in dismissals)
        + (RequireFreshReview(pr), EnsureReviewProjection(pr)),
        tuple(review_dismissal_command_id(key, request_id, review.review_id) for review in dismissals)
        + (review_projection_command_id(key, request_id),),
        authorized, rule, projection, replaces,
    )


# -- operator re-arm -------------------------------------------------------------

def review_ceiling_rearm_refusal(gate: UnresolvedGate) -> WorkflowRejected | None:
    """The visible reason a review ceiling gate cannot be re-armed as observed, if any.

    Shared by acceptance and the operator view so an oversize observed set is
    never an invisible dead state; once the reviews are dismissed on GitHub,
    `reconcile_review_ceiling` closes or refreshes the gate.
    """
    cause = _open(gate).cause
    if isinstance(cause, ReviewRejectionCeiling) and len(cause.counted) > MAX_COUNTED_REVIEWS:
        return _reject(
            WorkflowRejection.AUTHORIZATION_BOUND_EXCEEDED,
            f"{len(cause.counted)} counted reviews exceed the {MAX_COUNTED_REVIEWS}-dismissal bound of one re-arm; "
            "dismiss some on GitHub so reconciliation can refresh the ceiling",
        )
    return None


def _preflight(
    snapshot: ReviewWorkflowSnapshot, pr: ObservedPullRequest, counted: tuple[CountedReview, ...],
    evidence_id: str, external_holds: tuple[str, ...],
) -> ReviewRearmPreflight:
    return ReviewRearmPreflight(snapshot.revision.fence.key, pr.head_sha, evidence_id, counted, pr.state,
                                pr.labels, pr.observed_at, _holds(external_holds))


def _guidance(snapshot: ReviewWorkflowSnapshot, command: RequestRearm) -> str:
    if isinstance(command.guidance, SetGuidance):
        return command.guidance.text
    if isinstance(command.guidance, ClearGuidance):
        return ""
    return snapshot.episode.guidance


def request_review_rearm(
    snapshot: ReviewWorkflowSnapshot, command: RequestRearm, actor: OperatorActor, pr: PullRequestObservation,
    *, now: datetime, active_reviews: tuple[WorkflowAttemptIdentity, ...], evidence_id: str,
    trigger_label: Label, executor_identity: str, external_holds: tuple[str, ...] = (),
    prior_request: RequestRearm | None = None,
) -> WorkflowTransition:
    """Authorize dismissal of exactly the gate's observed counted set.

    Fresh counting uses the rule captured in the cause. The new decision
    absorbs any stopped re-arm's decision gate; other gates stay.
    """
    if not isinstance(snapshot, ReviewWorkflowSnapshot):
        return _reject(WorkflowRejection.INVALID_SCOPE, "review re-arm names a workflow of another family")
    if not isinstance(command, RequestRearm):
        raise ValueError("review re-arm requires a typed request")
    if command.expected.fence.key != snapshot.revision.fence.key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "request belongs to a different workflow")
    if prior_request is not None:
        if prior_request != command:
            return _reject(WorkflowRejection.INVALID_SCOPE, "request ID was reused with different content")
        return WorkflowUnchanged(snapshot)
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before re-arm acceptance")
    if isinstance(snapshot.intent, PendingReviewRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a review re-arm already owns this workflow")
    failure = _active(active_reviews) or _observed(snapshot, pr, now, complete=True)
    if failure is not None:
        return failure
    assert isinstance(pr, ObservedPullRequest) and isinstance(pr.reviews, CompleteReviews)
    gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == command.gate_id), None)
    if gate is None or not isinstance(gate.cause, ReviewRejectionCeiling) or (
        gate.cause.episode_id != snapshot.episode.episode_id
    ):
        return _reject(WorkflowRejection.NOT_EXHAUSTED, "the selected gate is not an open review ceiling of this episode")
    refusal = review_ceiling_rearm_refusal(gate)
    if refusal is not None:
        return refusal
    cause = gate.cause
    if pr.head_sha != cause.head_sha:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the decision changed: the head moved since the ceiling opened; a refreshed ceiling is required")
    counted = counted_rejections(pr.reviews, cause.rule)
    if not _ids(counted) <= _ids(cause.counted):
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "reviews counted outside the gate's observed set require a new ceiling decision")
    preflight = _preflight(snapshot, pr, counted, evidence_id, external_holds)
    by_id = {review.review_id: review for review in cause.counted}
    dismissals = tuple(by_id[review.review_id] for review in counted)
    revision = _next(snapshot, advance_generation=True)
    gates = tuple(RearmingGate(gate, command.request_id) if g == gate else g
                  for g in snapshot.gates if not _is_decision(g))
    # The absorbed decision's retired obligations are covered by this request
    # exactly as a continuation covers its own.
    absorbed = tuple(c for g in snapshot.gates if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision)
                     for c in g.cause.obligations)
    replaces, projection = _replacement(
        snapshot, gates, gate.gate_id, absorbed, review_projection_command_id(snapshot.revision.fence.key,
                                                                              command.request_id),
        external_holds, trigger_label)
    intent = _pending_intent(snapshot, command.request_id, gate.gate_id, dismissals, cause.counted, cause.rule,
                             projection, replaces)
    episode = replace(snapshot.episode, guidance=_guidance(snapshot, command),
                      continuation=FreshReviewRequired(revision.fence.generation))
    after = replace(snapshot, revision=revision, episode=episode, intent=intent, gates=gates)
    commands = _rearm_commands(after, preflight, executor_identity=executor_identity)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.REQUEST_REARM, after,
                           AuthorizedOperatorCommand(actor, command, preflight), commands, prior=snapshot)


# -- stop, continue, complete ----------------------------------------------------

def _retire_rearm(
    snapshot: ReviewWorkflowSnapshot, reason: StopReason, evidence_id: str, *,
    verified_commands: tuple[CommandId, ...], uncertainty: tuple[RemoteOutcomeUncertain, ...],
    cancellation: tuple[CancelRearm, OperatorActor] | None = None,
) -> WorkflowTransition:
    pending = snapshot.intent
    if not isinstance(pending, PendingReviewRearm):
        return _reject(WorkflowRejection.INVALID_SCOPE, "there is no pending review re-arm to retire")
    if not isinstance(verified_commands, tuple) or not isinstance(uncertainty, tuple) or any(
        not isinstance(c, RemoteOutcomeUncertain) for c in uncertainty
    ):
        raise ValueError("retirement evidence must be typed tuples")
    required = set(pending.required_commands)
    if len(set(verified_commands)) != len(verified_commands) or not set(verified_commands) <= required:
        return _reject(WorkflowRejection.INVALID_SCOPE, "verified commands do not belong to this re-arm")
    if any(c.command_id not in required or c.command_id in verified_commands for c in uncertainty):
        return _reject(WorkflowRejection.INVALID_SCOPE, "uncertainty conflicts with required command evidence")
    retired = tuple(c for c in pending.required_commands if c not in verified_commands)
    intent: SupersededReviewRearm | CancelledReviewRearm
    if cancellation is not None:
        command, actor = cancellation
        intent = CancelledReviewRearm(pending.request_id, pending.gate_id, pending.to_generation,
                                      command.cancellation_id, command.reason, verified_commands, retired,
                                      pending.authorized, pending.rule, pending.projection)
        audit: AuthorizedOperatorCommand | ObservedTransition = AuthorizedOperatorCommand(actor, command)
    else:
        intent = SupersededReviewRearm(pending.request_id, pending.gate_id, pending.to_generation, reason,
                                       evidence_id, verified_commands, retired, pending.authorized, pending.rule,
                                       pending.projection)
        audit = ObservedTransition((evidence_id,))
    generation = snapshot.revision.fence.generation
    gates = tuple(g for g in snapshot.gates if _open(g).gate_id != pending.gate_id)
    gates += (OpenGate(
        _identity(snapshot, "rearm-stopped", pending.request_id), generation,
        RearmNeedsDecision(pending.request_id, retired, reason),
        NoAuthorizedRemediation("the stopped review re-arm requires a fresh operator decision"),
    ),)
    existing = {cause.command_id for g in gates if isinstance(cause := _open(g).cause, RemoteOutcomeUncertain)}
    for cause in uncertainty:
        if cause.command_id not in existing:
            gates += (OpenGate(
                _identity(snapshot, "command-uncertain", cause.command_id), generation, cause,
                NoAuthorizedRemediation("only read-only outcome reconciliation remains authorized"),
            ),)
            existing.add(cause.command_id)
    if len(gates) > MAX_WORKFLOW_GATES:
        return _reject(WorkflowRejection.OTHER_HOLD, "the workflow cannot hold the stopped re-arm's gates")
    after = replace(snapshot, revision=_next(snapshot), intent=intent, gates=gates)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.STOP_REARM, after, audit, prior=snapshot)


def cancel_review_rearm(
    snapshot: ReviewWorkflowSnapshot, command: CancelRearm, actor: OperatorActor, *,
    verified_commands: tuple[CommandId, ...] = (), uncertainty: tuple[RemoteOutcomeUncertain, ...] = (),
) -> WorkflowTransition:
    snapshot = _review_snapshot(snapshot)
    if not isinstance(command, CancelRearm):
        raise ValueError("cancellation requires a typed operator command")
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before cancellation")
    if not isinstance(snapshot.intent, PendingReviewRearm) or snapshot.intent.request_id != command.request_id:
        return _reject(WorkflowRejection.INVALID_SCOPE, "cancellation names a different pending review re-arm")
    return _retire_rearm(snapshot, StopReason.OPERATOR_CANCELLED, command.cancellation_id,
                         verified_commands=verified_commands, uncertainty=uncertainty, cancellation=(command, actor))


def supersede_review_rearm(
    snapshot: ReviewWorkflowSnapshot, reason: StopReason, evidence_id: str, *,
    verified_commands: tuple[CommandId, ...] = (), uncertainty: tuple[RemoteOutcomeUncertain, ...] = (),
) -> WorkflowTransition:
    snapshot = _review_snapshot(snapshot)
    if not isinstance(reason, StopReason) or reason is StopReason.OPERATOR_CANCELLED:
        raise ValueError("supersession requires an observed stop reason; cancellation needs an operator")
    return _retire_rearm(snapshot, reason, evidence_id, verified_commands=verified_commands, uncertainty=uncertainty)


def review_rearm_stop_reason(
    accepted: ReviewRearmPreflight, pr: ObservedPullRequest, *, external_holds: tuple[str, ...] = (),
) -> StopReason | None:
    """Why fresh facts supersede an accepted re-arm, if they do.

    Head and state are facts of any present observation, so a partial review
    read still drives supersession. A moved head invalidates the recorded
    dismissal decision (plan decision 7); changed external holds change the
    projection decision.
    """
    if not isinstance(accepted, ReviewRearmPreflight) or not isinstance(pr, ObservedPullRequest):
        raise ValueError("supersession requires the accepted preflight and a present observation")
    if pr.key != accepted.pr_key:
        raise ValueError("supersession observation belongs to another pull request")
    if pr.state is not PullRequestState.OPEN:
        return StopReason.TARGET_TERMINAL
    if pr.head_sha != accepted.head_sha or set(_holds(external_holds)) != set(accepted.external_holds):
        return StopReason.DECISION_CHANGED
    return None


def continue_review_rearm(
    snapshot: ReviewWorkflowSnapshot, command: ContinueGrantedRearm, actor: OperatorActor, pr: PullRequestObservation,
    *, now: datetime, active_reviews: tuple[WorkflowAttemptIdentity, ...], evidence_id: str, trigger_label: Label,
    executor_identity: str, external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Re-plan the stopped intent's remaining authorized scope against fresh facts, under its rule."""
    if not isinstance(snapshot, ReviewWorkflowSnapshot):
        return _reject(WorkflowRejection.INVALID_SCOPE, "continuation names a workflow of another family")
    if not isinstance(command, ContinueGrantedRearm):
        raise ValueError("continuation requires a typed operator command")
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before continuation")
    if isinstance(snapshot.intent, PendingReviewRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a review re-arm already owns this workflow")
    stopped = snapshot.intent
    if not isinstance(stopped, (SupersededReviewRearm, CancelledReviewRearm)) or (
        stopped.request_id != command.stopped_request_id
    ):
        return _reject(WorkflowRejection.INVALID_SCOPE, "continuation requires the current stopped review intent")
    failure = _active(active_reviews) or _observed(snapshot, pr, now, complete=True)
    if failure is not None:
        return failure
    assert isinstance(pr, ObservedPullRequest) and isinstance(pr.reviews, CompleteReviews)
    # The snapshot type keeps exactly this decision gate open beside its stopped intent.
    gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision)
                 and g.cause.stopped_request_id == stopped.request_id), None)
    assert gate is not None
    counted = counted_rejections(pr.reviews, stopped.rule)
    if not _ids(counted) <= _ids(stopped.authorized):
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "reviews counted outside the stopped authorization require an explicit new authorization")
    preflight = _preflight(snapshot, pr, counted, evidence_id, external_holds)
    by_id = {review.review_id: review for review in stopped.authorized}
    dismissals = tuple(by_id[review.review_id] for review in counted)
    revision = _next(snapshot, advance_generation=True)
    assert isinstance(gate.cause, RearmNeedsDecision)
    gates = tuple(RearmingGate(gate, command.request_id) if g == gate else g for g in snapshot.gates)
    replaces, projection = _replacement(
        snapshot, gates, gate.gate_id, gate.cause.obligations,
        review_projection_command_id(snapshot.revision.fence.key, command.request_id), external_holds, trigger_label)
    intent = _pending_intent(snapshot, command.request_id, gate.gate_id, dismissals, stopped.authorized, stopped.rule,
                             projection, replaces)
    after = replace(
        snapshot, revision=revision, intent=intent,
        episode=replace(snapshot.episode, continuation=FreshReviewRequired(revision.fence.generation)), gates=gates,
    )
    commands = _rearm_commands(after, preflight, executor_identity=executor_identity)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.CONTINUE_REARM, after,
                           AuthorizedOperatorCommand(actor, command, preflight), commands, prior=snapshot)


def complete_review_rearm(
    snapshot: ReviewWorkflowSnapshot, pr: PullRequestObservation, *, verified_commands: tuple[CommandId, ...],
    evidence_ids: tuple[str, ...], now: datetime, ceiling: int, trigger_label: Label,
    external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Complete only on verified obligations and fresh complete evidence, under the
    intent's captured rule, that the cause is false."""
    snapshot = _review_snapshot(snapshot)
    pending = snapshot.intent
    if not isinstance(pending, PendingReviewRearm):
        return WorkflowUnchanged(snapshot)
    trigger = _trigger(trigger_label)
    holds = _holds(external_holds)
    failure = _observed(snapshot, pr, now, complete=True)
    if failure is not None:
        return failure
    assert isinstance(pr, ObservedPullRequest) and isinstance(pr.reviews, CompleteReviews)
    if not isinstance(verified_commands, tuple) or len(set(verified_commands)) != len(verified_commands) or (
        set(verified_commands) != set(pending.required_commands)
    ):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "every required remote obligation must be verified")
    counted = counted_rejections(pr.reviews, pending.rule)
    if _ids(counted) & _ids(pending.authorized):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "fresh reviews still count an authorized review")
    if not _ceiling_cleared(counted, ceiling):
        return _reject(WorkflowRejection.OTHER_HOLD, "fresh reviews still reach the rejection ceiling")
    remaining = tuple(g for g in snapshot.gates if _open(g).gate_id != pending.gate_id)
    # Covered uncertainty stays gated after completion but is no independent
    # hold: the projection released the attention label over it.
    if holds or any(not _retired_uncertainty(snapshot, g) for g in remaining):
        if Label.NEEDS_HUMAN_REVIEW not in pr.labels:
            return _reject(WorkflowRejection.OTHER_HOLD, "the remote projection must preserve remaining holds")
    elif Label.NEEDS_HUMAN_REVIEW in pr.labels or trigger not in pr.labels:
        return _reject(WorkflowRejection.OTHER_HOLD, "the remote projection has not released the review trigger")
    intent = CompletedReviewRearm(pending.request_id, pending.gate_id, pending.to_generation, verified_commands)
    after = replace(snapshot, revision=_next(snapshot), intent=intent, gates=remaining)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.RECONCILE_REARM, after,
                           ObservedTransition(evidence_ids), prior=snapshot)


# -- validation seam -------------------------------------------------------------

@dataclass(frozen=True)
class ReviewTransitionFacts:
    """Harness-owned facts the committing adapter supplies to re-derive a proposal.

    - `observation`: the durable PR observation behind a gate, re-arm,
      supersession or completion; freshness is judged against `now`.
    - `rule`: the configured counting rule, captured only by a NEW gate.
    - `escalation`: the ceiling escalation receipt a NEW gate requires.
    - `submission`: the SUBMIT_REVIEW command/receipt facts a recorded review requires.
    - `accepted_preflight`: the stopped intent's accepted preflight for
      decision-changed supersession.
    """
    now: datetime
    ceiling: int
    trigger_label: Label
    executor_identity: str
    rule: CountingRule | None = None
    observation: PullRequestObservation | None = None
    accepted_preflight: ReviewRearmPreflight | None = None
    external_holds: tuple[str, ...] = ()
    escalation: CeilingEscalation | None = None
    submission: ReviewSubmissionReceipt | None = None


def _by_identity(commands: tuple[LifecycleCommand, ...]) -> list[LifecycleCommand]:
    return sorted(commands, key=lambda command: str(command.command_id))


def _normalized(snapshot: ReviewWorkflowSnapshot) -> ReviewWorkflowSnapshot:
    return replace(snapshot, gates=tuple(sorted(snapshot.gates, key=lambda g: _open(g).gate_id)))


def _supersession_evidence(
    before: ReviewWorkflowSnapshot, intent: SupersededReviewRearm, facts: ReviewTransitionFacts,
) -> None:
    if intent.reason is StopReason.REQUIRED_EFFECT_REJECTED:
        return  # The adapter verifies the refusal receipt named by the evidence.
    pr = facts.observation
    if not isinstance(pr, ObservedPullRequest):
        raise ValueError("absent or unavailable pull requests cannot justify supersession")
    if pr.key != before.revision.fence.key or not 0 <= (facts.now - pr.observed_at).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS:
        raise ValueError("supersession observation is foreign or stale")
    if intent.reason is StopReason.TARGET_TERMINAL:
        if pr.state is PullRequestState.OPEN:
            raise ValueError("terminal supersession requires an observed terminal pull request")
        return
    accepted = facts.accepted_preflight
    if not isinstance(accepted, ReviewRearmPreflight) or accepted.pr_key != before.revision.fence.key:
        raise ValueError("decision supersession requires the accepted preflight")
    if pr.head_sha == accepted.head_sha and set(_holds(facts.external_holds)) == set(accepted.external_holds):
        raise ValueError("decision supersession requires a moved head or changed holds")


def _observation(facts: ReviewTransitionFacts) -> PullRequestObservation:
    if facts.observation is None:
        raise ValueError("this review transition requires its durable pull request observation")
    return facts.observation


def _reconciled(
    before: ReviewWorkflowSnapshot, facts: ReviewTransitionFacts, audit: ObservedTransition,
) -> WorkflowTransition:
    return reconcile_review_ceiling(
        before, _observation(facts), now=facts.now, ceiling=facts.ceiling, evidence_ids=audit.evidence_ids,
        trigger_label=facts.trigger_label, executor_identity=facts.executor_identity,
        external_holds=facts.external_holds,
    )


def validate_review_transition(
    before: ReviewWorkflowSnapshot, transition: WorkflowChanged, facts: ReviewTransitionFacts,
) -> None:
    """Re-derive the full expected transition from harness-owned facts.

    A well-formed envelope is not permission: the proposal's snapshot, audit
    (including the operator preflight, re-derived from the observation),
    commands and prior must equal what this policy produces from `before`.
    """
    if not isinstance(before, ReviewWorkflowSnapshot) or not isinstance(transition, WorkflowChanged):
        raise ValueError("review validation requires a typed prior snapshot and proposal")
    if not isinstance(facts, ReviewTransitionFacts):
        raise ValueError("review validation requires harness-owned transition facts")
    if transition.prior != before or transition.expected != before.revision:
        raise ValueError("review proposal does not replace the stored snapshot")
    after, audit, mutation = transition.snapshot, transition.audit, transition.mutation
    if not isinstance(after, ReviewWorkflowSnapshot) or after.authority != before.authority:
        raise ValueError("review proposal changed its family authority")
    expected: WorkflowTransition
    if mutation is WorkflowMutation.OPEN_GATE and isinstance(audit, ObservedTransition):
        if facts.escalation is not None:
            if facts.rule is None:
                raise ValueError("a new review ceiling requires the configured counting rule")
            expected = open_review_ceiling(before, _observation(facts), now=facts.now, rule=facts.rule,
                                           ceiling=facts.ceiling, escalation=facts.escalation,
                                           evidence_ids=audit.evidence_ids)
        else:
            expected = _reconciled(before, facts, audit)
    elif mutation is WorkflowMutation.RELEASE_CEILING and isinstance(audit, ObservedTransition):
        expected = _reconciled(before, facts, audit)
    elif mutation is WorkflowMutation.RECORD_REVIEW and isinstance(audit, ObservedTransition):
        if facts.submission is None:
            raise ValueError("a recorded review requires its harness-owned submission receipt")
        expected = record_review(before, facts.submission)
    elif mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM) and isinstance(
        audit, AuthorizedOperatorCommand
    ) and isinstance(audit.preflight, ReviewRearmPreflight):
        evidence_id = audit.preflight.complete_reviews_evidence_id
        if mutation is WorkflowMutation.REQUEST_REARM and isinstance(audit.command, RequestRearm):
            expected = request_review_rearm(
                before, audit.command, audit.actor, _observation(facts), now=facts.now, active_reviews=(),
                evidence_id=evidence_id, trigger_label=facts.trigger_label,
                executor_identity=facts.executor_identity, external_holds=facts.external_holds,
            )
        elif mutation is WorkflowMutation.CONTINUE_REARM and isinstance(audit.command, ContinueGrantedRearm):
            expected = continue_review_rearm(
                before, audit.command, audit.actor, _observation(facts), now=facts.now, active_reviews=(),
                evidence_id=evidence_id, trigger_label=facts.trigger_label,
                executor_identity=facts.executor_identity, external_holds=facts.external_holds,
            )
        else:
            raise ValueError("review re-arm does not carry its operator command")
    elif mutation is WorkflowMutation.STOP_REARM:
        intent = after.intent
        existing = {_open(g).gate_id for g in before.gates}
        uncertainty = tuple(cause for g in after.gates if _open(g).gate_id not in existing
                            and isinstance(cause := _open(g).cause, RemoteOutcomeUncertain))
        if isinstance(intent, CancelledReviewRearm) and isinstance(audit, AuthorizedOperatorCommand) and isinstance(
            audit.command, CancelRearm
        ):
            expected = cancel_review_rearm(before, audit.command, audit.actor,
                                           verified_commands=intent.verified_commands, uncertainty=uncertainty)
        elif isinstance(intent, SupersededReviewRearm) and isinstance(audit, ObservedTransition):
            _supersession_evidence(before, intent, facts)
            expected = supersede_review_rearm(before, intent.reason, intent.evidence_id,
                                              verified_commands=intent.verified_commands, uncertainty=uncertainty)
        else:
            raise ValueError("review stop does not carry its authorized disposition")
    elif mutation is WorkflowMutation.RECONCILE_REARM and isinstance(audit, ObservedTransition):
        completed = after.intent
        if not isinstance(completed, CompletedReviewRearm):
            raise ValueError("review completion requires its completed intent")
        expected = complete_review_rearm(
            before, _observation(facts), verified_commands=completed.verified_commands,
            evidence_ids=audit.evidence_ids, now=facts.now, ceiling=facts.ceiling,
            trigger_label=facts.trigger_label, external_holds=facts.external_holds,
        )
    else:
        raise ValueError("workflow mutation is not supported by review policy")
    if not isinstance(expected, WorkflowChanged):
        raise ValueError("review proposal does not represent an authorized state transition")
    assert isinstance(expected.snapshot, ReviewWorkflowSnapshot)
    if expected.mutation is not mutation or _normalized(expected.snapshot) != _normalized(after):
        raise ValueError("review proposal disagrees with the pure transition policy")
    if expected.audit != audit:
        raise ValueError("review proposal audit disagrees with the harness-owned facts")
    if _by_identity(expected.commands) != _by_identity(transition.commands):
        raise ValueError("review proposal commands disagree with the accepted obligations")
