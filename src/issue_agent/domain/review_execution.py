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
class ImportedReviewSession:
    evidence_id: str

    def __post_init__(self) -> None:
        identifier(self.evidence_id, "imported session evidence")


@dataclass(frozen=True)
class NoReviewSession:
    pass


@dataclass(frozen=True)
class ReusableReviewSession:
    runtime: RuntimeName
    handle: str
    source: LaunchedReviewChild | ImportedReviewSession

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("review session requires its runtime")
        validate_review_session_handle(self.handle)
        if not isinstance(self.source, (LaunchedReviewChild, ImportedReviewSession)):
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
            if isinstance(source, ImportedReviewSession) and self.session_sequence != 0:
                raise ValueError("an imported session precedes activated execution rounds")


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


def _archive_audit_digest(value: object, name: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} requires a SHA-256 digest")


def _archive_receipt_member(
    generation: int, receipt_id: str, receipt_sha256: str,
    accepted_namespace: str, accepted_key: str,
) -> int:
    # Expected receipts are generation ordered; the namespace census is key ordered.
    encoded = "\0".join((str(generation), receipt_id, receipt_sha256,
                         accepted_namespace, accepted_key)).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest())


@dataclass(frozen=True)
class ReviewGenerationReceiptSweepCursor:
    scope: str
    key: str

    def __post_init__(self) -> None:
        if (not isinstance(self.scope, str)
                or canonical_workflow_repository(self.scope) != self.scope):
            raise ValueError("generation receipt sweep cursor requires a canonical repository")
        identifier(self.key, "generation receipt sweep key")


@dataclass(frozen=True)
class ReviewGenerationArchiveReceiptMember:
    generation: int
    receipt_id: str
    receipt_sha256: str
    accepted_namespace: str
    accepted_key: str

    def __post_init__(self) -> None:
        integer(self.generation, "swept generation", minimum=1)
        identifier(self.receipt_id, "swept generation receipt")
        _archive_audit_digest(self.receipt_sha256, "swept receipt digest")
        if self.accepted_namespace not in {"workflow-request", "workflow-transition"}:
            raise ValueError("swept receipt requires an accepted transition namespace")
        identifier(self.accepted_key, "swept accepted key")


@dataclass(frozen=True)
class ReviewGenerationArchiveFingerprint:
    """Small identity of one already verified retained generation source pair."""

    cursor: ReviewExecutionBoundaryCursor
    receipt_sha256: str
    accepted_namespace: str
    accepted_key: str
    accepted_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, ReviewExecutionBoundaryCursor):
            raise ValueError("archive fingerprint requires its generation cursor")
        if self.accepted_namespace not in {"workflow-request", "workflow-transition"}:
            raise ValueError("archive fingerprint requires an accepted transition namespace")
        identifier(self.accepted_key, "archive fingerprint accepted key")
        _archive_audit_digest(self.receipt_sha256, "archive receipt digest")
        _archive_audit_digest(self.accepted_sha256, "archive accepted digest")


@dataclass(frozen=True)
class ReviewGenerationArchiveBaseline:
    through_generation: int
    rolling_sha256: str

    def __post_init__(self) -> None:
        integer(self.through_generation, "archive baseline frontier", minimum=0)
        _archive_audit_digest(self.rolling_sha256, "archive baseline digest")


@dataclass(frozen=True)
class ReviewGenerationArchiveScan:
    """A durable, incomplete expected-generation scan; no admission authority."""

    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_generation: int
    next_generation: int
    preceding: ReviewExecutionBoundaryCursor | None
    rolling_sha256: str
    receipt_membership_xor: str
    baseline: ReviewGenerationArchiveBaseline | None
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionBinding)
                or not isinstance(self.origin_receipt, ReviewExecutionOrigin)):
            raise ValueError("archive scan requires its execution origin binding")
        _archive_audit_digest(self.origin_source_sha256, "archive origin source digest")
        base = self.origin.rejection.fence.generation
        integer(self.through_generation, "archive scan frontier", minimum=base + 1)
        integer(self.next_generation, "archive scan next generation", minimum=base + 1)
        if self.next_generation > self.through_generation:
            raise ValueError("archive scan must have an unvisited generation")
        if self.next_generation == base + 1:
            if self.preceding is not None:
                raise ValueError("initial archive scan cannot invent a predecessor")
        elif (not isinstance(self.preceding, ReviewExecutionBoundaryCursor)
              or self.preceding.revision.fence.generation != self.next_generation - 1
              or self.preceding.receipt_id != review_generation_receipt_id(
                  self.origin, self.next_generation - 1,
              ) or self.preceding.revision.fence.key != self.origin.rejection.fence.key):
            raise ValueError("archive scan predecessor disagrees with its frontier")
        _archive_audit_digest(self.rolling_sha256, "archive rolling digest")
        _archive_audit_digest(self.receipt_membership_xor, "archive receipt set")
        if (self.baseline is not None and (not isinstance(self.baseline, ReviewGenerationArchiveBaseline)
                or not self.next_generation <= self.baseline.through_generation <= self.through_generation)):
            raise ValueError("archive scan baseline must be an unvisited prefix")
        if (not isinstance(self.started_at, datetime) or self.started_at.tzinfo is None
                or self.started_at.utcoffset() is None or not isinstance(self.due_at, datetime)
                or self.due_at.tzinfo is None or self.due_at.utcoffset() is None):
            raise ValueError("archive scan requires aware start and due times")


