"""Ordered review execution evidence; phase-4 continuation construction.

Adapters must verify the named admission, launch and effect receipts before
constructing these facts. Model output cannot mint them. This module supplies
no runtime authority until the execution family is explicitly activated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import hashlib

from .agent_task import RuntimeCapabilities, RuntimeName
from .workflow import (
    AuthorizedOperatorCommand, ContinueGrantedRearm, ObservedTransition,
    RequestRearm, WorkflowMutation,
)
from .workflow_identity import (
    PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowRevision,
    canonical_workflow_repository,
    validate_workflow_identifier as identifier,
    validate_workflow_integer as integer,
)

MAX_EXECUTION_SUFFIX = 4096
MAX_ROUND_CHILDREN = 256
_MAX_INTEGER = 2**63 - 1
_LEGACY_SESSION_MARKERS = frozenset({"multi", "github-review", "codex-review"})


def is_review_session_mode_marker(value: object) -> bool:
    return isinstance(value, str) and value in _LEGACY_SESSION_MARKERS


def validate_review_session_handle(handle: str) -> None:
    identifier(handle, "review session handle")
    try:
        handle.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("review session handle must be valid UTF-8 text") from exc
    if is_review_session_mode_marker(handle):
        raise ValueError("legacy mode markers are not reusable session handles")


@dataclass(frozen=True)
class ReviewExecutionBinding:
    rejection: WorkflowBinding
    execution_epoch: int
    execution_episode: str

    def __post_init__(self) -> None:
        if not isinstance(self.rejection, WorkflowBinding) or (
            self.rejection.authority.family is not WorkflowFamily.REVIEW_REJECTIONS
            or not isinstance(self.rejection.fence.key.target, PullRequestRef)
        ):
            raise ValueError("review execution requires its parent rejection binding")
        integer(self.execution_epoch, "execution epoch", minimum=1)
        identifier(self.execution_episode, "execution episode")


@dataclass(frozen=True)
class ReviewRoundIdentity:
    binding: ReviewExecutionBinding
    round_id: str
    sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewExecutionBinding):
            raise ValueError("review round requires an execution binding")
        identifier(self.round_id, "review round")
        integer(self.sequence, "review round sequence", minimum=1)


class ReviewChildKind(str, Enum):
    REVIEWER = "reviewer"
    GATE = "gate"
    DIMENSION = "dimension"
    VALIDATOR = "validator"


@dataclass(frozen=True)
class ReviewChildIdentity:
    round: ReviewRoundIdentity
    stage_id: str
    child_id: str
    kind: ReviewChildKind

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity) or not isinstance(self.kind, ReviewChildKind):
            raise ValueError("review child requires a round and typed role")
        identifier(self.stage_id, "review stage")
        identifier(self.child_id, "review child")


def review_child_budget_leg(child: ReviewChildIdentity, parent_leg: str) -> str:
    """Keep parent admission identity while selecting the configured child budget."""
    if not isinstance(child, ReviewChildIdentity) or parent_leg not in {"REVIEW", "RE_REVIEW"}:
        raise ValueError("review child budget requires its bound parent review leg")
    if child.kind is ReviewChildKind.GATE:
        return "REVIEW_GATE"
    if child.kind is ReviewChildKind.DIMENSION:
        return "REVIEW_" + child.child_id.upper().replace("-", "_")
    if child.kind is ReviewChildKind.VALIDATOR:
        return "VALIDATE_" + child.child_id.partition(":")[0].upper().replace("-", "_")
    return parent_leg


@dataclass(frozen=True)
class ReviewChildAdmission:
    """One budget reservation attached to a planned child; no launch is implied."""

    child: ReviewChildIdentity
    run_id: str
    reservation_id: str
    claim_occurrence_id: str
    admitted_revision: WorkflowRevision
    owner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.child, ReviewChildIdentity):
            raise ValueError("review admission requires one planned child")
        for name in ("run_id", "reservation_id", "claim_occurrence_id", "owner_id"):
            identifier(getattr(self, name), f"review admission {name}")
        if (not isinstance(self.admitted_revision, WorkflowRevision)
                or self.admitted_revision.fence != self.child.round.binding.rejection.fence):
            raise ValueError("review admission revision disagrees with its child binding")


@dataclass(frozen=True)
class ReviewUnlaunchedRelease:
    """Immutable release proof minted with the unlaunched accounting transition."""

    admission: ReviewChildAdmission
    receipt_id: str
    reason_digest: str
    released_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ReviewChildAdmission):
            raise ValueError("review release requires its exact admission")
        if self.receipt_id != f"review-execution-release:{self.admission.run_id}":
            raise ValueError("review release receipt disagrees with its run")
        if (not isinstance(self.reason_digest, str) or len(self.reason_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.reason_digest)):
            raise ValueError("review release requires a SHA-256 reason digest")
        if (not isinstance(self.released_at, datetime) or self.released_at.tzinfo is None
                or self.released_at.utcoffset() is None):
            raise ValueError("review release requires an aware time")


@dataclass(frozen=True)
class ReviewChildAdmissionRefusal:
    """A final, transactionally proved cap refusal for one planned child."""

    child: ReviewChildIdentity
    receipt_id: str
    run_id: str
    reservation_id: str
    claim_occurrence_id: str
    refused_revision: WorkflowRevision
    owner_id: str
    request_fingerprint: str
    limit_repository: str
    limit_dimension: str
    limit_period_id: str
    limit_period_start: datetime
    limit_period_end: datetime
    limit_period_timezone: str
    currency: str
    committed_micros: int
    requested_micros: int
    cap_micros: int
    refused_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.child, ReviewChildIdentity):
            raise ValueError("review refusal requires one planned child")
        if (not isinstance(self.refused_revision, WorkflowRevision)
                or self.refused_revision.fence != self.child.round.binding.rejection.fence):
            raise ValueError("review refusal revision disagrees with its child binding")
        for name in ("receipt_id", "run_id", "reservation_id", "claim_occurrence_id", "owner_id",
                     "request_fingerprint", "limit_dimension", "limit_period_id", "currency"):
            identifier(getattr(self, name), f"review refusal {name}")
        if not isinstance(self.limit_repository, str) or not isinstance(self.limit_period_timezone, str):
            raise ValueError("review refusal limit scope is malformed")
        if not self.limit_period_timezone:
            raise ValueError("review refusal limit timezone is required")
        for value in (self.limit_period_start, self.limit_period_end, self.refused_at):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("review refusal timestamps require offsets")
        if self.limit_period_start >= self.limit_period_end:
            raise ValueError("review refusal limit period is empty")
        for name in ("committed_micros", "requested_micros", "cap_micros"):
            integer(getattr(self, name), f"review refusal {name}", minimum=0)
        if self.committed_micros + self.requested_micros <= self.cap_micros:
            raise ValueError("review refusal requires proved cap exhaustion")


@dataclass(frozen=True)
class ReviewChildLaunchRegistration:
    """Durable guarded process registration, before provider exec or billing."""

    admission: ReviewChildAdmission
    receipt_id: str
    dispatch_id: str
    pid: int
    pgid: int
    process_start_ticks: int
    registered_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ReviewChildAdmission):
            raise ValueError("review launch requires its exact child admission")
        if self.receipt_id != f"review-execution-launch:{self.admission.run_id}":
            raise ValueError("review launch receipt disagrees with its run")
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id:
            raise ValueError("review launch requires its paid dispatch identity")
        for name in ("pid", "pgid", "process_start_ticks"):
            integer(getattr(self, name), f"review launch {name}", minimum=1)
        if (not isinstance(self.registered_at, datetime) or self.registered_at.tzinfo is None
                or self.registered_at.utcoffset() is None):
            raise ValueError("review launch requires an aware registration time")


@dataclass(frozen=True)
class LaunchedReviewChild:
    child: ReviewChildIdentity
    run_id: str
    launch_receipt: str

    def __post_init__(self) -> None:
        if not isinstance(self.child, ReviewChildIdentity):
            raise ValueError("launched review requires its planned child")
        identifier(self.run_id, "review run")
        identifier(self.launch_receipt, "review launch receipt")


@dataclass(frozen=True)
class RenewedReviewSessionSource:
    evidence_id: str

    def __post_init__(self) -> None:
        identifier(self.evidence_id, "renewed session origin")


@dataclass(frozen=True)
class NoReviewSession:
    pass


@dataclass(frozen=True)
class ReusableReviewSession:
    runtime: RuntimeName
    handle: str
    source: LaunchedReviewChild | RenewedReviewSessionSource

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("review session requires its runtime")
        validate_review_session_handle(self.handle)
        if not isinstance(self.source, (LaunchedReviewChild, RenewedReviewSessionSource)):
            raise ValueError("review session requires verified source provenance")
        if isinstance(self.source, LaunchedReviewChild) and self.source.child.kind is not ReviewChildKind.REVIEWER:
            raise ValueError("only a reviewer invocation can supply the reusable session")


ReviewSession = NoReviewSession | ReusableReviewSession


def compatible_review_session(
    session: ReviewSession, runtime: RuntimeName, capabilities: RuntimeCapabilities,
) -> ReviewSession:
    if not isinstance(session, (NoReviewSession, ReusableReviewSession)) or not isinstance(runtime, RuntimeName):
        raise ValueError("session selection requires typed continuation and runtime")
    if not isinstance(capabilities, RuntimeCapabilities):
        raise ValueError("session selection requires runtime capabilities")
    if isinstance(session, ReusableReviewSession) and (
        session.runtime is runtime and capabilities.supports_resume and capabilities.resume_preserves_sandbox
    ):
        return session
    return NoReviewSession()


@dataclass(frozen=True)
class PreserveReviewSession:
    pass


@dataclass(frozen=True)
class ReplaceReviewSession:
    session: ReusableReviewSession

    def __post_init__(self) -> None:
        if not isinstance(self.session, ReusableReviewSession) or not isinstance(self.session.source, LaunchedReviewChild):
            raise ValueError("a runtime session update requires a launched reviewer")


@dataclass(frozen=True)
class ClearReviewSession:
    producer: LaunchedReviewChild

    def __post_init__(self) -> None:
        if not isinstance(self.producer, LaunchedReviewChild) or self.producer.child.kind is not ReviewChildKind.REVIEWER:
            raise ValueError("a sessionless update requires an eligible reviewer producer")


ReviewSessionUpdate = PreserveReviewSession | ReplaceReviewSession | ClearReviewSession


@dataclass(frozen=True)
class PreserveDisplayedRound:
    pass


@dataclass(frozen=True)
class SetDisplayedRound:
    number: int

    def __post_init__(self) -> None:
        integer(self.number, "displayed review round", minimum=1)


def _launched(values: tuple[LaunchedReviewChild, ...]) -> None:
    if not isinstance(values, tuple) or not 1 <= len(values) <= MAX_ROUND_CHILDREN:
        raise ValueError("a paid round requires bounded launched-child evidence")
    if any(not isinstance(value, LaunchedReviewChild) for value in values):
        raise ValueError("a paid round requires typed launch evidence")
    if len({value.run_id for value in values}) != len(values) or len({value.child.child_id for value in values}) != len(values):
        raise ValueError("a round cannot contain duplicate runs or children")
    if len({value.child.round for value in values}) != 1:
        raise ValueError("all launched children must belong to the same outer round")


@dataclass(frozen=True)
class PaidReviewProgress:
    submission_receipt: str
    launched: tuple[LaunchedReviewChild, ...]

    def __post_init__(self) -> None:
        identifier(self.submission_receipt, "productive submission receipt")
        _launched(self.launched)


@dataclass(frozen=True)
class CopiedReviewProgress:
    submission_receipt: str

    def __post_init__(self) -> None:
        identifier(self.submission_receipt, "copied submission receipt")


@dataclass(frozen=True)
class AddReviewStrike:
    pass


@dataclass(frozen=True)
class SaturateReviewFailures:
    ceiling: int
    classification_receipt: str

    def __post_init__(self) -> None:
        integer(self.ceiling, "deterministic death ceiling", minimum=1)
        identifier(self.classification_receipt, "deterministic death classification")


@dataclass(frozen=True)
class ReviewRoundFailure:
    launched: tuple[LaunchedReviewChild, ...]
    outcome_receipt: str
    debit: AddReviewStrike | SaturateReviewFailures = AddReviewStrike()

    def __post_init__(self) -> None:
        _launched(self.launched)
        identifier(self.outcome_receipt, "failed outer round receipt")
        if not isinstance(self.debit, (AddReviewStrike, SaturateReviewFailures)):
            raise ValueError("failed round requires a typed allowance debit")


@dataclass(frozen=True)
class ReviewInfrastructureDeath:
    """A launched round ended from verified non-striking infrastructure loss."""

    launched: tuple[LaunchedReviewChild, ...]
    outcome_receipt: str

    def __post_init__(self) -> None:
        _launched(self.launched)
        identifier(self.outcome_receipt, "infrastructure death closure receipt")


@dataclass(frozen=True)
class ReviewBudgetExhausted:
    """A settled launched child stopped at the provider's budget boundary."""

    launched: tuple[LaunchedReviewChild, ...]
    outcome_receipt: str

    def __post_init__(self) -> None:
        _launched(self.launched)
        identifier(self.outcome_receipt, "budget-exhausted closure receipt")


