"""Closed PR-scoped review-rejection operator contracts (ADR 0015 review slice).

Distinct schema names from the resolve contracts, which stay byte-stable.
Wire data cannot mint workflow evidence, actors, dismissal authority or an
executor: a command names only the operator's decision and the authority
revision it was made against.
"""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from ...domain import workflow as w
from ...domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
    canonical_workflow_repository,
)
from .resolve_contracts import ClearGuidance, ClosedContract, Counter, Guidance, Identifier, Positive, SetGuidance


class ReviewExpected(ClosedContract):
    repository: Identifier
    pull_request: Positive
    family: Literal["review_rejections"]
    epoch: Positive
    generation: Counter
    version: Counter
    episode_id: Identifier

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        if canonical_workflow_repository(value) != value:
            raise ValueError("repository must be canonical")
        return value

    def binding(self) -> WorkflowBinding:
        return WorkflowBinding(
            WorkflowFence(WorkflowKey(self.repository, PullRequestRef(self.pull_request)), self.generation),
            FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, self.epoch), self.episode_id,
        )

    def revision(self) -> WorkflowRevision:
        return WorkflowRevision(self.binding().fence, self.version)


class ReviewRequestRearm(ClosedContract):
    """Authorize dismissal of exactly the gate's observed counted bot reviews; the set is never on the wire."""
    operation: Literal["request_rearm"]
    expected: ReviewExpected
    request_id: Identifier
    gate_id: Identifier
    guidance: Guidance

    def command(self) -> w.RequestRearm:
        guidance = (w.SetGuidance(self.guidance.text) if isinstance(self.guidance, SetGuidance)
                    else w.ClearGuidance() if isinstance(self.guidance, ClearGuidance) else w.KeepGuidance())
        return w.RequestRearm(self.request_id, self.expected.revision(), self.gate_id, guidance,
                              expected_binding=self.expected.binding())


class ReviewContinueRearm(ClosedContract):
    operation: Literal["continue_rearm"]
    expected: ReviewExpected
    request_id: Identifier
    stopped_request_id: Identifier

    def command(self) -> w.ContinueGrantedRearm:
        return w.ContinueGrantedRearm(self.request_id, self.expected.revision(), self.stopped_request_id,
                                      expected_binding=self.expected.binding())


class ReviewCancelRearm(ClosedContract):
    operation: Literal["cancel_rearm"]
    expected: ReviewExpected
    cancellation_id: Identifier
    request_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=16384)]

    def command(self) -> w.CancelRearm:
        return w.CancelRearm(self.cancellation_id, self.expected.revision(), self.request_id, self.reason,
                             expected_binding=self.expected.binding())


class ReviewAuthorizeRetry(ClosedContract):
    """One execution of an existing command; the evidence is the harness's own read."""
    operation: Literal["authorize_retry"]
    expected: ReviewExpected
    authorization_id: Identifier
    gate_id: Identifier
    command_id: Identifier
    evidence_id: Identifier

    def command(self) -> w.AuthorizeAbsentRetry:
        return w.AuthorizeAbsentRetry(
            self.authorization_id, self.expected.revision(), w.CommandId(self.command_id),
            self.gate_id, self.evidence_id, expected_binding=self.expected.binding(),
        )


ReviewCommandBody = ReviewRequestRearm | ReviewContinueRearm | ReviewCancelRearm | ReviewAuthorizeRetry
ReviewCommand = Annotated[ReviewCommandBody, Field(discriminator="operation")]


def requested_rearm(body: ReviewCommandBody) -> str | None:
    """The re-arm history a command result should display, if it names one."""
    return None if isinstance(body, ReviewAuthorizeRetry) else body.request_id


# -- view ------------------------------------------------------------------------------------------

class ReviewCountedReview(ClosedContract):
    review_id: str
    author: str
    commit_id: str | None


class ReviewHoldUncontested(ClosedContract):
    """The harness restores a removed attention hold, a bounded number of times."""
    state: Literal["restoring"]
    restores: int
    restore_limit: int