@dataclass(frozen=True)
class CompletedReviewGenerationArchiveScan:
    """One completed pass; the next pass must revisit the retained sources."""

    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_generation: int
    final_cursor: ReviewExecutionBoundaryCursor | None
    rolling_sha256: str
    receipt_membership_xor: str
    started_at: datetime
    completed_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionBinding)
                or not isinstance(self.origin_receipt, ReviewExecutionOrigin)):
            raise ValueError("completed archive scan requires its origin")
        _archive_audit_digest(self.origin_source_sha256, "completed archive origin source digest")
        base = self.origin.rejection.fence.generation
        integer(self.through_generation, "completed archive frontier", minimum=base)
        if self.through_generation == base:
            if (self.final_cursor is not None or self.rolling_sha256 != _EMPTY_ARCHIVE_AUDIT_DIGEST
                    or self.receipt_membership_xor != _EMPTY_ARCHIVE_RECEIPT_SET):
                raise ValueError("empty archive scan cannot invent a source")
        elif (not isinstance(self.final_cursor, ReviewExecutionBoundaryCursor)
              or self.final_cursor.revision.fence.generation != self.through_generation
              or self.final_cursor.revision.fence.key != self.origin.rejection.fence.key
              or self.final_cursor.receipt_id != review_generation_receipt_id(
                  self.origin, self.through_generation,
              )):
            raise ValueError("completed archive scan requires its final source")
        _archive_audit_digest(self.rolling_sha256, "completed archive digest")
        _archive_audit_digest(self.receipt_membership_xor, "completed archive receipt set")
        if (not isinstance(self.started_at, datetime) or self.started_at.tzinfo is None
                or self.started_at.utcoffset() is None or not isinstance(self.completed_at, datetime)
                or self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
                or not isinstance(self.due_at, datetime) or self.due_at.tzinfo is None
                or self.due_at.utcoffset() is None
                or not self.started_at <= self.completed_at):
            raise ValueError("completed archive scan requires ordered aware times")


@dataclass(frozen=True)
class ReviewGenerationArchiveSweep:
    """Resume the namespace census after the expected source pass."""

    generation_scan: CompletedReviewGenerationArchiveScan
    after: ReviewGenerationReceiptSweepCursor | None = None
    upper: ReviewGenerationReceiptSweepCursor | None = None
    receipt_membership_xor: str = _EMPTY_ARCHIVE_RECEIPT_SET
    source_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.generation_scan, CompletedReviewGenerationArchiveScan):
            raise ValueError("archive sweep requires a completed expected generation pass")
        if (self.after is not None and not isinstance(self.after, ReviewGenerationReceiptSweepCursor)
                or self.upper is not None and not isinstance(self.upper, ReviewGenerationReceiptSweepCursor)
                or self.after is not None and self.upper is None
                or self.after is not None and self.upper is not None
                and (self.after.scope, self.after.key) > (self.upper.scope, self.upper.key)):
            raise ValueError("archive sweep requires ordered bounded namespace cursors")
        _archive_audit_digest(self.receipt_membership_xor, "archive sweep receipt set")
        expected = (self.generation_scan.through_generation
                    - self.generation_scan.origin.rejection.fence.generation)
        integer(self.source_count, "archive sweep source count", minimum=0)
        if (self.source_count > expected or self.source_count == 0
                and self.receipt_membership_xor != _EMPTY_ARCHIVE_RECEIPT_SET):
            raise ValueError("archive sweep source set exceeds its expected frontier")


@dataclass(frozen=True)
class CompletedReviewGenerationArchiveAudit:
    """One full expected-source and namespace pass, still without admission authority."""

    generation_scan: CompletedReviewGenerationArchiveScan
    sweep_upper: ReviewGenerationReceiptSweepCursor | None
    receipt_membership_xor: str
    source_count: int
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.generation_scan, CompletedReviewGenerationArchiveScan)
                or self.sweep_upper is not None
                and not isinstance(self.sweep_upper, ReviewGenerationReceiptSweepCursor)):
            raise ValueError("completed archive audit requires its expected pass and sweep frontier")
        _archive_audit_digest(self.receipt_membership_xor, "completed archive sweep receipt set")
        expected = (self.generation_scan.through_generation
                    - self.generation_scan.origin.rejection.fence.generation)
        if (type(self.source_count) is not int or self.source_count != expected
                or self.receipt_membership_xor != self.generation_scan.receipt_membership_xor
                or expected > 0 and self.sweep_upper is None
                or not isinstance(self.completed_at, datetime)
                or self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
                or not isinstance(self.next_due_at, datetime)
                or self.next_due_at.tzinfo is None or self.next_due_at.utcoffset() is None
                or not self.generation_scan.completed_at <= self.completed_at < self.next_due_at):
            raise ValueError("completed archive audit requires a matching source census and due time")


@dataclass(frozen=True)
class BlockedReviewGenerationArchiveScan:
    scan: ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan
    generation: int
    reason: str
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.scan, (ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan))
                or type(self.generation) is not int
                or not (self.scan.next_generation if isinstance(self.scan, ReviewGenerationArchiveScan)
                        else self.scan.through_generation) <= self.generation <= self.scan.through_generation
                or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or not isinstance(self.detected_at, datetime) or self.detected_at.tzinfo is None
                or self.detected_at.utcoffset() is None):
            raise ValueError("blocked archive scan requires its exact discrepancy")


@dataclass(frozen=True)
class BlockedReviewGenerationArchiveSweep:
    prior: ReviewGenerationArchiveSweep | CompletedReviewGenerationArchiveAudit
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (ReviewGenerationArchiveSweep, CompletedReviewGenerationArchiveAudit))
                or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or self.source_rowid is not None and (type(self.source_rowid) is not int
                    or not -_MAX_INTEGER - 1 <= self.source_rowid <= _MAX_INTEGER)
                or not isinstance(self.detected_at, datetime)
                or self.detected_at.tzinfo is None or self.detected_at.utcoffset() is None):
            raise ValueError("blocked archive sweep requires its exact discrepancy")


