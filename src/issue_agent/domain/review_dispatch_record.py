"""The review daemon's recorded dispatch decision for one pull request (ADR 0015 operator surface).

Provenance: harness-owned. Only the review role's plan tick writes it
(`ActiveReviewRuntime.record_plan`, through the review owner's workflow control),
from the review daemon's own GitHub read of the pull request. The operator
transport and the dashboard only read it, and the operator view renders it
with its as-of time instead of deciding anything itself. A recorded
decision can be old, and says how old; it is never a second opinion.

Values and functions perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum

from .labels import Label
from .review_rejection import PullRequestState, validate_commit_sha, validate_labels
from .review_workflow_policy import (
    ReleaseProjectionPending, ReviewDispatch, ReviewHeld, ReviewKind, ReviewNotOwed, ReviewOwed, SubmissionUnresolved,
)
from .workflow_identity import (
    MAX_DETAIL_CHARS, MAX_WORKFLOW_GATES, PullRequestRef, WorkflowKey, validate_workflow_identifier,
    validate_workflow_text,
)

# An unchanged decision refreshes its as-of time at most this often, so the
# view's age is honest to within this bound without a write every tick.
DECISION_CONFIRM_SECONDS = 300
# A pull request that left the daemon's review read is read on its own at
# once, then at most this often while its own read leaves it undecided. A
# closed one that still holds workflow gates keeps this cadence for
# CLOSED_PROMPT_SECONDS after the recorded close, so a prompt reopen finds its
# lever; its commands stay offered and acceptance re-reads GitHub anyway.
DEPARTED_CONFIRM_SECONDS = 900
CLOSED_PROMPT_SECONDS = 86400
# A closed pull request without workflow holds, or closed for longer than the
# prompt window, can still be reopened; a merged one cannot.
CLOSED_RECONFIRM_SECONDS = 21600
# Consecutive failed departed reads (lost access, a deleted repository) back
# off exponentially from DEPARTED_CONFIRM_SECONDS to this ceiling (ADR 0012).
MAX_DEPARTED_BACKOFF_SECONDS = 21600
_MAX_FAILED_READS = 1_000_000
# Departure reads per plan tick, so a long history never becomes a read storm.
MAX_DEPARTED_READS = 4
_MAX_HOLDS = 16


@dataclass(frozen=True)
class ReviewNotAdmitted:
    """Review dispatch leaves this pull request alone for holds outside the review workflow, each named.

    Holds are labels (`needs-human-review`, `ai-parked`, `no-ai-review`, a
    missing trigger), a lifecycle hold, the unproductive-round breaker, or the
    identity mode. No re-arm removes them.
    """
    holds: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.holds, tuple) or not 1 <= len(self.holds) <= _MAX_HOLDS:
            raise ValueError("a not-admitted decision names between 1 and 16 holds")
        for hold in self.holds:
            validate_workflow_text(hold, "exclusion hold", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class ReviewDeferred:
    """The daemon could not decide this tick (an unreadable fact), or waits for a bounded read schedule."""
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "deferred decision reason", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class ReviewTerminal:
    """A harness read found the pull request closed or merged; no review is ever dispatched for it."""
    state: PullRequestState

    def __post_init__(self) -> None:
        if not isinstance(self.state, PullRequestState) or self.state is PullRequestState.OPEN:
            raise ValueError("a terminal decision names a closed or merged pull request")


class WithheldReason(Enum):
    """Why this tick's dispatch admission withheld a review the plan owed (decided in the review role's `decide`)."""
    # A fix run still holds the pull request's issue (`hold_review_dispatch`).
    FIX_CLAIM = "fix_claim"
    # The issue is reserved by another pull request here or another deployment.
    RESERVED = "reserved"


@dataclass(frozen=True)
class ReviewInFlight:
    """The owed review is already running: this pull request's own dispatch still holds its reservation."""
    kind: ReviewKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewKind):
            raise ValueError("a running review names its kind")


@dataclass(frozen=True)
class ReviewWithheld:
    """A review is owed, but this tick's dispatch admission withheld it; the next tick decides again."""
    kind: ReviewKind
    reason: WithheldReason
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewKind) or not isinstance(self.reason, WithheldReason):
            raise ValueError("a withheld review names its kind and a typed reason")
        validate_workflow_text(self.detail, "withheld review detail", limit=MAX_DETAIL_CHARS)