@dataclass(frozen=True)
class ReviewPinDeferred:
    preflight_receipt: str

    def __post_init__(self) -> None:
        identifier(self.preflight_receipt, "pin deferral receipt")


@dataclass(frozen=True)
class ReviewAdmissionRefused:
    refusal_receipt: str

    def __post_init__(self) -> None:
        identifier(self.refusal_receipt, "pre-reservation refusal receipt")


@dataclass(frozen=True)
class ReviewReservationReleased:
    reservation_id: str
    release_receipt: str

    def __post_init__(self) -> None:
        identifier(self.reservation_id, "never-launched reservation")
        identifier(self.release_receipt, "never-launched release receipt")


@dataclass(frozen=True)
class ReviewUnlaunchedRound:
    """The closure receipt retains every refusal/release/no-launch settlement."""

    closure_receipt: str

    def __post_init__(self) -> None:
        identifier(self.closure_receipt, "unlaunched round closure receipt")


class ReviewPreparationKind(str, Enum):
    REVISION_DIFF = "revision_diff"
    CONTAINMENT = "containment"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class ReviewPreparationFailure:
    kind: ReviewPreparationKind
    evidence_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewPreparationKind):
            raise ValueError("preparation failure requires a typed evidence kind")
        identifier(self.evidence_id, "preparation failure evidence")


