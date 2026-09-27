"""Bounded harness reads a review dispatch depends on; ADR 0012/0015 UNVERIFIABLE loops.

These reads decide what a review may do; none may be guessed:

- `EXEMPTION_TRAILERS`: the PR's commit trailers declaring gate exemptions
  (ADR 0014). Unreadable is never "no declarations".
- `REVIEW_HISTORY`: the complete paginated review list the rejection ceiling
  is counted from, and that settling an escalation re-reads. A partial read
  can neither count nor disprove the ceiling.
- `LINKED_ISSUE`: the linked issue's lifecycle and current workflow holds.
  A failed known-issue read is never interpreted as an unlinked PR.
- `RUNTIME_PREPARATION`: a local runtime preflight after a verified child
  could not launch. It cannot manufacture a review verdict.

An unavailable read is UNVERIFIABLE, so the review (or the settlement)
defers without a verdict. Unbounded, that is a silent retry loop that pays a
full read every tick. Here each unavailable read for one head backs off
exponentially and, at the reconciliation age/read ceiling, a visible
`EvidenceUnavailable` gate holds the PR; only a later scheduled read that
succeeds releases it, and the work then runs under the original contract.
The gate is never an approval, an exemption or a count. Values and functions
perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib

from .review_rejection import validate_commit_sha
from .workflow import (
    ActiveReconciliation, EvidenceUnavailable, NoAuthorizedRemediation, OpenGate, RearmingGate, ReconciliationPolicy,
)
from .workflow_identity import MAX_DETAIL_CHARS, PullRequestRef, WorkflowBinding, validate_workflow_text


class ReviewEvidenceKind(str, Enum):
    EXEMPTION_TRAILERS = "exemption-evidence"
    REVIEW_HISTORY = "review-history"
    LINKED_ISSUE = "linked-issue"
    RUNTIME_PREPARATION = "review-runtime-preparation"


_SUBJECT = {
    ReviewEvidenceKind.EXEMPTION_TRAILERS: "gate-exemption declarations",
    ReviewEvidenceKind.REVIEW_HISTORY: "the complete review history",
    ReviewEvidenceKind.LINKED_ISSUE: "the linked issue and its current holds",
    ReviewEvidenceKind.RUNTIME_PREPARATION: "the local review runtime configuration",
}
_REMEDIATION = {
    ReviewEvidenceKind.EXEMPTION_TRAILERS: (
        "a scheduled harness read of the commit trailers must succeed; it never approves or grants an exemption"),
    ReviewEvidenceKind.REVIEW_HISTORY: (
        "a scheduled harness read of every review page must succeed; it never counts, clears or approves"),
    ReviewEvidenceKind.LINKED_ISSUE: (
        "a scheduled harness read of the linked issue and its authority must succeed; it never releases that issue"),
    ReviewEvidenceKind.RUNTIME_PREPARATION: (
        "a scheduled local runtime preflight must succeed; it never creates a review verdict"),
}


def _kind(kind: object) -> ReviewEvidenceKind:
    if not isinstance(kind, ReviewEvidenceKind):
        raise ValueError("review evidence requires a typed evidence kind")
    return kind


def evidence_gate_id(kind: ReviewEvidenceKind, pr: int, head_sha: str) -> str:
    """The one visible hold a head's unreadable evidence of this kind can open."""
    kind = _kind(kind)
    if type(pr) is not int or pr <= 0:
        raise ValueError("review evidence belongs to a pull request")
    validate_commit_sha(head_sha, "review evidence head")
    return f"{kind.value}:" + hashlib.sha256(f"{pr}:{head_sha}".encode()).hexdigest()


def exemption_evidence_gate_id(pr: int, head_sha: str) -> str:
    return evidence_gate_id(ReviewEvidenceKind.EXEMPTION_TRAILERS, pr, head_sha)


def review_evidence_gate_kind(gate: object) -> ReviewEvidenceKind | None:
    """The evidence kind a gate holds a review on, or None for any other gate."""
    opened = gate.gate if isinstance(gate, RearmingGate) else gate
    if not isinstance(opened, OpenGate) or not isinstance(opened.cause, EvidenceUnavailable):
        return None
    return next((kind for kind in ReviewEvidenceKind if opened.gate_id.startswith(f"{kind.value}:")), None)


def is_review_evidence_gate(gate: object) -> bool:
    """Whether a gate holds a review on unreadable evidence of any kind.

    It belongs to a review of one generation's head: a generation advance
    owes a fresh review that re-reads the evidence, so the advance supersedes
    the hold rather than carrying it (`ends_with_advance`).
    """
    return review_evidence_gate_kind(gate) is not None


def is_exemption_evidence_gate(gate: object) -> bool:
    return review_evidence_gate_kind(gate) is ReviewEvidenceKind.EXEMPTION_TRAILERS