class ReviewHoldContested(ClosedContract):
    """Restorations are spent: an actor keeps removing the hold; the gate alone still holds every review."""
    state: Literal["contested"]
    restores: int
    restore_limit: int


class ReviewHoldUnreadable(ClosedContract):
    """The harness's restoration record for this ceiling is malformed; the gate and its lever are unaffected."""
    state: Literal["unreadable"]
    reason: str


ReviewHoldState = Annotated[
    ReviewHoldUncontested | ReviewHoldContested | ReviewHoldUnreadable, Field(discriminator="state"),
]


class ReviewRejectionCeilingCause(ClosedContract):
    kind: Literal["review_rejection_ceiling"]
    episode_id: str
    head_sha: str
    review_bot: str
    ceiling: int
    counted_total: int
    counted: list[ReviewCountedReview]
    authorization_bound: int
    non_resetting_commits: list[str]
    hold: ReviewHoldState


class ReviewRemoteUncertainCause(ClosedContract):
    kind: Literal["remote_uncertain"]
    command_id: str
    unresolved_since: str
    failed_reads: int
    last_error: str


class ReviewRetryAuthorizationRequiredCause(ClosedContract):
    kind: Literal["retry_authorization_required"]
    command_id: str
    absence_evidence_id: str
    retry_authorizable: bool


class ReviewEvidenceUnavailableCause(ClosedContract):
    kind: Literal["evidence_unavailable"]
    evidence: Literal["exemption_trailers", "review_history", "prewrite_read", "rearm_observation", "other"]
    source: str
    unresolved_since: str
    failed_reads: int
    last_error: str


class ReviewRearmNeedsDecisionCause(ClosedContract):
    kind: Literal["rearm_needs_decision"]
    stopped_request_id: str
    obligations: list[str]
    reason: str


class ReviewProjectionRefusedCause(ClosedContract):
    kind: Literal["review_projection_refused"]
    projection: Literal["ceiling_release", "escalation_hold"]
    command_id: str
    generation: int
    reason: str


ReviewCauseKind = Literal[
    "review_rejection_ceiling", "remote_uncertain", "retry_authorization_required", "evidence_unavailable",
    "rearm_needs_decision", "review_projection_refused",
]


class ReviewGateFactsUnavailableCause(ClosedContract):
    """The store's own facts about this gate disagree (its retry grants or obligation receipts).

    The gate still holds and reconciliation is unaffected; nothing is offered
    on it until the facts agree again.
    """
    kind: Literal["facts_unavailable"]
    underlying: ReviewCauseKind
    reason: str


ReviewGateCause = Annotated[
    ReviewRejectionCeilingCause | ReviewRemoteUncertainCause | ReviewRetryAuthorizationRequiredCause
    | ReviewEvidenceUnavailableCause | ReviewRearmNeedsDecisionCause | ReviewProjectionRefusedCause
    | ReviewGateFactsUnavailableCause,
    Field(discriminator="kind"),
]


class ReviewNoRemediation(ClosedContract):
    kind: Literal["none_authorized"]
    reason: str


class ReviewAttemptedRemediation(ClosedContract):
    kind: Literal["attempted"]
    operation: str
    evidence_ids: list[str]
    outcome: str


ReviewRemediation = Annotated[ReviewNoRemediation | ReviewAttemptedRemediation, Field(discriminator="kind")]

ReviewOperation = Literal["request_rearm", "continue_rearm", "cancel_rearm", "authorize_retry"]


class ReviewActionOffered(ClosedContract):
    state: Literal["offered"]
    operation: ReviewOperation
    effect: str


class ReviewActionRefused(ClosedContract):
    state: Literal["refused"]
    operation: ReviewOperation
    reason: str


ReviewAction = Annotated[ReviewActionOffered | ReviewActionRefused, Field(discriminator="state")]


class ReviewPendingRetry(ClosedContract):
    """One authorized retry accepted and awaiting the executor's decision."""
    authorization_id: str
    command_id: str


class ReviewGate(ClosedContract):
    gate_id: str
    state: Literal["open", "rearming"]
    opened_generation: int
    cause: ReviewGateCause
    remediation: ReviewRemediation
    actions: list[ReviewAction]
    pending_retry: ReviewPendingRetry | None


