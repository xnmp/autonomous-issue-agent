"""Ordered review execution evidence; phase-4 continuation construction.

Adapters must verify the named admission, launch and effect receipts before
constructing these facts. Model output cannot mint them. This module supplies
no runtime authority until the execution family is explicitly activated.
"""

from dataclasses import dataclass, replace
from enum import Enum

from .agent_task import RuntimeCapabilities, RuntimeName
from .workflow_identity import (
    PullRequestRef, WorkflowBinding, WorkflowFamily,
    validate_workflow_identifier as identifier,
    validate_workflow_integer as integer,
)

MAX_EXECUTION_SUFFIX = 4096
MAX_ROUND_CHILDREN = 256
_MAX_INTEGER = 2**63 - 1
_LEGACY_SESSION_MARKERS = frozenset({"multi", "github-review", "codex-review"})


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
        identifier(self.handle, "review session handle")
        if self.handle in _LEGACY_SESSION_MARKERS:
            raise ValueError("legacy mode markers are not reusable session handles")
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
    PaidReviewProgress | CopiedReviewProgress | ReviewRoundFailure | ReviewPinDeferred
    | ReviewAdmissionRefused | ReviewReservationReleased | ReviewRoundCancelled
)
_DISPOSITIONS = (
    PaidReviewProgress, CopiedReviewProgress, ReviewRoundFailure, ReviewPinDeferred,
    ReviewAdmissionRefused, ReviewReservationReleased, ReviewRoundCancelled,
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
        paid = isinstance(self.disposition, (PaidReviewProgress, ReviewRoundFailure))
        if paid:
            if any(value.child.round != self.round for value in self.disposition.launched):
                raise ValueError("outcome launch evidence belongs to another round")
        elif not isinstance(self.session, PreserveReviewSession):
            raise ValueError("an unpaid outcome preserves the reviewer session")
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
class ReviewExecutionCheckpoint:
    through_sequence: int
    state: ReviewExecutionState
    policy: ReviewExecutionPolicy = ReviewExecutionPolicy()

    def __post_init__(self) -> None:
        integer(self.through_sequence, "execution checkpoint sequence")
        if not isinstance(self.state, ReviewExecutionState):
            raise ValueError("execution checkpoint requires a validated projection")
        if not isinstance(self.policy, ReviewExecutionPolicy):
            raise ValueError("execution checkpoint requires its captured episode policy")
        if max(self.state.session_sequence, self.state.display_sequence, self.state.productive_sequence) > self.through_sequence:
            raise ValueError("execution checkpoint cannot contain a future outcome")


@dataclass(frozen=True)
class ReviewExecutionProjection:
    state: ReviewExecutionState
    checkpoint: ReviewExecutionCheckpoint
    latest_sequence: int


def _same_episode(current: ReviewExecutionBinding, observed: ReviewExecutionBinding) -> bool:
    return (
        current.execution_epoch == observed.execution_epoch
        and current.execution_episode == observed.execution_episode
        and current.rejection.authority == observed.rejection.authority
        and current.rejection.episode_id == observed.rejection.episode_id
        and current.rejection.fence.key == observed.rejection.fence.key
    )


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
        if not _same_episode(checkpoint.state.binding, identity.binding):
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
            prefix = ReviewExecutionCheckpoint(sequence, state, checkpoint.policy)
    return ReviewExecutionProjection(state, prefix, max(ordered, default=checkpoint.through_sequence))