RecordedDispatch = (ReviewTerminal | ReviewNotAdmitted | ReviewDeferred | ReviewWithheld | ReviewInFlight
                    | ReviewDispatch)


@dataclass(frozen=True)
class AdmissionWithheld:
    """This tick's dispatch admission started no review for the pull request, for `reason`."""
    reason: WithheldReason
    detail: str


@dataclass(frozen=True)
class AdmissionInFlight:
    """This tick's dispatch admission found the pull request's own review dispatch still running."""


AdmissionOutcome = AdmissionWithheld | AdmissionInFlight


def admitted_decision(decision: RecordedDispatch, admission: AdmissionOutcome | None) -> RecordedDispatch:
    """The plan's decision as dispatch admission left it: an owed review admission did not start says why."""
    if not isinstance(decision, ReviewOwed) or admission is None:
        return decision
    if isinstance(admission, AdmissionInFlight):
        return ReviewInFlight(decision.kind)
    if not isinstance(admission, AdmissionWithheld):
        raise ValueError("an admission outcome must be typed")
    return ReviewWithheld(decision.kind, admission.reason,
                          admission.detail[:MAX_DETAIL_CHARS] or admission.reason.value)


class DecisionRead(Enum):
    """Which of the review daemon's reads a decision was made from."""
    # The daemon's review read: trigger-labelled pull requests, plus every
    # open one when adopting (``build_review_world``).
    REVIEW_READ = "review_read"
    # The pull request's own read, after it left the review read.
    DEPARTED_READ = "departed_read"


def _validate_dispatch(decision: object) -> None:
    """The policy's dispatch values carry no checks of their own; a stored one is checked here."""
    if isinstance(decision, (ReviewTerminal, ReviewNotAdmitted, ReviewDeferred, ReviewWithheld, ReviewInFlight,
                             SubmissionUnresolved)):
        return
    if isinstance(decision, ReviewOwed):
        if not isinstance(decision.kind, ReviewKind):
            raise ValueError("an owed review names its kind")
    elif isinstance(decision, ReviewNotOwed):
        validate_workflow_text(decision.reason, "not-owed reason", limit=MAX_DETAIL_CHARS)
    elif isinstance(decision, ReviewHeld):
        if (not isinstance(decision.gate_ids, tuple) or not 1 <= len(decision.gate_ids) <= MAX_WORKFLOW_GATES
                or len(set(decision.gate_ids)) != len(decision.gate_ids)):
            raise ValueError("a held decision names its distinct gates")
        for gate_id in decision.gate_ids:
            validate_workflow_identifier(gate_id, "holding gate")
    elif isinstance(decision, ReleaseProjectionPending):
        validate_workflow_identifier(decision.command_id, "release projection command")
    else:
        raise ValueError("a recorded dispatch decision must be a typed decision")


@dataclass(frozen=True)
class DecisionBasis:
    """The pull request facts the daemon decided from, all from its own read.

    `external_holds` is the re-arm hold source applied to that read (trusted
    hold labels and the unproductive-round breaker): holds a re-arm keeps.
    """
    head_sha: str
    state: PullRequestState
    labels: frozenset[Label]
    external_holds: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "decided pull request head")
        if not isinstance(self.state, PullRequestState):
            raise ValueError("decided pull request state must be typed")
        validate_labels(self.labels, "decided pull request labels")
        if not isinstance(self.external_holds, tuple) or len(self.external_holds) > _MAX_HOLDS or len(
            set(self.external_holds)
        ) != len(self.external_holds):
            raise ValueError("external holds are a bounded tuple of distinct holds")
        for hold in self.external_holds:
            validate_workflow_text(hold, "external hold", limit=MAX_DETAIL_CHARS)


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True)
class DepartedReadFailure:
    """Consecutive failed departed reads: how many, the latest reason, and when they started and last failed."""
    failed_reads: int
    reason: str
    first_failed_at: datetime
    last_failed_at: datetime

    def __post_init__(self) -> None:
        if type(self.failed_reads) is not int or not 1 <= self.failed_reads <= _MAX_FAILED_READS:
            raise ValueError("failed departed reads are a bounded positive count")
        validate_workflow_text(self.reason, "failed departed read reason", limit=MAX_DETAIL_CHARS)
        if _aware(self.first_failed_at, "first failed read") > _aware(self.last_failed_at, "last failed read"):
            raise ValueError("the first failed read precedes the last")