class ReviewDismissalObligation(ClosedContract):
    kind: Literal["dismiss_review"]
    review_id: str


class ReviewProjectionObligation(ClosedContract):
    kind: Literal["projection"]
    remove: str | None
    add: str


class ReviewUnlabelledObligation(ClosedContract):
    kind: Literal["unlabelled"]


ReviewObligation = Annotated[
    ReviewDismissalObligation | ReviewProjectionObligation | ReviewUnlabelledObligation, Field(discriminator="kind"),
]


class ReviewRearmCommand(ClosedContract):
    command_id: str
    obligation: ReviewObligation
    status: Literal["pending", "verified", "refused", "uncertain", "held", "unavailable"]


class ReviewPendingRearm(ClosedContract):
    state: Literal["pending"]
    request_id: str
    gate_id: str
    generation: int
    authorized_reviews: list[str]
    commands: list[ReviewRearmCommand]


class ReviewCompletedRearm(ClosedContract):
    state: Literal["completed"]
    request_id: str
    gate_id: str
    generation: int
    verified_commands: list[str]


class ReviewStoppedRearm(ClosedContract):
    """A cancelled or superseded request; retired obligations never execute."""
    state: Literal["cancelled", "superseded"]
    request_id: str
    gate_id: str
    generation: int
    reason: str
    authorized_reviews: list[str]
    verified_commands: list[str]
    retired_commands: list[str]


ReviewRearm = Annotated[ReviewPendingRearm | ReviewCompletedRearm | ReviewStoppedRearm, Field(discriminator="state")]


class ReviewFreshReviewRequired(ClosedContract):
    kind: Literal["fresh_review_required"]
    owed_since_generation: int


class ReviewBoundSubmission(ClosedContract):
    kind: Literal["bound_submission"]
    command_id: str
    generation: int


class ReviewImportedBaseline(ClosedContract):
    kind: Literal["imported"]
    evidence_id: str


ReviewBaselineSource = Annotated[ReviewBoundSubmission | ReviewImportedBaseline, Field(discriminator="kind")]


class ReviewReviewedBaseline(ClosedContract):
    kind: Literal["reviewed_baseline"]
    head_sha: str
    patch_id: str
    reviewed_generation: int
    source: ReviewBaselineSource


ReviewContinuation = Annotated[ReviewFreshReviewRequired | ReviewReviewedBaseline, Field(discriminator="kind")]


class ReviewDecisionBasis(ClosedContract):
    """The pull request facts the review daemon decided from, all from its own read."""
    head_sha: str
    state: Literal["open", "closed", "merged"]
    labels: list[str]
    external_holds: list[str] = Field(description="Holds a re-arm keeps: trusted hold labels and the unproductive-round breaker.")


class ReviewDispatchTerminal(ClosedContract):
    kind: Literal["terminal"]
    state: Literal["closed", "merged"]


class ReviewDispatchNotAdmitted(ClosedContract):
    """Holds outside the review workflow keep review dispatch off this pull request; no re-arm removes them."""
    kind: Literal["not_admitted"]
    holds: list[str]


class ReviewDispatchDeferred(ClosedContract):
    """The daemon could not decide (an unreadable fact) or waits for a bounded read schedule."""
    kind: Literal["deferred"]
    reason: str


class ReviewDispatchHeld(ClosedContract):
    kind: Literal["held"]
    gate_ids: list[str]


class ReviewDispatchSubmissionUnresolved(ClosedContract):
    kind: Literal["submission_unresolved"]
    command_ids: list[str]


class ReviewDispatchReleasePending(ClosedContract):
    kind: Literal["release_projection_pending"]
    command_id: str


class ReviewDispatchOwed(ClosedContract):
    kind: Literal["owed"]
    review: Literal["initial", "re_review"]


class ReviewDispatchNotOwed(ClosedContract):
    kind: Literal["not_owed"]
    reason: str


class ReviewDispatchWithheld(ClosedContract):
    """Owed, but this tick's dispatch admission withheld it: a fix run's claim, or another reservation."""
    kind: Literal["withheld"]
    review: Literal["initial", "re_review"]
    reason: Literal["fix_claim", "reserved"]
    detail: str