@dataclass(frozen=True)
class ReviewPreparationDeferred:
    failure: ReviewPreparationFailure

    def __post_init__(self) -> None:
        if not isinstance(self.failure, ReviewPreparationFailure):
            raise ValueError("preparation deferral requires verified failure evidence")


class ReviewCancellationReason(str, Enum):
    OPERATOR = "operator"
    SHUTDOWN = "shutdown"
    UNPAID_ABANDONMENT = "unpaid_abandonment"


@dataclass(frozen=True)
class ReviewRoundCancelled:
    reason: ReviewCancellationReason
    evidence_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ReviewCancellationReason):
            raise ValueError("cancelled round requires a typed non-charging reason")
        identifier(self.evidence_id, "review cancellation evidence")


ReviewExecutionDisposition = (
    PaidReviewProgress | CopiedReviewProgress | ReviewRoundFailure | ReviewInfrastructureDeath | ReviewBudgetExhausted | ReviewPinDeferred
    | ReviewAdmissionRefused | ReviewReservationReleased | ReviewRoundCancelled
    | ReviewUnlaunchedRound | ReviewPreparationDeferred
)
_DISPOSITIONS = (
    PaidReviewProgress, CopiedReviewProgress, ReviewRoundFailure, ReviewInfrastructureDeath, ReviewBudgetExhausted, ReviewPinDeferred,
    ReviewAdmissionRefused, ReviewReservationReleased, ReviewRoundCancelled,
    ReviewUnlaunchedRound, ReviewPreparationDeferred,
)


@dataclass(frozen=True)
class ReviewRoundOutcome:
    """Finalized outer outcome, never an open round or uncertain submission."""

    round: ReviewRoundIdentity
    disposition: ReviewExecutionDisposition
    session: ReviewSessionUpdate = PreserveReviewSession()
    display: PreserveDisplayedRound | SetDisplayedRound = PreserveDisplayedRound()

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity) or not isinstance(self.disposition, _DISPOSITIONS):
            raise ValueError("review outcome requires a round and typed disposition")
        if not isinstance(self.session, (PreserveReviewSession, ReplaceReviewSession, ClearReviewSession)):
            raise ValueError("review outcome requires a typed session disposition")
        if not isinstance(self.display, (PreserveDisplayedRound, SetDisplayedRound)):
            raise ValueError("review outcome requires a typed displayed-round disposition")
        paid = isinstance(self.disposition, (PaidReviewProgress, ReviewRoundFailure, ReviewInfrastructureDeath, ReviewBudgetExhausted))
        if paid:
            if any(value.child.round != self.round for value in self.disposition.launched):
                raise ValueError("outcome launch evidence belongs to another round")
        if not isinstance(self.disposition, PaidReviewProgress) and not isinstance(self.session, PreserveReviewSession):
            raise ValueError("only productive reviewer work may update the session")
        if not isinstance(self.disposition, PaidReviewProgress) and not isinstance(self.display, PreserveDisplayedRound):
            raise ValueError("only paid productive review changes its displayed round")
        producer = (
            self.session.session.source if isinstance(self.session, ReplaceReviewSession)
            else self.session.producer if isinstance(self.session, ClearReviewSession) else None
        )
        if producer is not None and (
            not paid or producer not in self.disposition.launched
        ):
            raise ValueError("session update must name this outcome's launched reviewer")


@dataclass(frozen=True)
class ReviewExecutionPolicy:
    free_pin_deferrals: int = 5

    def __post_init__(self) -> None:
        integer(self.free_pin_deferrals, "free pin deferrals")