@dataclass(frozen=True)
class ReviewGenerationArchiveCursor:
    state: (
        ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan
        | ReviewGenerationArchiveSweep | CompletedReviewGenerationArchiveAudit
        | BlockedReviewGenerationArchiveScan | BlockedReviewGenerationArchiveSweep
    )
    generation_sources_version: int
    prior_baseline: ReviewGenerationArchiveBaseline | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.state, (
            ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan,
            ReviewGenerationArchiveSweep, CompletedReviewGenerationArchiveAudit,
            BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep,
        )) or type(self.generation_sources_version) is not int
                or not 0 <= self.generation_sources_version < _MAX_INTEGER
                or self.prior_baseline is not None
                and not isinstance(self.prior_baseline, ReviewGenerationArchiveBaseline)):
            raise ValueError("generation archive cursor requires a bounded source revision")


def review_generation_archive_scan_overdue(
    state: (
        ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan
        | ReviewGenerationArchiveSweep | CompletedReviewGenerationArchiveAudit
        | BlockedReviewGenerationArchiveScan | BlockedReviewGenerationArchiveSweep
    ),
    now: datetime,
) -> bool:
    """A source discrepancy or missed pass deadline must block future admission."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("archive scan due check requires an aware time")
    if isinstance(state, (BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep)):
        return True
    if isinstance(state, ReviewGenerationArchiveScan):
        return now >= state.due_at
    if isinstance(state, CompletedReviewGenerationArchiveScan):
        return now >= state.due_at
    if isinstance(state, ReviewGenerationArchiveSweep):
        return now >= state.generation_scan.due_at
    if isinstance(state, CompletedReviewGenerationArchiveAudit):
        return now >= state.next_due_at
    raise ValueError("archive scan due check requires a typed state")


def begin_review_generation_archive_scan(
    origin: ReviewExecutionBinding, origin_receipt: ReviewExecutionOrigin, origin_source_sha256: str,
    through_generation: int,
    now: datetime, due_seconds: int,
    baseline: ReviewGenerationArchiveBaseline | None = None,
) -> ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan:
    """Start a bounded pass from the same episode's first expected generation."""
    if (not isinstance(origin, ReviewExecutionBinding)
            or not isinstance(origin_receipt, ReviewExecutionOrigin)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("archive scan requires an aware start and typed origin")
    _archive_audit_digest(origin_source_sha256, "archive origin source digest")
    integer(through_generation, "archive scan frontier", minimum=origin.rejection.fence.generation)
    if baseline is not None:
        if (not isinstance(baseline, ReviewGenerationArchiveBaseline)
                or not origin.rejection.fence.generation <= baseline.through_generation <= through_generation):
            raise ValueError("archive scan baseline exceeds its expected frontier")
        if baseline.through_generation == origin.rejection.fence.generation:
            if baseline.rolling_sha256 != _EMPTY_ARCHIVE_AUDIT_DIGEST:
                raise ValueError("empty archive baseline must retain its empty digest")
            baseline = None
    integer(due_seconds, "archive scan due seconds", minimum=1)
    if due_seconds > MAX_ARCHIVE_AUDIT_DUE_SECONDS:
        raise ValueError("archive scan due window exceeds one day")
    try:
        due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("archive scan due window exceeds representable time") from exc
    if through_generation == origin.rejection.fence.generation:
        return CompletedReviewGenerationArchiveScan(
            origin, origin_receipt, origin_source_sha256, through_generation, None,
            _EMPTY_ARCHIVE_AUDIT_DIGEST, _EMPTY_ARCHIVE_RECEIPT_SET, now, now, due,
        )
    return ReviewGenerationArchiveScan(
        origin, origin_receipt, origin_source_sha256, through_generation,
        origin.rejection.fence.generation + 1,
        None, _EMPTY_ARCHIVE_AUDIT_DIGEST, _EMPTY_ARCHIVE_RECEIPT_SET,
        baseline, now, due,
    )


def advance_review_generation_archive_scan(
    scan: ReviewGenerationArchiveScan, sources: tuple[ReviewGenerationArchiveFingerprint, ...],
    now: datetime,
) -> ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan | BlockedReviewGenerationArchiveScan:
    """Fold one verified page in generation order without retaining its payloads."""
    if not isinstance(scan, ReviewGenerationArchiveScan) or not isinstance(sources, tuple) or not 1 <= len(sources) <= MAX_ARCHIVE_AUDIT_STEP:
        raise ValueError("archive scan advance requires a bounded verified page")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None or now < scan.started_at:
        raise ValueError("archive scan advance requires an aware later time")
    digest = scan.rolling_sha256
    receipt_set = int(scan.receipt_membership_xor, 16)
    baseline = scan.baseline
    for index, source in enumerate(sources):
        generation = scan.next_generation + index
        if (not isinstance(source, ReviewGenerationArchiveFingerprint)
                or generation > scan.through_generation
                or source.cursor.revision.fence.generation != generation
                or source.cursor.revision.fence.key != scan.origin.rejection.fence.key
                or source.cursor.receipt_id != review_generation_receipt_id(scan.origin, generation)):
            raise ValueError("archive scan page disagrees with its expected generation")
        encoded = "\0".join((str(generation), source.cursor.receipt_id,
                             str(source.cursor.revision.version), source.receipt_sha256,
                             source.accepted_namespace, source.accepted_key,
                             source.accepted_sha256)).encode()
        digest = hashlib.sha256(bytes.fromhex(digest) + encoded).hexdigest()
        receipt_set ^= _archive_receipt_member(
            generation, source.cursor.receipt_id, source.receipt_sha256,
            source.accepted_namespace, source.accepted_key,
        )
        if baseline is not None and generation == baseline.through_generation:
            if digest != baseline.rolling_sha256:
                return BlockedReviewGenerationArchiveScan(
                    scan, generation, "archived generation source changed since its prior pass", now,
                )
            baseline = None
    final = sources[-1].cursor
    next_generation = scan.next_generation + len(sources)
    if next_generation <= scan.through_generation:
        return ReviewGenerationArchiveScan(
            scan.origin, scan.origin_receipt, scan.origin_source_sha256,
            scan.through_generation, next_generation, final,
            digest, f"{receipt_set:064x}", baseline, scan.started_at, scan.due_at,
        )
    return CompletedReviewGenerationArchiveScan(
        scan.origin, scan.origin_receipt, scan.origin_source_sha256,
        scan.through_generation, final, digest, f"{receipt_set:064x}",
        scan.started_at, now, scan.due_at,
    )


def advance_review_generation_archive_sweep(
    sweep: ReviewGenerationArchiveSweep,
    sources: tuple[ReviewGenerationArchiveReceiptMember, ...],
    after: ReviewGenerationReceiptSweepCursor | None,
    upper: ReviewGenerationReceiptSweepCursor | None,
    has_more: bool,
    now: datetime,
    due_seconds: int,
) -> ReviewGenerationArchiveSweep | CompletedReviewGenerationArchiveAudit | BlockedReviewGenerationArchiveSweep:
    """Fold a bounded namespace page and reconcile its retained receipt set."""
    if (not isinstance(sweep, ReviewGenerationArchiveSweep) or not isinstance(sources, tuple)
            or len(sources) > MAX_ARCHIVE_AUDIT_STEP
            or type(has_more) is not bool
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None or now < sweep.generation_scan.completed_at):
        raise ValueError("archive sweep advance requires a bounded page and aware later time")
    integer(due_seconds, "archive sweep due seconds", minimum=1)
    if due_seconds > MAX_ARCHIVE_AUDIT_DUE_SECONDS:
        raise ValueError("archive sweep due window exceeds one day")
    if (upper is not None and not isinstance(upper, ReviewGenerationReceiptSweepCursor)
            or after is not None and not isinstance(after, ReviewGenerationReceiptSweepCursor)
            or sweep.upper is not None and upper != sweep.upper
            or upper is None and (after is not None or sources or has_more)
            or upper is not None and after is None
            or sweep.after is not None and (after is None or
                (after.scope, after.key) < (sweep.after.scope, sweep.after.key)
                or (has_more or sources) and after == sweep.after)
            or has_more and after is None):
        raise ValueError("archive sweep page did not advance its fixed namespace frontier")
    origin = sweep.generation_scan.origin
    base = origin.rejection.fence.generation
    through = sweep.generation_scan.through_generation
    seen: set[int] = set()
    receipt_set = int(sweep.receipt_membership_xor, 16)
    for source in sources:
        if (not isinstance(source, ReviewGenerationArchiveReceiptMember)
                or not base < source.generation <= through
                or source.generation in seen
                or source.receipt_id != review_generation_receipt_id(origin, source.generation)):
            raise ValueError("archive sweep page disagrees with expected episode receipts")
        seen.add(source.generation)
        receipt_set ^= _archive_receipt_member(
            source.generation, source.receipt_id, source.receipt_sha256,
            source.accepted_namespace, source.accepted_key,
        )
    if sweep.source_count + len(sources) > through - base:
        return BlockedReviewGenerationArchiveSweep(
            sweep, "generation receipt census contains extra episode sources", None, now,
        )
    advanced = ReviewGenerationArchiveSweep(
        sweep.generation_scan, after, upper, f"{receipt_set:064x}",
        sweep.source_count + len(sources),
    )
    if has_more:
        return advanced
    if (advanced.source_count != through - base
            or advanced.receipt_membership_xor != sweep.generation_scan.receipt_membership_xor):
        return BlockedReviewGenerationArchiveSweep(
            advanced, "generation receipt census disagrees with expected source pairs", None, now,
        )
    try:
        next_due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("archive sweep due window exceeds representable time") from exc
    return CompletedReviewGenerationArchiveAudit(
        sweep.generation_scan, upper, advanced.receipt_membership_xor,
        advanced.source_count, now, next_due,
    )


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
    IMPORTED = "imported"
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
class ImportedReviewExecutionOriginReceipt:
    """Immutable activation ancestry for an imported sequence-zero checkpoint."""

    origin: ReviewExecutionOrigin
    checkpoint: ReviewExecutionCheckpoint
    activation_plan_digest: str
    source_digest: str
    inventory_digest: str
    import_decision_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or self.origin.kind is not ReviewExecutionOriginKind.IMPORTED):
            raise ValueError("imported execution origin requires an imported cause")
        if (not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or self.checkpoint.through_sequence != 0
                or self.checkpoint.boundary_cursor is not None):
            raise ValueError("imported execution origin requires its initial checkpoint")
        for name in ("activation_plan_digest", "source_digest", "inventory_digest", "import_decision_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"imported execution origin {name} requires a SHA-256 digest")


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
class ImportedReviewExecutionDecision:
    """The activation plan's selected imported execution starting state."""

    origin: ReviewExecutionOrigin
    checkpoint: ReviewExecutionCheckpoint
    activation_plan_digest: str
    source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or self.origin.kind is not ReviewExecutionOriginKind.IMPORTED
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or self.checkpoint.through_sequence != 0
                or self.checkpoint.boundary_cursor is not None):
            raise ValueError("imported execution decision requires its initial origin and checkpoint")
        for name in ("activation_plan_digest", "source_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"imported execution decision {name} requires a SHA-256 digest")


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
        if isinstance(self.state.session, ReusableReviewSession) and isinstance(self.state.session.source, ImportedReviewSession):
            if self.origin.kind is not ReviewExecutionOriginKind.IMPORTED:
                raise ValueError("an imported session requires an imported execution origin")
        if self.last_sequence == 0 and self.origin.kind is not ReviewExecutionOriginKind.IMPORTED:
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
                    and isinstance(state.session.source, ImportedReviewSession)
                    and self.origin.kind is not ReviewExecutionOriginKind.IMPORTED):
                raise ValueError("an imported session requires an imported execution origin")
        if (self.last_sequence == 0 and self.origin.kind is not ReviewExecutionOriginKind.IMPORTED
                and (self.state.failures or self.state.pin_deferrals
                     or not isinstance(self.state.session, NoReviewSession))):
            raise ValueError("a fresh execution origin cannot invent prior allowance or session")

    @property
    def binding(self) -> ReviewExecutionBinding:
        return self.state.binding


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