class ReviewDispatchInFlight(ClosedContract):
    """Owed, and already running: this pull request's own review dispatch still holds its reservation."""
    kind: Literal["in_flight"]
    review: Literal["initial", "re_review"]


ReviewDispatch = Annotated[
    ReviewDispatchTerminal | ReviewDispatchNotAdmitted | ReviewDispatchDeferred | ReviewDispatchHeld
    | ReviewDispatchSubmissionUnresolved | ReviewDispatchReleasePending | ReviewDispatchOwed | ReviewDispatchWithheld
    | ReviewDispatchInFlight | ReviewDispatchNotOwed,
    Field(discriminator="kind"),
]


class ReviewReadFailure(ClosedContract):
    """Consecutive failed departed reads; the decision and facts beside it are the last good read's, if any."""
    failed_reads: int
    reason: str
    first_failed_at: str
    last_failed_at: str


class ReviewDaemonRecorded(ClosedContract):
    """The review daemon's own latest decision: made at `decided_at`, last confirmed unchanged at `as_of`."""
    state: Literal["recorded"]
    dispatch: ReviewDispatch
    pull_request: ReviewDecisionBasis | None
    # `review_read`: the daemon's trigger-labelled review read; `departed_read`:
    # the pull request's own read after it left that read.
    read: Literal["review_read", "departed_read"]
    decided_at: str
    as_of: str
    read_failure: ReviewReadFailure | None = Field(
        description="The pull request's own read keeps failing; null once a read succeeds.")


class ReviewDaemonUnrecorded(ClosedContract):
    """The review daemon has not recorded a decision for this pull request yet."""
    state: Literal["unrecorded"]


class ReviewDaemonUnreadable(ClosedContract):
    state: Literal["unreadable"]
    reason: str


ReviewDaemonDecision = Annotated[
    ReviewDaemonRecorded | ReviewDaemonUnrecorded | ReviewDaemonUnreadable, Field(discriminator="state"),
]


class ReviewObservation(ClosedContract):
    unresolved_since: str
    next_read_at: str
    failed_reads: int
    last_error: str


class ReviewAvailableControl(ClosedContract):
    status: Literal["available"]
    expected: ReviewExpected
    workflow_held: bool = Field(description="SQLite workflow holds only, not a remote readiness assertion; acceptance re-reads GitHub.")
    gates: list[ReviewGate]
    guidance: str
    continuation: ReviewContinuation
    daemon: ReviewDaemonDecision = Field(description="The review daemon's recorded dispatch decision, never derived by this API.")
    current_intent: ReviewRearm | None
    requested_id: str | None
    requested_intent: ReviewRearm | None
    observation: ReviewObservation | None


class ReviewMigrationRequiredControl(ClosedContract):
    """Review dispatch and operator re-arm require explicit family activation."""
    status: Literal["migration_required"]
    repository: str
    pull_request: int
    active_families: list[str]


class ReviewAbsentControl(ClosedContract):
    status: Literal["absent"]
    repository: str
    pull_request: int
    epoch: int


class ReviewUnavailableControl(ClosedContract):
    status: Literal["unavailable"]
    reason: str


ReviewControl = Annotated[
    ReviewAvailableControl | ReviewMigrationRequiredControl | ReviewAbsentControl
    | ReviewUnavailableControl,
    Field(discriminator="status"),
]


class WorkflowFamiliesAvailable(ClosedContract):
    """The repository's activated workflow families; the legacy unblock is retired while any is active."""
    status: Literal["available"]
    repository: str
    active_families: list[Literal["resolve", "review_rejections"]]


class WorkflowFamiliesUnavailable(ClosedContract):
    status: Literal["unavailable"]
    reason: str


WorkflowFamilies = Annotated[
    WorkflowFamiliesAvailable | WorkflowFamiliesUnavailable, Field(discriminator="status"),
]


class ReviewCommandResult(ClosedContract):
    outcome: Literal["accepted", "replayed", "conflict", "rejected", "unavailable"]
    reason: str
    control: ReviewControl