@dataclass(frozen=True)
class ReviewExecutionState:
    binding: ReviewExecutionBinding
    failures: int = 0
    pin_deferrals: int = 0
    session: ReviewSession = NoReviewSession()
    session_sequence: int = 0
    displayed_round: int = 0
    display_sequence: int = 0
    productive_sequence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewExecutionBinding):
            raise ValueError("execution projection requires its binding")
        for name in ("failures", "pin_deferrals", "session_sequence", "displayed_round", "display_sequence", "productive_sequence"):
            integer(getattr(self, name), name)
        if not isinstance(self.session, (NoReviewSession, ReusableReviewSession)):
            raise ValueError("execution projection requires a typed session")
        if isinstance(self.session, ReusableReviewSession):
            source = self.session.source
            if isinstance(source, LaunchedReviewChild) and (
                source.child.round.binding != self.binding or source.child.round.sequence != self.session_sequence
            ):
                raise ValueError("session projection disagrees with its producer binding or sequence")
            if isinstance(source, RenewedReviewSessionSource) and self.session_sequence != 0:
                raise ValueError("an origin-carried session precedes this episode's execution rounds")


@dataclass(frozen=True)
class ReviewExecutionBoundaryCursor:
    receipt_id: str
    revision: WorkflowRevision

    def __post_init__(self) -> None:
        identifier(self.receipt_id, "compacted generation boundary")
        if not isinstance(self.revision, WorkflowRevision):
            raise ValueError("compacted generation boundary requires its revision")


_EMPTY_ARCHIVE_AUDIT_DIGEST = hashlib.sha256(b"").hexdigest()
_EMPTY_ARCHIVE_RECEIPT_SET = "0" * 64
MAX_ARCHIVE_AUDIT_STEP = 16
MAX_ARCHIVE_AUDIT_DUE_SECONDS = 86400


@dataclass(frozen=True)
class ReviewExecutionCheckpoint:
    through_sequence: int
    state: ReviewExecutionState
    policy: ReviewExecutionPolicy = ReviewExecutionPolicy()
    boundary_cursor: ReviewExecutionBoundaryCursor | None = None

    def __post_init__(self) -> None:
        integer(self.through_sequence, "execution checkpoint sequence")
        if not isinstance(self.state, ReviewExecutionState):
            raise ValueError("execution checkpoint requires a validated projection")
        if not isinstance(self.policy, ReviewExecutionPolicy):
            raise ValueError("execution checkpoint requires its captured episode policy")
        if (self.boundary_cursor is not None
                and (not isinstance(self.boundary_cursor, ReviewExecutionBoundaryCursor)
                     or self.boundary_cursor.revision.fence != self.state.binding.rejection.fence)):
            raise ValueError("execution checkpoint boundary cursor disagrees with its state")
        if max(self.state.session_sequence, self.state.display_sequence, self.state.productive_sequence) > self.through_sequence:
            raise ValueError("execution checkpoint cannot contain a future outcome")


class ReviewExecutionOriginKind(str, Enum):
    INITIALIZED = "initialized"
    RENEWED = "renewed"


@dataclass(frozen=True)
class ReviewExecutionOrigin:
    kind: ReviewExecutionOriginKind
    evidence_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewExecutionOriginKind):
            raise ValueError("execution origin requires a typed cause")
        identifier(self.evidence_id, "execution origin evidence")