@dataclass(frozen=True)
class ReviewExecutionCompactionPlan:
    checkpoint: ReviewExecutionCheckpoint
    retired_boundaries: tuple[ReviewExecutionGenerationBoundary, ...]
    retired_outcomes: tuple[ReviewRoundOutcome, ...]
    remaining_boundaries: tuple[ReviewExecutionGenerationBoundary, ...]
    remaining_outcomes: tuple[ReviewRoundOutcome, ...]

    @property
    def last_boundary_receipt(self) -> str | None:
        cursor = self.checkpoint.boundary_cursor
        return cursor.receipt_id if cursor is not None else None

    @property
    def last_boundary_revision(self) -> WorkflowRevision | None:
        cursor = self.checkpoint.boundary_cursor
        return cursor.revision if cursor is not None else None


@dataclass(frozen=True)
class ReviewExecutionCompactionRoundSource:
    """Small identity of one verified retired round row and its receipt set."""

    round: ReviewRoundIdentity
    row_sha256: str
    receipt_set_sha256: str
    lineage_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity):
            raise ValueError("compaction round source requires a typed round")
        _archive_audit_digest(self.row_sha256, "compaction round row digest")
        _archive_audit_digest(self.receipt_set_sha256, "compaction round receipt digest")
        if self.lineage_sha256 is not None:
            _archive_audit_digest(self.lineage_sha256, "compaction round lineage digest")