@dataclass(frozen=True)
class RecordedReviewDecision:
    """One pull request's latest daemon decision: made at `decided_at`, last confirmed unchanged at `as_of`.

    `read_failure` annotates a departed read that keeps failing: its count
    sets the next read's backoff. The decision and facts are then the last
    good read's, or a factless deferral when there never was one.
    """
    key: WorkflowKey
    decision: RecordedDispatch
    basis: DecisionBasis | None
    read: DecisionRead
    decided_at: datetime
    as_of: datetime
    read_failure: DepartedReadFailure | None = None

    @property
    def failed_reads(self) -> int:
        return 0 if self.read_failure is None else self.read_failure.failed_reads

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.key.target, PullRequestRef):
            raise ValueError("a recorded review decision belongs to one pull request")
        _validate_dispatch(self.decision)
        if not isinstance(self.read, DecisionRead):
            raise ValueError("a recorded decision names the read it was made from")
        if self.basis is None:
            if not isinstance(self.decision, ReviewDeferred):
                raise ValueError("only a deferred decision may lack the facts it was decided from")
        elif not isinstance(self.basis, DecisionBasis):
            raise ValueError("a decision basis must be typed")
        elif isinstance(self.decision, ReviewTerminal) != (self.basis.state is not PullRequestState.OPEN):
            raise ValueError("a decision is terminal exactly when its basis is closed or merged")
        elif isinstance(self.decision, ReviewTerminal) and self.decision.state is not self.basis.state:
            raise ValueError("a terminal decision names the state it was decided from")
        if _aware(self.decided_at, "decision time") > _aware(self.as_of, "decision confirmation time"):
            raise ValueError("a decision is confirmed no earlier than it was made")
        if self.read_failure is not None:
            if not isinstance(self.read_failure, DepartedReadFailure):
                raise ValueError("a failed departed read annotation must be typed")
            if self.read is not DecisionRead.DEPARTED_READ:
                raise ValueError("only a departed read counts failures")
            if self.read_failure.first_failed_at < self.decided_at:
                raise ValueError("failures annotate a decision made before them")


def record_review_decision(
    prior: RecordedReviewDecision | None, key: WorkflowKey, decision: RecordedDispatch,
    basis: DecisionBasis | None, read: DecisionRead, now: datetime,
) -> RecordedReviewDecision | None:
    """The row this tick writes, or None when the recorded decision stands unrefreshed.

    A changed decision, basis or read is written at once. An unchanged one
    only refreshes `as_of`, and at most every `DECISION_CONFIRM_SECONDS`.
    """
    _aware(now, "decision time")
    if prior is not None and (not isinstance(prior, RecordedReviewDecision) or prior.key != key):
        raise ValueError("a decision update replaces this pull request's own record")
    if prior is not None and (prior.decision, prior.basis, prior.read) == (decision, basis, read):
        # A successful read ends a failure run at once: the annotation must not outlive it.
        if prior.read_failure is None and now - prior.as_of < timedelta(seconds=DECISION_CONFIRM_SECONDS):
            return None
        return replace(prior, as_of=now, read_failure=None)
    return RecordedReviewDecision(key, decision, basis, read, now, now)


def record_departed_read_failure(
    prior: RecordedReviewDecision | None, key: WorkflowKey, reason: str, now: datetime,
) -> RecordedReviewDecision:
    """The record of one more failed departed read: always written, since the count moves the next read.

    A departed record with facts from its own last good read keeps them (a
    closed pull request stays shown closed, as of that read) and gains the
    failure annotation. Without such facts (the pull request just left the
    review read, whose decision is stale by then) it is a factless deferral.
    """
    _aware(now, "decision time")
    if prior is not None and (not isinstance(prior, RecordedReviewDecision) or prior.key != key):
        raise ValueError("a decision update replaces this pull request's own record")
    earlier = prior.read_failure if prior is not None else None
    failure = (DepartedReadFailure(1, reason, now, now) if earlier is None else
               DepartedReadFailure(min(earlier.failed_reads + 1, _MAX_FAILED_READS), reason,
                                   earlier.first_failed_at, now))
    if prior is not None and prior.read is DecisionRead.DEPARTED_READ and prior.basis is not None:
        return replace(prior, read_failure=failure)
    return RecordedReviewDecision(key, ReviewDeferred(reason), None, DecisionRead.DEPARTED_READ,
                                  failure.first_failed_at, now, read_failure=failure)