@dataclass(frozen=True)
class InitializedReviewExecutionOriginReceipt:
    """A fresh episode bound to the first harness observation of its PR."""

    origin: ReviewExecutionOrigin
    checkpoint: ReviewExecutionCheckpoint
    observation_evidence_id: str
    activation_plan_digest: str
    source_digest: str
    inventory_digest: str
    activated_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or self.origin.kind is not ReviewExecutionOriginKind.INITIALIZED
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or self.checkpoint.through_sequence != 0
                or self.checkpoint.boundary_cursor is not None
                or self.checkpoint.state != ReviewExecutionState(self.checkpoint.state.binding)):
            raise ValueError("initialized execution origin requires a fresh sequence-zero checkpoint")
        identifier(self.observation_evidence_id, "initialized execution observation")
        for name in ("activation_plan_digest", "source_digest", "inventory_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"initialized execution origin {name} requires a SHA-256 digest")
        if (not isinstance(self.activated_at, datetime) or self.activated_at.tzinfo is None
                or self.activated_at.utcoffset() is None):
            raise ValueError("initialized execution origin requires an aware activation time")


def initialized_review_execution(
    rejection: WorkflowBinding, execution_epoch: int, observation_evidence_id: str,
    activation_plan_digest: str, source_digest: str, inventory_digest: str,
    activated_at: datetime,
) -> tuple[ActivatedReviewExecution, InitializedReviewExecutionOriginReceipt]:
    """Derive the one fresh episode for a first observed PR and authority pair."""
    integer(execution_epoch, "execution epoch", minimum=1)
    identifier(observation_evidence_id, "initialized execution observation")
    if (not isinstance(rejection, WorkflowBinding)
            or rejection.authority.family is not WorkflowFamily.REVIEW_REJECTIONS
            or not isinstance(rejection.fence.key.target, PullRequestRef)):
        raise ValueError("initialized execution requires a review rejection binding")
    parts = (rejection.fence.key.repository, str(rejection.fence.key.target.number),
             str(rejection.authority.epoch), rejection.episode_id, str(execution_epoch))
    episode = "review-execution-initial:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()
    origin_id = "review-execution-origin:" + hashlib.sha256(
        (episode + "\0" + observation_evidence_id).encode(),
    ).hexdigest()
    binding = ReviewExecutionBinding(rejection, execution_epoch, episode)
    state = ReviewExecutionState(binding)
    checkpoint = ReviewExecutionCheckpoint(0, state)
    origin = ReviewExecutionOrigin(ReviewExecutionOriginKind.INITIALIZED, origin_id)
    receipt = InitializedReviewExecutionOriginReceipt(
        origin, checkpoint, observation_evidence_id,
        activation_plan_digest, source_digest, inventory_digest, activated_at,
    )
    return ActivatedReviewExecution(origin, checkpoint, state, 0), receipt


@dataclass(frozen=True)
class ExecutionNotActivated:
    pass


@dataclass(frozen=True)
class ActivatedReviewExecution:
    origin: ReviewExecutionOrigin
    checkpoint: ReviewExecutionCheckpoint
    state: ReviewExecutionState
    last_sequence: int
    checkpoint_receipt: str | None = None
    boundaries: tuple[ReviewExecutionGenerationBoundary, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.origin, ReviewExecutionOrigin):
            raise ValueError("activated execution requires its origin evidence")
        if not isinstance(self.checkpoint, ReviewExecutionCheckpoint) or not isinstance(self.state, ReviewExecutionState):
            raise ValueError("activated execution requires its checkpoint and current state")
        if not isinstance(self.boundaries, tuple) or len(self.boundaries) > MAX_EXECUTION_SUFFIX or any(
            not isinstance(boundary, ReviewExecutionGenerationBoundary) for boundary in self.boundaries
        ):
            raise ValueError("activated execution requires bounded generation boundaries")
        if self.boundaries:
            if (self.checkpoint.state.binding != self.boundaries[0].before.binding
                    or self.boundaries[-1].after.binding != self.state.binding
                    or self.boundaries[-1].through_sequence > self.last_sequence):
                raise ValueError("generation boundaries disagree with active execution")
        elif self.checkpoint.state.binding != self.state.binding:
            raise ValueError("execution checkpoint and state belong to different bindings")
        integer(self.last_sequence, "last allocated review round")
        if self.checkpoint.through_sequence > self.last_sequence:
            raise ValueError("execution checkpoint exceeds allocated rounds")
        if self.checkpoint_receipt is not None:
            identifier(self.checkpoint_receipt, "execution checkpoint receipt")
        if (self.checkpoint.through_sequence == 0 and self.checkpoint.boundary_cursor is None
                and self.checkpoint_receipt is not None):
            raise ValueError("initial execution checkpoint has no compaction receipt")
        if max(self.state.session_sequence, self.state.display_sequence, self.state.productive_sequence) > self.last_sequence:
            raise ValueError("execution state contains a future outcome")
        if (not self.boundaries and self.checkpoint.through_sequence == self.last_sequence
                and self.checkpoint.state != self.state):
            raise ValueError("execution state disagrees with its completed checkpoint")
        if self.last_sequence == 0:
            if self.state.failures or self.state.pin_deferrals or not isinstance(self.state.session, NoReviewSession):
                raise ValueError("a fresh execution origin cannot invent failures or a reusable session")

    @property
    def binding(self) -> ReviewExecutionBinding:
        return self.state.binding


@dataclass(frozen=True)
class CurrentReviewExecution:
    """Generation-local authority under ADR 0016; retained work lives outside it.

    ``ActivatedReviewExecution`` remains the legacy adapter encoding until the
    stopped-writer conversion. Current authority never contains its ancestry.
    """

    origin: ReviewExecutionOrigin
    baseline: ReviewExecutionCheckpoint
    state: ReviewExecutionState
    generation_start_sequence: int
    last_sequence: int

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or not isinstance(self.baseline, ReviewExecutionCheckpoint)
                or not isinstance(self.state, ReviewExecutionState)):
            raise ValueError("current execution requires typed origin, baseline and state")
        integer(self.generation_start_sequence, "generation start sequence")
        integer(self.last_sequence, "last allocated review round")
        if (self.baseline.boundary_cursor is not None
                or self.baseline.state.binding != self.state.binding):
            raise ValueError("current execution baseline requires the current generation binding")
        if not self.generation_start_sequence <= self.baseline.through_sequence <= self.last_sequence:
            raise ValueError("current execution baseline exceeds its allocated generation range")
        if self.last_sequence - self.baseline.through_sequence > MAX_EXECUTION_SUFFIX:
            raise ValueError("current execution pending capacity is exhausted")
        if max(self.state.session_sequence, self.state.display_sequence,
               self.state.productive_sequence) > self.last_sequence:
            raise ValueError("current execution state contains a future outcome")
        if self.baseline.through_sequence == self.last_sequence and self.baseline.state != self.state:
            raise ValueError("current execution state disagrees with its completed baseline")
        for state in (self.baseline.state, self.state):
            if (isinstance(state.session, ReusableReviewSession)
                    and isinstance(state.session.source, RenewedReviewSessionSource)
                    and (self.origin.kind is not ReviewExecutionOriginKind.RENEWED
                         or state.session.source.evidence_id != self.origin.evidence_id)):
                raise ValueError("a renewed session requires its exact renewed origin")
        if (self.last_sequence == 0
                and (self.state.failures or self.state.pin_deferrals
                     or not isinstance(self.state.session, NoReviewSession)
                     and not isinstance(self.state.session.source, RenewedReviewSessionSource))):
            raise ValueError("a fresh execution origin cannot invent prior allowance or session")

    @property
    def binding(self) -> ReviewExecutionBinding:
        return self.state.binding


@dataclass(frozen=True)
class RenewedReviewExecutionOriginReceipt:
    """One operator grant, without prior states or an execution ancestry chain."""

    origin: ReviewExecutionOrigin
    checkpoint: ReviewExecutionCheckpoint
    previous_binding: ReviewExecutionBinding
    request_id: str
    expected: WorkflowRevision

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or self.origin.kind is not ReviewExecutionOriginKind.RENEWED
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or self.checkpoint.through_sequence != 0
                or self.checkpoint.boundary_cursor is not None
                or not isinstance(self.previous_binding, ReviewExecutionBinding)
                or not isinstance(self.expected, WorkflowRevision)):
            raise ValueError("renewed origin requires one exact sequence-zero grant")
        identifier(self.request_id, "execution renewal request")
        after = self.checkpoint.state.binding
        before = self.previous_binding
        if (before.rejection.fence != self.expected.fence
                or after.rejection.fence.key != before.rejection.fence.key
                or after.rejection.fence.generation != before.rejection.fence.generation + 1
                or after.rejection.authority != before.rejection.authority
                or after.rejection.episode_id != before.rejection.episode_id
                or after.execution_epoch != before.execution_epoch
                or after.execution_episode == before.execution_episode):
            raise ValueError("renewed origin must replace only its selected execution episode")
        CurrentReviewExecution(self.origin, self.checkpoint, self.checkpoint.state, 0, 0)


