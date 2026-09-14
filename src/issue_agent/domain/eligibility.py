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
    TRIAGE = "triage"
    GROOM = "groom"
    COMPLETE_RESOLVE = "complete_resolve"
    COMPLETE_TRIAGE = "complete_triage"
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