@dataclass(frozen=True)
class ReviewExecutionCheckpointReceipt:
    """Fixed-size checkpoint chain and retired source manifest, not a minted SQL proof."""

    origin: ReviewExecutionOrigin
    previous_checkpoint: ReviewExecutionCheckpoint
    previous_receipt_id: str | None
    checkpoint: ReviewExecutionCheckpoint
    round_stream_sha256: str
    generation_stream_sha256: str
    last_round_source: ReviewExecutionCompactionRoundSource | None
    last_generation_source: ReviewGenerationArchiveFingerprint | None

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or not isinstance(self.previous_checkpoint, ReviewExecutionCheckpoint)
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)):
            raise ValueError("checkpoint receipt requires its origin and two typed frontiers")
        initial = (self.previous_checkpoint.through_sequence == 0
                   and self.previous_checkpoint.boundary_cursor is None)
        if (self.previous_receipt_id is None) != initial:
            raise ValueError("checkpoint receipt must name its previous checkpoint or initial origin")
        if self.previous_receipt_id is not None:
            identifier(self.previous_receipt_id, "previous execution checkpoint receipt")
        if (not same_execution_episode(self.previous_checkpoint.state.binding,
                                       self.checkpoint.state.binding)
                or self.previous_checkpoint.policy != self.checkpoint.policy):
            raise ValueError("checkpoint receipt changed its execution episode or policy")
        previous_sequence = self.previous_checkpoint.through_sequence
        next_sequence = self.checkpoint.through_sequence
        previous_generation = self.previous_checkpoint.state.binding.rejection.fence.generation
        next_generation = self.checkpoint.state.binding.rejection.fence.generation
        rounds = next_sequence - previous_sequence
        generations = next_generation - previous_generation
        if (rounds < 0 or generations < 0 or rounds > MAX_EXECUTION_SUFFIX
                or generations > MAX_EXECUTION_SUFFIX or (rounds == 0 and generations == 0)):
            raise ValueError("checkpoint receipt requires a bounded advancing frontier")
        _archive_audit_digest(self.round_stream_sha256, "checkpoint round source stream")
        _archive_audit_digest(self.generation_stream_sha256, "checkpoint generation source stream")
        if (rounds == 0) != (self.last_round_source is None):
            raise ValueError("checkpoint receipt round anchor disagrees with its frontier")
        if rounds == 0 and self.round_stream_sha256 != _EMPTY_ARCHIVE_AUDIT_DIGEST:
            raise ValueError("checkpoint receipt cannot invent retired round sources")
        if self.last_round_source is not None and (
            not isinstance(self.last_round_source, ReviewExecutionCompactionRoundSource)
            or self.last_round_source.round.sequence != next_sequence
            or not same_execution_episode(self.previous_checkpoint.state.binding,
                                          self.last_round_source.round.binding)
            or self.last_round_source.round.binding.rejection.fence.generation > next_generation
        ):
            raise ValueError("checkpoint receipt has a foreign final round source")
        if (generations == 0) != (self.last_generation_source is None):
            raise ValueError("checkpoint receipt generation anchor disagrees with its frontier")
        if generations == 0 and (
            self.generation_stream_sha256 != _EMPTY_ARCHIVE_AUDIT_DIGEST
            or self.checkpoint.boundary_cursor != self.previous_checkpoint.boundary_cursor
        ):
            raise ValueError("checkpoint receipt cannot invent retired generation sources")
        if self.last_generation_source is not None and (
            not isinstance(self.last_generation_source, ReviewGenerationArchiveFingerprint)
            or self.last_generation_source.cursor != self.checkpoint.boundary_cursor
            or self.last_generation_source.cursor.revision.fence.generation != next_generation
        ):
            raise ValueError("checkpoint receipt has a foreign final generation source")