def renew_current_review_execution(
    execution: CurrentReviewExecution, expected: WorkflowRevision,
    rejection: WorkflowBinding, request_id: str,
) -> tuple[CurrentReviewExecution, RenewedReviewExecutionOriginReceipt]:
    """Reset the selected allowance, retaining display and compatible handle facts.

    The adapter separately proves complete old-episode quiescence at acceptance;
    this current-generation check cannot certify older retained work.
    """
    if (not isinstance(execution, CurrentReviewExecution)
            or execution.baseline.through_sequence != execution.last_sequence
            or not isinstance(expected, WorkflowRevision)
            or not isinstance(rejection, WorkflowBinding)
            or expected.fence != execution.binding.rejection.fence):
        raise ValueError("execution renewal requires a terminal current suffix")
    identifier(request_id, "execution renewal request")
    before = execution.binding
    key = before.rejection.fence.key
    parts = (key.repository, str(key.target.number), str(before.rejection.authority.epoch),
             before.rejection.episode_id, str(before.execution_epoch), before.execution_episode,
             str(expected.fence.generation), str(expected.version), request_id)
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    origin = ReviewExecutionOrigin(ReviewExecutionOriginKind.RENEWED, "review-execution-renewal:" + digest)
    binding = ReviewExecutionBinding(rejection, execution.binding.execution_epoch, "review-execution-renewed:" + digest)
    session = execution.state.session
    if isinstance(session, ReusableReviewSession):
        session = ReusableReviewSession(session.runtime, session.handle, RenewedReviewSessionSource(origin.evidence_id))
    state = ReviewExecutionState(binding, session=session, displayed_round=execution.state.displayed_round)
    checkpoint = ReviewExecutionCheckpoint(0, state, execution.baseline.policy)
    receipt = RenewedReviewExecutionOriginReceipt(origin, checkpoint, execution.binding, request_id, expected)
    return CurrentReviewExecution(origin, checkpoint, state, 0, 0), receipt


ReviewExecutionComponent = ExecutionNotActivated | ActivatedReviewExecution | CurrentReviewExecution


@dataclass(frozen=True)
class OpenReviewExecutionRound:
    round: ReviewRoundIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity):
            raise ValueError("open execution round requires its allocated identity")


@dataclass(frozen=True)
class CompletedReviewExecutionRound:
    outcome: ReviewRoundOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReviewRoundOutcome):
            raise ValueError("completed execution round requires its typed outcome")

    @property
    def round(self) -> ReviewRoundIdentity:
        return self.outcome.round


PendingReviewExecutionRound = OpenReviewExecutionRound | CompletedReviewExecutionRound


@dataclass(frozen=True)
class ReviewExecutionReduction:
    execution: CurrentReviewExecution
    pending: tuple[PendingReviewExecutionRound, ...]


@dataclass(frozen=True)
class ReviewExecutionProjection:
    state: ReviewExecutionState
    checkpoint: ReviewExecutionCheckpoint
    latest_sequence: int


def same_execution_episode(current: ReviewExecutionBinding, observed: ReviewExecutionBinding) -> bool:
    """Whether two round bindings belong to one execution episode across generations."""
    if not isinstance(current, ReviewExecutionBinding) or not isinstance(observed, ReviewExecutionBinding):
        return False
    return (
        current.execution_epoch == observed.execution_epoch
        and current.execution_episode == observed.execution_episode
        and current.rejection.authority == observed.rejection.authority
        and current.rejection.episode_id == observed.rejection.episode_id
        and current.rejection.fence.key == observed.rejection.fence.key
    )


def carry_review_execution_generation(
    state: ReviewExecutionState, binding: ReviewExecutionBinding, through_sequence: int,
) -> ReviewExecutionState:
    """Preserve execution allowance when rejection recovery owes a fresh review."""
    if not isinstance(state, ReviewExecutionState) or not isinstance(binding, ReviewExecutionBinding):
        raise ValueError("generation carry requires validated execution state and binding")
    integer(through_sequence, "generation boundary sequence")
    if (not same_execution_episode(state.binding, binding)
            or binding.rejection.fence.generation != state.binding.rejection.fence.generation + 1
            or through_sequence < max(state.session_sequence, state.display_sequence,
                                      state.productive_sequence)):
        raise ValueError("generation carry requires the next rejection generation and allocated sequence")
    return replace(
        state, binding=binding, session=NoReviewSession(), session_sequence=through_sequence,
    )


@dataclass(frozen=True)
class ReviewExecutionGenerationBoundary:
    """Frozen projection at a generation advance, distinct from finalized prefix."""

    before: ReviewExecutionState
    after: ReviewExecutionState
    through_sequence: int
    applied_sequences: tuple[int, ...]
    receipt_id: str
    from_revision: WorkflowRevision
    to_revision: WorkflowRevision
    mutation: WorkflowMutation

    def __post_init__(self) -> None:
        if not isinstance(self.before, ReviewExecutionState) or not isinstance(self.after, ReviewExecutionState):
            raise ValueError("generation boundary requires validated states")
        integer(self.through_sequence, "generation boundary sequence")
        identifier(self.receipt_id, "generation boundary receipt")
        if (not isinstance(self.from_revision, WorkflowRevision)
                or not isinstance(self.to_revision, WorkflowRevision)
                or self.from_revision.fence != self.before.binding.rejection.fence
                or self.to_revision.fence != self.after.binding.rejection.fence
                or self.to_revision.version != self.from_revision.version + 1
                or not isinstance(self.mutation, WorkflowMutation)
                or self.mutation not in (
                    WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM,
                    WorkflowMutation.RELEASE_CEILING,
                )):
            raise ValueError("generation boundary requires its exact advancing transition")
        if self.after != carry_review_execution_generation(
            self.before, self.after.binding, self.through_sequence,
        ):
            raise ValueError("generation boundary must carry the prior allowance and clear its session")
        if (not isinstance(self.applied_sequences, tuple)
                or len(self.applied_sequences) > MAX_EXECUTION_SUFFIX):
            raise ValueError("generation boundary requires bounded applied round identities")
        previous = 0
        for sequence in self.applied_sequences:
            integer(sequence, "generation boundary applied sequence", minimum=1)
            if sequence <= previous or sequence > self.through_sequence:
                raise ValueError("generation boundary applied sequences must be ordered and allocated")
            previous = sequence


@dataclass(frozen=True)
class ReviewExecutionAcceptedTransitionSource:
    """A bounded generation receipt binding its accepted transition by digest."""

    boundary: ReviewExecutionGenerationBoundary
    namespace: str
    key: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, ReviewExecutionGenerationBoundary):
            raise ValueError("accepted transition source requires a generation boundary")
        if self.namespace not in {"workflow-request", "workflow-transition"}:
            raise ValueError("accepted transition source requires a known namespace")
        identifier(self.key, "accepted generation transition key")
        if (not isinstance(self.payload_sha256, str) or len(self.payload_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.payload_sha256)):
            raise ValueError("accepted transition source requires a SHA-256 digest")


