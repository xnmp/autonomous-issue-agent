"""Action-specific eligibility results shared by scheduling and completion."""

from dataclasses import dataclass
from enum import Enum

from .labels import Label


class LifecycleOperation(str, Enum):
    RESOLVE = "resolve"
    RESUME = "resume"
    REPAIR = "repair"
    PR_RESUME = "pr_resume"
    REVIEW = "review"
    MERGE = "merge"
    UPDATE_BRANCH = "update_branch"
    REMEDIATE_CI = "remediate_ci"
    GROOM = "groom"
    COMPLETE_RESOLVE = "complete_resolve"
    CLOSE = "close"
    CLOSE_MERGED = "close_merged"


@dataclass(frozen=True)
class AllowedEligibility:
    operation: LifecycleOperation


@dataclass(frozen=True)
class HeldEligibility:
    operation: LifecycleOperation
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class InvalidEligibility:
    operation: LifecycleOperation
    reason: str


Eligibility = AllowedEligibility | HeldEligibility | InvalidEligibility


@dataclass(frozen=True)
class IssueCompletionGuard:
    """Facts authorizing a completion, retained across durable command retries."""

    operation: LifecycleOperation
    title: str
    body: str
    author: str
    state_reason: str
    labels: frozenset[Label]
    comments: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class IssueHoldGuard:
    """ADR 0015 semantic applicability fence for a hold projection.

    Exactly the facts that hold, release or terminate resolve work: the open
    state and its reason, and every lifecycle, hold and intervention label.
    Linked-PR absence is implied: a projection is only minted when no PR is
    linked, and the executor re-reads linkage before every write. Comments and
    title/body edits bump the raw issue revision but are not decision facts,
    so they neither supersede a re-arm nor make its authorized retry
    inapplicable. Priority labels only order queued work and are excluded.
    """

    state_reason: str
    labels: frozenset[Label]

    def __post_init__(self) -> None:
        if not isinstance(self.state_reason, str) or len(self.state_reason) > 256 or "\x00" in self.state_reason:
            raise ValueError("hold guard state reason must be bounded text")
        if not isinstance(self.labels, frozenset) or any(not isinstance(label, Label) for label in self.labels):
            raise ValueError("hold guard labels must be a frozenset of labels")
        if self.labels & ORDERING_LABELS:
            raise ValueError("hold guard labels exclude ordering-only labels")


# Labels that order ai-ready work but never hold, release or terminate it.
ORDERING_LABELS = frozenset({Label.P1, Label.P2, Label.P3})