@dataclass(frozen=True)
class DecisionUpdate:
    """A decision the daemon made from one of its reads, to record."""
    key: WorkflowKey
    decision: RecordedDispatch
    basis: DecisionBasis | None
    read: DecisionRead


@dataclass(frozen=True)
class DepartedReadFailed:
    """A departed pull request's own read failed, to record against its schedule."""
    key: WorkflowKey
    reason: str


DecisionEntry = DecisionUpdate | DepartedReadFailed


def next_decision_record(
    prior: RecordedReviewDecision | None, entry: DecisionEntry, now: datetime,
) -> RecordedReviewDecision | None:
    """The row one entry writes over `prior`, or None when the recorded decision stands."""
    if isinstance(entry, DepartedReadFailed):
        return record_departed_read_failure(prior, entry.key, entry.reason, now)
    if not isinstance(entry, DecisionUpdate):
        raise ValueError("a decision entry must be typed")
    decision = entry.decision
    if entry.basis is None and not isinstance(decision, ReviewDeferred):
        decision = ReviewDeferred("the pull request facts behind this decision are unreadable")
    return record_review_decision(prior, entry.key, decision, entry.basis, entry.read, now)


def departed_backoff_seconds(failed_reads: int) -> int:
    """The wait after `failed_reads` consecutive failed departed reads: doubling, capped."""
    if type(failed_reads) is not int or failed_reads < 1:
        raise ValueError("a departed-read backoff follows at least one failure")
    return min(DEPARTED_CONFIRM_SECONDS * 2 ** min(failed_reads - 1, 16), MAX_DEPARTED_BACKOFF_SECONDS)


def departed_backoff_at_ceiling(failed_reads: int) -> bool:
    """Whether this failure count first reaches the backoff ceiling (the journal's second event)."""
    return (departed_backoff_seconds(failed_reads) == MAX_DEPARTED_BACKOFF_SECONDS
            and (failed_reads == 1 or departed_backoff_seconds(failed_reads - 1) < MAX_DEPARTED_BACKOFF_SECONDS))


def departed_read_after(record: RecordedReviewDecision | None, *, holds_gates: bool) -> datetime | None:
    """From when a pull request missing from the review read is due its own read; None when never.

    A decision made in the review read is stale the moment the pull request
    leaves that read, so it is due at once. After its own read: failures
    back off exponentially to a ceiling from the last failure; a merged
    pull request is never read again; a closed one is re-read every
    `DEPARTED_CONFIRM_SECONDS` while its workflow holds gates and it closed
    under `CLOSED_PROMPT_SECONDS` ago (as of the last read), otherwise every
    `CLOSED_RECONFIRM_SECONDS`; any other every `DEPARTED_CONFIRM_SECONDS`.
    """
    if record is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if record.read is DecisionRead.REVIEW_READ:
        return record.as_of
    decision = record.decision
    if isinstance(decision, ReviewTerminal) and decision.state is PullRequestState.MERGED:
        return None
    if record.read_failure is not None:
        return record.read_failure.last_failed_at + timedelta(
            seconds=departed_backoff_seconds(record.read_failure.failed_reads))
    if isinstance(decision, ReviewTerminal):
        prompt = holds_gates and record.as_of - record.decided_at < timedelta(seconds=CLOSED_PROMPT_SECONDS)
        if not prompt:
            return record.as_of + timedelta(seconds=CLOSED_RECONFIRM_SECONDS)
    return record.as_of + timedelta(seconds=DEPARTED_CONFIRM_SECONDS)


def departed_read_due(record: RecordedReviewDecision | None, now: datetime, *, holds_gates: bool) -> bool:
    """Whether a pull request missing from the review read should be read on its own now."""
    due = departed_read_after(record, holds_gates=holds_gates)
    return due is not None and _aware(now, "departure check time") >= due