@dataclass(frozen=True)
class ReviewExecutionTransitionProofSource:
    """Small accepted-transition proof minted with a generation boundary."""

    boundary: ReviewExecutionGenerationBoundary
    namespace: str
    key: str
    payload_sha256: str
    audit_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, ReviewExecutionGenerationBoundary):
            raise ValueError("generation transition proof requires its boundary")
        identifier(self.key, "generation transition proof key")
        for name, digest in (("payload", self.payload_sha256), ("audit", self.audit_sha256)):
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError(f"generation transition proof requires a SHA-256 {name} digest")
        if self.boundary.mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
            if self.namespace != "workflow-request":
                raise ValueError("generation request proof disagrees with its boundary")
        elif self.boundary.mutation is WorkflowMutation.RELEASE_CEILING:
            if self.namespace != "workflow-transition":
                raise ValueError("generation release proof disagrees with its boundary")
        else:
            raise ValueError("generation transition proof requires an advancing mutation")


def _apply(
    state: ReviewExecutionState, outcome: ReviewRoundOutcome, policy: ReviewExecutionPolicy,
) -> ReviewExecutionState:
    sequence, result = outcome.round.sequence, outcome.disposition
    if outcome.round.binding.rejection.fence.generation < state.binding.rejection.fence.generation:
        return state
    failures, pins, productive = state.failures, state.pin_deferrals, state.productive_sequence
    if isinstance(result, (PaidReviewProgress, CopiedReviewProgress)):
        failures, pins, productive = 0, 0, sequence
    elif isinstance(result, ReviewRoundFailure):
        failures = (min(failures + 1, _MAX_INTEGER) if isinstance(result.debit, AddReviewStrike)
                    else max(failures, result.debit.ceiling))
    elif isinstance(result, ReviewPinDeferred):
        # An unavailable pinned checkout is a machine failure, never a strike;
        # the count is the streak the review plan's backoff reads.
        # `policy.free_pin_deferrals` is inert until the persisted checkpoints
        # that carry it are cut over.
        pins = min(pins + 1, _MAX_INTEGER)
    session, session_sequence = state.session, state.session_sequence
    if isinstance(outcome.session, ReplaceReviewSession):
        session, session_sequence = outcome.session.session, sequence
    elif isinstance(outcome.session, ClearReviewSession):
        session, session_sequence = NoReviewSession(), sequence
    display, display_sequence = state.displayed_round, state.display_sequence
    if isinstance(outcome.display, SetDisplayedRound):
        display, display_sequence = outcome.display.number, sequence
    return replace(
        state, failures=failures, pin_deferrals=pins, productive_sequence=productive,
        session=session, session_sequence=session_sequence, displayed_round=display,
        display_sequence=display_sequence,
    )


def project_review_execution(
    checkpoint: ReviewExecutionCheckpoint,
    outcomes: tuple[ReviewRoundOutcome, ...],
) -> ReviewExecutionProjection:
    """Project finalized suffix outcomes in preflight order, independent of delivery.

    The adapter retains the suffix until all preceding rounds are finalized.
    Exact replay below the checkpoint must be verified against its immutable
    receipt before calling this function; a compacted receipt cannot be checked
    from the aggregate alone. An oversized unresolved suffix refuses rather
    than discarding a gap and treating later outcomes as contiguous.
    """
    if not isinstance(checkpoint, ReviewExecutionCheckpoint):
        raise ValueError("execution projection requires a checkpoint")
    if not isinstance(outcomes, tuple) or len(outcomes) > MAX_EXECUTION_SUFFIX:
        raise ValueError("execution suffix must be a bounded tuple")
    ordered: dict[int, ReviewRoundOutcome] = {}
    identities: dict[str, ReviewRoundIdentity] = {}
    for outcome in outcomes:
        if not isinstance(outcome, ReviewRoundOutcome):
            raise ValueError("execution suffix contains an invalid outcome")
        identity = outcome.round
        if not same_execution_episode(checkpoint.state.binding, identity.binding):
            raise ValueError("execution outcome belongs to another authority or episode")
        if identity.binding.rejection.fence.generation > checkpoint.state.binding.rejection.fence.generation:
            raise ValueError("execution outcome belongs to a future generation")
        if identity.sequence <= checkpoint.through_sequence:
            raise ValueError("compacted execution outcome requires prior receipt replay verification")
        if identity.sequence in ordered and ordered[identity.sequence] != outcome:
            raise ValueError("conflicting outcomes for one review round sequence")
        if identity.round_id in identities and identities[identity.round_id] != identity:
            raise ValueError("one review round cannot have multiple identities")
        ordered[identity.sequence] = outcome
        identities[identity.round_id] = identity
    state, prefix = checkpoint.state, checkpoint
    for sequence, outcome in sorted(ordered.items()):
        state = _apply(state, outcome, checkpoint.policy)
        if sequence == prefix.through_sequence + 1:
            prefix = ReviewExecutionCheckpoint(sequence, state, checkpoint.policy, checkpoint.boundary_cursor)
    return ReviewExecutionProjection(state, prefix, max(ordered, default=checkpoint.through_sequence))


def allocate_current_review_round(
    execution: CurrentReviewExecution, round_id: str,
) -> tuple[CurrentReviewExecution, ReviewRoundIdentity]:
    """Allocate no partial identity when unresolved work is at capacity.

    The adapter enforces round-id uniqueness against retained SQL identities.
    """
    if not isinstance(execution, CurrentReviewExecution):
        raise ValueError("round allocation requires current execution authority")
    if execution.last_sequence - execution.baseline.through_sequence >= MAX_EXECUTION_SUFFIX:
        raise ValueError("current execution pending capacity is exhausted")
    round = ReviewRoundIdentity(execution.binding, round_id, execution.last_sequence + 1)
    return replace(execution, last_sequence=round.sequence), round