@dataclass(frozen=True)
class EvidenceSchedule:
    """The read history of one head's evidence of one kind (never a verdict)."""
    head_sha: str
    history: ActiveReconciliation
    last_error: str

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "review evidence head")
        if not isinstance(self.history, ActiveReconciliation) or self.history.failed_reads < 1:
            raise ValueError("a review evidence schedule records at least one unavailable read")
        validate_workflow_text(self.last_error, "review evidence error", limit=MAX_DETAIL_CHARS)


ExemptionEvidenceSchedule = EvidenceSchedule


@dataclass(frozen=True)
class EvidenceProgress:
    schedule: EvidenceSchedule
    gate: OpenGate | None


ExemptionEvidenceProgress = EvidenceProgress


def unavailable_evidence(
    kind: ReviewEvidenceKind, binding: WorkflowBinding, head_sha: str, prior: EvidenceSchedule | None, *,
    now: datetime, error: str, policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> EvidenceProgress:
    """Schedule the next read after an unavailable one; gate at the ceiling.

    A prior schedule for another head is a different question and restarts
    the history. Reads continue at the maximum delay once gated.
    """
    kind = _kind(kind)
    if not isinstance(binding, WorkflowBinding) or not isinstance(binding.fence.key.target, PullRequestRef):
        raise ValueError("review evidence belongs to a pull request workflow")
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("review evidence scheduling requires a validated policy")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("review evidence scheduling requires an aware clock")
    validate_commit_sha(head_sha, "review evidence head")
    if not isinstance(error, str) or not error:
        raise ValueError("an unavailable review evidence read records why")
    if prior is not None and not isinstance(prior, EvidenceSchedule):
        raise ValueError("review evidence history must be a validated schedule")
    same = prior is not None and prior.head_sha == head_sha
    since = prior.history.unresolved_since if same and prior is not None else now
    if now < since:
        # Backward time cannot manufacture an older unresolved age.
        since = now
    reads = min(prior.history.failed_reads + 1, 2**63 - 1) if same and prior is not None else 1
    schedule = EvidenceSchedule(head_sha, ActiveReconciliation(since, now + timedelta(seconds=_delay(reads, policy)),
                                                               reads), error[:MAX_DETAIL_CHARS])
    if not _exhausted(schedule.history, now, policy):
        return EvidenceProgress(schedule, None)
    pr = binding.fence.key.target.number
    return EvidenceProgress(schedule, OpenGate(
        evidence_gate_id(kind, pr, head_sha), binding.fence.generation,
        EvidenceUnavailable(f"{_SUBJECT[kind]} of pull request #{pr} at {head_sha}", since, reads,
                            schedule.last_error),
        NoAuthorizedRemediation(_REMEDIATION[kind]),
    ))


def _delay(reads: int, policy: ReconciliationPolicy) -> float:
    return min(policy.maximum_delay_seconds, policy.initial_delay_seconds * 2 ** min(reads - 1, 17))


def _exhausted(history: ActiveReconciliation, now: datetime, policy: ReconciliationPolicy) -> bool:
    return (history.failed_reads >= policy.maximum_reads
            or (now - history.unresolved_since).total_seconds() >= policy.maximum_age_seconds)


@dataclass(frozen=True)
class ObservationProgress:
    history: ActiveReconciliation
    at_ceiling: bool
    surface: bool  # the ceiling is reached and has not been surfaced yet


def unavailable_first_observation(
    prior: ActiveReconciliation | None, *, surfaced: bool, now: datetime,
    policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> ObservationProgress:
    """Back off the first read of a pull request the workflow has never observed.

    Without a workflow there is nothing to gate, so the ceiling is surfaced
    once (journalled; `surfaced` records that it was) and reads continue at
    the maximum delay. Unbounded, an unreadable pull request costs one
    complete read per tick.
    """
    if not isinstance(surfaced, bool):
        raise ValueError("whether the ceiling was surfaced is a boolean")
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("observation scheduling requires a validated policy")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("observation scheduling requires an aware clock")
    if prior is not None and not isinstance(prior, ActiveReconciliation):
        raise ValueError("observation history must be a validated schedule")
    since = prior.unresolved_since if prior is not None else now
    since = min(since, now)  # backward time cannot manufacture an older unresolved age
    reads = min(prior.failed_reads + 1, 2**63 - 1) if prior is not None else 1
    history = ActiveReconciliation(since, now + timedelta(seconds=_delay(reads, policy)), reads)
    exhausted = _exhausted(history, now, policy)
    return ObservationProgress(history, exhausted, exhausted and not surfaced)


def unavailable_exemption_evidence(
    binding: WorkflowBinding, head_sha: str, prior: EvidenceSchedule | None, *, now: datetime,
    error: str, policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> EvidenceProgress:
    return unavailable_evidence(ReviewEvidenceKind.EXEMPTION_TRAILERS, binding, head_sha, prior, now=now,
                                error=error, policy=policy)


def evidence_due(schedule: EvidenceSchedule | None, head_sha: str, *, now: datetime) -> bool:
    """Whether a read of this head's evidence may be attempted now."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("review evidence scheduling requires an aware clock")
    if schedule is None or schedule.head_sha != head_sha:
        return True
    return schedule.history.next_read_at <= now


exemption_evidence_due = evidence_due