def build_review_execution_checkpoint_receipt(
    execution: ActivatedReviewExecution,
    plan: ReviewExecutionCompactionPlan,
    round_sources: tuple[ReviewExecutionCompactionRoundSource, ...],
    generation_sources: tuple[ReviewGenerationArchiveFingerprint, ...],
) -> ReviewExecutionCheckpointReceipt:
    """Bind a validated prefix plan to ordered source fingerprints supplied by an adapter."""
    if (not isinstance(execution, ActivatedReviewExecution)
            or not isinstance(plan, ReviewExecutionCompactionPlan)
            or not isinstance(round_sources, tuple)
            or not isinstance(generation_sources, tuple)
            or len(round_sources) > MAX_EXECUTION_SUFFIX
            or len(generation_sources) > MAX_EXECUTION_SUFFIX):
        raise ValueError("checkpoint receipt builder requires bounded typed plan and sources")
    if (plan_review_execution_compaction(
        execution, plan.retired_outcomes + plan.remaining_outcomes,
        plan.checkpoint.through_sequence,
    ) != plan or len(round_sources) != len(plan.retired_outcomes)
            or len(generation_sources) != len(plan.retired_boundaries)):
        raise ValueError("checkpoint receipt sources disagree with the projected prefix")
    round_digest = _EMPTY_ARCHIVE_AUDIT_DIGEST
    for source, outcome in zip(round_sources, plan.retired_outcomes, strict=True):
        if (not isinstance(source, ReviewExecutionCompactionRoundSource)
                or source.round != outcome.round):
            raise ValueError("checkpoint receipt has an unverified round source order")
        parts = (str(source.round.sequence), source.round.round_id,
                 str(source.round.binding.rejection.fence.generation),
                 source.row_sha256, source.receipt_set_sha256)
        if source.lineage_sha256 is not None:
            parts += (source.lineage_sha256,)
        encoded = "\0".join(parts).encode()
        round_digest = hashlib.sha256(bytes.fromhex(round_digest) + encoded).hexdigest()
    generation_digest = _EMPTY_ARCHIVE_AUDIT_DIGEST
    for source, boundary in zip(generation_sources, plan.retired_boundaries, strict=True):
        if (not isinstance(source, ReviewGenerationArchiveFingerprint)
                or source.cursor != ReviewExecutionBoundaryCursor(
                    boundary.receipt_id, boundary.to_revision,
                )):
            raise ValueError("checkpoint receipt has an unverified generation source order")
        encoded = "\0".join((str(boundary.to_revision.fence.generation),
                             source.cursor.receipt_id, str(source.cursor.revision.version),
                             source.receipt_sha256, source.accepted_namespace,
                             source.accepted_key, source.accepted_sha256)).encode()
        generation_digest = hashlib.sha256(bytes.fromhex(generation_digest) + encoded).hexdigest()
    return ReviewExecutionCheckpointReceipt(
        execution.origin, execution.checkpoint, execution.checkpoint_receipt,
        plan.checkpoint, round_digest, generation_digest,
        round_sources[-1] if round_sources else None,
        generation_sources[-1] if generation_sources else None,
    )


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