def reduce_current_review_execution(
    execution: CurrentReviewExecution, pending: tuple[PendingReviewExecutionRound, ...],
) -> ReviewExecutionReduction:
    """Recompute only allocated unresolved work and retire its completed prefix."""
    if not isinstance(execution, CurrentReviewExecution):
        raise ValueError("execution reduction requires current authority")
    if (not isinstance(pending, tuple) or len(pending) > MAX_EXECUTION_SUFFIX
            or len(pending) != execution.last_sequence - execution.baseline.through_sequence):
        raise ValueError("execution reduction requires the complete bounded pending range")
    ordered: dict[int, PendingReviewExecutionRound] = {}
    identities: set[str] = set()
    for item in pending:
        if not isinstance(item, (OpenReviewExecutionRound, CompletedReviewExecutionRound)):
            raise ValueError("execution pending range contains an untyped round")
        round = item.round
        if (round.binding != execution.binding
                or not execution.baseline.through_sequence < round.sequence <= execution.last_sequence):
            raise ValueError("execution pending round belongs to a foreign or unallocated identity")
        if round.sequence in ordered or round.round_id in identities:
            raise ValueError("execution pending range has conflicting round identities")
        ordered[round.sequence] = item
        identities.add(round.round_id)
    state, baseline = execution.baseline.state, execution.baseline
    for sequence, item in sorted(ordered.items()):
        if isinstance(item, CompletedReviewExecutionRound):
            state = _apply(state, item.outcome, baseline.policy)
            if sequence == baseline.through_sequence + 1:
                baseline = ReviewExecutionCheckpoint(sequence, state, baseline.policy)
    return ReviewExecutionReduction(
        replace(execution, baseline=baseline, state=state),
        tuple(item for sequence, item in sorted(ordered.items()) if sequence > baseline.through_sequence),
    )


def finalize_current_review_round(
    execution: CurrentReviewExecution, pending: tuple[PendingReviewExecutionRound, ...],
    outcome: ReviewRoundOutcome,
) -> ReviewExecutionReduction:
    """Exact pending retries are inert; retired retries require indexed receipts."""
    if not isinstance(outcome, ReviewRoundOutcome) or not isinstance(execution, CurrentReviewExecution):
        raise ValueError("execution finalization requires current authority and a typed outcome")
    # Late generations are settled through retained receipts by the adapter,
    # never folded into this generation's baseline or pending range.
    if outcome.round.binding != execution.binding:
        raise ValueError("execution finalization belongs to a foreign or stale generation")
    if outcome.round.sequence <= execution.baseline.through_sequence:
        raise ValueError("retired execution outcome requires exact receipt replay verification")
    validated = reduce_current_review_execution(execution, pending)
    if validated.execution.state != execution.state:
        raise ValueError("current execution state disagrees with its pending projection")
    selected = next((item for item in pending if item.round == outcome.round), None)
    if selected is None:
        raise ValueError("execution outcome lacks its exact allocated pending identity")
    if isinstance(selected, CompletedReviewExecutionRound):
        if selected.outcome != outcome:
            raise ValueError("conflicting duplicate execution outcome")
        return validated
    return reduce_current_review_execution(
        execution, tuple(CompletedReviewExecutionRound(outcome) if item == selected else item for item in pending),
    )


def advance_current_review_generation(
    execution: CurrentReviewExecution, pending: tuple[PendingReviewExecutionRound, ...],
    from_revision: WorkflowRevision, to_revision: WorkflowRevision, mutation: WorkflowMutation,
) -> tuple[CurrentReviewExecution, ReviewExecutionGenerationBoundary]:
    """Freeze the accepted projection; old open rounds remain separately recoverable."""
    validated = reduce_current_review_execution(execution, pending)
    if validated.execution.state != execution.state:
        raise ValueError("current execution state disagrees with its pending projection")
    return freeze_current_review_generation(
        execution, from_revision, to_revision, mutation,
        tuple(item.round.sequence for item in sorted(pending, key=lambda item: item.round.sequence)
              if isinstance(item, CompletedReviewExecutionRound)),
    )


def freeze_current_review_generation(
    execution: CurrentReviewExecution, from_revision: WorkflowRevision, to_revision: WorkflowRevision,
    mutation: WorkflowMutation, applied_sequences: tuple[int, ...],
) -> tuple[CurrentReviewExecution, ReviewExecutionGenerationBoundary]:
    """Carry trusted SQL state; the adapter supplies identities from its validated pending rows."""
    if (not isinstance(execution, CurrentReviewExecution)
            or not isinstance(from_revision, WorkflowRevision)
            or not isinstance(to_revision, WorkflowRevision)
            or from_revision.fence != execution.binding.rejection.fence):
        raise ValueError("generation advance requires current authority and its exact revision")
    if not isinstance(applied_sequences, tuple) or any(
        type(sequence) is not int or sequence <= execution.baseline.through_sequence
        or sequence > execution.last_sequence for sequence in applied_sequences
    ):
        raise ValueError("generation freeze requires selected pending finalized identities")
    binding = replace(execution.binding, rejection=replace(execution.binding.rejection, fence=to_revision.fence))
    after = carry_review_execution_generation(execution.state, binding, execution.last_sequence)
    boundary = ReviewExecutionGenerationBoundary(
        execution.state, after, execution.last_sequence,
        applied_sequences,
        review_generation_receipt_id(execution.binding, to_revision.fence.generation),
        from_revision, to_revision, mutation,
    )
    return CurrentReviewExecution(
        execution.origin, ReviewExecutionCheckpoint(execution.last_sequence, after, execution.baseline.policy),
        after, execution.last_sequence, execution.last_sequence,
    ), boundary


def review_generation_receipt_id(binding: ReviewExecutionBinding, to_generation: int) -> str:
    """Derive an episode's accepted generation source identity for indexed audit reads."""
    if (not isinstance(binding, ReviewExecutionBinding)
            or not isinstance(binding.rejection.fence.key.target, PullRequestRef)):
        raise ValueError("review generation source requires a review execution PR binding")
    integer(to_generation, "review generation source generation", minimum=1)
    key = binding.rejection.fence.key
    identity = (
        f"{key.repository}:{key.target.number}:"
        f"{binding.execution_epoch}:{binding.execution_episode}:{to_generation}"
    )
    return "review-generation:" + hashlib.sha256(identity.encode()).hexdigest()