def accepted_review_transition_source(
    boundary: ReviewExecutionGenerationBoundary, namespace: str, key: str, payload: str,
) -> ReviewExecutionAcceptedTransitionSource:
    """Bind one canonical accepted payload without copying its growing snapshot."""
    if not isinstance(payload, str):
        raise ValueError("accepted generation transition payload must be text")
    return ReviewExecutionAcceptedTransitionSource(
        boundary, namespace, key, hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def accepted_review_transition_proof_source(
    boundary: ReviewExecutionGenerationBoundary, namespace: str, key: str,
    payload: str, audit: AuthorizedOperatorCommand | ObservedTransition, audit_payload: str,
) -> ReviewExecutionTransitionProofSource:
    """Bind the accepted bytes and typed cause without copying their snapshot."""
    if not isinstance(payload, str) or not isinstance(audit_payload, str):
        raise ValueError("generation transition proof payloads must be text")
    command_type = {
        WorkflowMutation.REQUEST_REARM: RequestRearm,
        WorkflowMutation.CONTINUE_REARM: ContinueGrantedRearm,
    }.get(boundary.mutation)
    if command_type is not None:
        if (not isinstance(audit, AuthorizedOperatorCommand)
                or not isinstance(audit.command, command_type)
                or audit.command.request_id != key
                or audit.command.expected != boundary.from_revision):
            raise ValueError("generation request proof disagrees with its boundary")
    elif (boundary.mutation is not WorkflowMutation.RELEASE_CEILING
          or not isinstance(audit, ObservedTransition)):
        raise ValueError("generation release proof disagrees with its boundary")
    return ReviewExecutionTransitionProofSource(
        boundary, namespace, key, hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        hashlib.sha256(audit_payload.encode("utf-8")).hexdigest(),
    )


def matches_accepted_review_transition_source(
    captured: object, boundary: ReviewExecutionGenerationBoundary,
    namespace: str, key: str, payload: str,
) -> bool:
    """Revalidate new digest receipts and retained legacy full-payload receipts."""
    if isinstance(captured, ReviewExecutionAcceptedTransitionSource):
        try:
            return captured == accepted_review_transition_source(boundary, namespace, key, payload)
        except (TypeError, ValueError, UnicodeError):
            return False
    if isinstance(captured, ReviewExecutionTransitionProofSource):
        try:
            return (captured.boundary == boundary and captured.namespace == namespace
                    and captured.key == key
                    and captured.payload_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest())
        except (TypeError, ValueError, UnicodeError):
            return False
    return isinstance(captured, tuple) and captured == (boundary, namespace, key, payload)


def review_generation_chain_digest(receipt_ids: tuple[str, ...]) -> str:
    """Bind an ordered, bounded crossing without storing a lifetime-sized list."""
    if not isinstance(receipt_ids, tuple) or not 1 <= len(receipt_ids) <= MAX_EXECUTION_SUFFIX:
        raise ValueError("generation crossing requires bounded receipts")
    for receipt_id in receipt_ids:
        identifier(receipt_id, "generation crossing receipt")
        try:
            receipt_id.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("generation crossing receipt must be UTF-8") from exc
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValueError("generation crossing requires distinct receipts")
    return hashlib.sha256("\x00".join(receipt_ids).encode()).hexdigest()


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
        pins = min(pins + 1, _MAX_INTEGER)
        if pins > policy.free_pin_deferrals:
            failures = min(failures + 1, _MAX_INTEGER)
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
    if (not isinstance(execution, CurrentReviewExecution)
            or not isinstance(from_revision, WorkflowRevision)
            or not isinstance(to_revision, WorkflowRevision)
            or from_revision.fence != execution.binding.rejection.fence):
        raise ValueError("generation advance requires current authority and its exact revision")
    validated = reduce_current_review_execution(execution, pending)
    if validated.execution.state != execution.state:
        raise ValueError("current execution state disagrees with its pending projection")
    binding = replace(execution.binding, rejection=replace(execution.binding.rejection, fence=to_revision.fence))
    after = carry_review_execution_generation(execution.state, binding, execution.last_sequence)
    boundary = ReviewExecutionGenerationBoundary(
        execution.state, after, execution.last_sequence,
        tuple(item.round.sequence for item in sorted(pending, key=lambda item: item.round.sequence)
              if isinstance(item, CompletedReviewExecutionRound)),
        review_generation_receipt_id(execution.binding, to_revision.fence.generation),
        from_revision, to_revision, mutation,
    )
    return CurrentReviewExecution(
        execution.origin, ReviewExecutionCheckpoint(execution.last_sequence, after, execution.baseline.policy),
        after, execution.last_sequence, execution.last_sequence,
    ), boundary


def replay_review_execution_generations(
    checkpoint: ReviewExecutionCheckpoint,
    outcomes: tuple[ReviewRoundOutcome, ...],
    boundaries: tuple[ReviewExecutionGenerationBoundary, ...],
) -> ReviewExecutionState:
    """Replay captured pre-advance outcomes; late older outcomes remain audit."""
    if not isinstance(checkpoint, ReviewExecutionCheckpoint):
        raise ValueError("generation replay requires a checkpoint")
    if not isinstance(outcomes, tuple) or len(outcomes) > MAX_EXECUTION_SUFFIX:
        raise ValueError("generation replay requires a bounded outcome tuple")
    if not isinstance(boundaries, tuple) or len(boundaries) > MAX_EXECUTION_SUFFIX:
        raise ValueError("generation replay requires bounded boundaries")
    if not boundaries:
        return project_review_execution(checkpoint, outcomes).state
    ordered: dict[int, ReviewRoundOutcome] = {}
    identities: dict[str, ReviewRoundIdentity] = {}
    for outcome in outcomes:
        if not isinstance(outcome, ReviewRoundOutcome):
            raise ValueError("generation replay contains an invalid outcome")
        identity = outcome.round
        if (identity.sequence <= checkpoint.through_sequence
                or not same_execution_episode(checkpoint.state.binding, identity.binding)):
            raise ValueError("generation outcome belongs to a compacted or foreign execution")
        if identity.sequence in ordered and ordered[identity.sequence] != outcome:
            raise ValueError("conflicting outcomes for one review round sequence")
        if identity.round_id in identities and identities[identity.round_id] != identity:
            raise ValueError("one review round cannot have multiple identities")
        ordered[identity.sequence] = outcome
        identities[identity.round_id] = identity
    state, cutoff = checkpoint.state, checkpoint.through_sequence
    by_sequence = sorted(ordered.items())
    cursor = 0
    previous_revision = checkpoint.boundary_cursor.revision if checkpoint.boundary_cursor is not None else None
    for index, boundary in enumerate(boundaries):
        if (not isinstance(boundary, ReviewExecutionGenerationBoundary)
                or boundary.before.binding != state.binding
                or boundary.through_sequence < cutoff
                or (previous_revision is not None
                    and boundary.from_revision.version < previous_revision.version)):
            raise ValueError("generation boundaries must advance one execution history")
        while cursor < len(by_sequence) and by_sequence[cursor][0] <= boundary.through_sequence:
            if by_sequence[cursor][1].round.binding != state.binding:
                raise ValueError("round allocation disagrees with its generation boundary")
            cursor += 1
        for sequence in boundary.applied_sequences:
            if index == 0 and sequence <= checkpoint.through_sequence:
                continue
            outcome = ordered.get(sequence)
            if (sequence <= cutoff or outcome is None
                    or outcome.round.binding != state.binding):
                raise ValueError("generation boundary lacks an applicable finalized outcome")
            state = _apply(state, outcome, checkpoint.policy)
        if state != boundary.before:
            raise ValueError("generation boundary disagrees with its prior outcome projection")
        state, cutoff, previous_revision = boundary.after, boundary.through_sequence, boundary.to_revision
    for sequence, outcome in by_sequence[cursor:]:
        if outcome.round.binding != state.binding:
            raise ValueError("generation replay contains an outcome beyond its binding boundary")
        state = _apply(state, outcome, checkpoint.policy)
    return state


def apply_review_execution_replay_outcome(
    state: ReviewExecutionState,
    outcome: ReviewRoundOutcome,
    policy: ReviewExecutionPolicy,
) -> ReviewExecutionState:
    """Apply one source-verified outcome at its exact generation binding."""
    if (not isinstance(state, ReviewExecutionState)
            or not isinstance(outcome, ReviewRoundOutcome)
            or not isinstance(policy, ReviewExecutionPolicy)
            or outcome.round.binding != state.binding):
        raise ValueError("replay outcome disagrees with its current generation")
    return _apply(state, outcome, policy)


def advance_review_execution_generation(
    execution: ActivatedReviewExecution, from_revision: WorkflowRevision,
    to_revision: WorkflowRevision, mutation: WorkflowMutation,
    applied_sequences: tuple[int, ...],
) -> ActivatedReviewExecution:
    """Bind a rejection advance to a source-captured execution boundary."""
    if not isinstance(execution, ActivatedReviewExecution) or not isinstance(from_revision, WorkflowRevision):
        raise ValueError("generation advance requires active execution and its captured revision")
    if (execution.binding.rejection.fence != from_revision.fence
            or not isinstance(to_revision, WorkflowRevision)):
        raise ValueError("generation advance disagrees with the active rejection revision")
    lower = (execution.boundaries[-1].through_sequence if execution.boundaries
             else execution.checkpoint.through_sequence)
    if (not isinstance(applied_sequences, tuple)
            or any(type(sequence) is not int or sequence <= lower or sequence > execution.last_sequence
                   for sequence in applied_sequences)):
        raise ValueError("generation advance requires captured finalized suffix outcomes")
    next_binding = replace(
        execution.binding, rejection=replace(execution.binding.rejection, fence=to_revision.fence),
    )
    after = carry_review_execution_generation(execution.state, next_binding, execution.last_sequence)
    receipt_id = review_generation_receipt_id(execution.binding, to_revision.fence.generation)
    boundary = ReviewExecutionGenerationBoundary(
        execution.state, after, execution.last_sequence, applied_sequences,
        receipt_id, from_revision, to_revision, mutation,
    )
    return replace(execution, state=after, boundaries=execution.boundaries + (boundary,))


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


def review_generation_within_frontier(
    origin: ReviewExecutionBinding, through_generation: int,
    boundary: ReviewExecutionGenerationBoundary,
) -> bool:
    """Classify one swept receipt against an episode's expected generation range."""
    if not isinstance(origin, ReviewExecutionBinding) or not isinstance(boundary, ReviewExecutionGenerationBoundary):
        raise ValueError("generation frontier requires a typed episode and boundary")
    integer(through_generation, "generation frontier", minimum=origin.rejection.fence.generation)
    return (same_execution_episode(origin, boundary.before.binding)
            and origin.rejection.fence.generation < boundary.to_revision.fence.generation <= through_generation)


def plan_review_execution_compaction(
    execution: ActivatedReviewExecution, outcomes: tuple[ReviewRoundOutcome, ...],
    through_sequence: int,
) -> ReviewExecutionCompactionPlan:
    """Find a finalized frontier whose retired boundaries preserve full replay."""
    if not isinstance(execution, ActivatedReviewExecution):
        raise ValueError("execution compaction requires an active episode")
    integer(through_sequence, "execution compaction sequence")
    if through_sequence < execution.checkpoint.through_sequence or through_sequence > execution.last_sequence:
        raise ValueError("execution compaction frontier exceeds allocated rounds")
    if not isinstance(outcomes, tuple) or len(outcomes) > MAX_EXECUTION_SUFFIX:
        raise ValueError("execution compaction requires a bounded outcome suffix")
    if any(not isinstance(outcome, ReviewRoundOutcome) for outcome in outcomes):
        raise ValueError("execution compaction contains an invalid outcome")
    sequences = tuple(outcome.round.sequence for outcome in outcomes)
    if (any(sequence <= execution.checkpoint.through_sequence or sequence > execution.last_sequence
            for sequence in sequences) or len(set(sequences)) != len(sequences)):
        raise ValueError("execution compaction requires exact allocated outcome identities")
    prefix = tuple(sorted(
        (outcome for outcome in outcomes if outcome.round.sequence <= through_sequence),
        key=lambda outcome: outcome.round.sequence,
    ))
    expected = through_sequence - execution.checkpoint.through_sequence
    if expected > len(outcomes) or expected > MAX_EXECUTION_SUFFIX or (
        tuple(sorted(outcome.round.sequence for outcome in prefix))
        != tuple(range(execution.checkpoint.through_sequence + 1, through_sequence + 1))
    ):
        raise ValueError("execution compaction cannot cross an open or missing round")
    retired = tuple(boundary for boundary in execution.boundaries if boundary.through_sequence <= through_sequence)
    remaining_boundaries = execution.boundaries[len(retired):]
    if not retired and through_sequence == execution.checkpoint.through_sequence:
        raise ValueError("execution compaction requires a new finalized frontier")
    if remaining_boundaries:
        cutoff = retired[-1].through_sequence if retired else execution.checkpoint.through_sequence
        applied = frozenset(remaining_boundaries[0].applied_sequences)
        projected_prefix = tuple(
            outcome for outcome in prefix
            if outcome.round.sequence <= cutoff or outcome.round.sequence in applied
        )
    else:
        projected_prefix = prefix
    state = replay_review_execution_generations(execution.checkpoint, projected_prefix, retired)
    cursor = (
        ReviewExecutionBoundaryCursor(retired[-1].receipt_id, retired[-1].to_revision)
        if retired else execution.checkpoint.boundary_cursor
    )
    checkpoint = ReviewExecutionCheckpoint(through_sequence, state, execution.checkpoint.policy, cursor)
    remaining = tuple(sorted(
        (outcome for outcome in outcomes if outcome.round.sequence > through_sequence),
        key=lambda outcome: outcome.round.sequence,
    ))
    if (replay_review_execution_generations(execution.checkpoint, outcomes, execution.boundaries)
            != execution.state
            or replay_review_execution_generations(checkpoint, remaining, remaining_boundaries)
            != execution.state):
        raise ValueError("execution compaction changed the active projection")
    return ReviewExecutionCompactionPlan(checkpoint, retired, prefix, remaining_boundaries, remaining)
