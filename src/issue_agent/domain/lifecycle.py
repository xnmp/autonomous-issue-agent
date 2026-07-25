"""Lifecycle views over GitHub labels.

GitHub labels remain the external coordination channel, but workflow code should
not have to rediscover lifecycle meaning from raw label membership at every call
site. These value objects make the issue/PR lifecycle explicit while preserving
the existing label-backed behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .actions import PostComment, Relabel, Target
from .labels import Label


class IssuePhase(str, Enum):
    READY = "ready"
    WORKING = "working"
    BLOCKED = "blocked"
    PR_OPEN = "pr_open"
    DONE = "done"
    TRIAGE = "triage"
    SUGGESTION = "suggestion"
    NEEDS_DESIGN = "needs_design"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    UNTRACKED = "untracked"


class PrPhase(str, Enum):
    REVIEWABLE = "reviewable"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    CLOSED = "closed"
    EXTERNAL_REVIEW = "external_review"
    NO_REVIEW = "no_review"
    UNTRACKED = "untracked"


_ISSUE_PHASES: tuple[tuple[Label, IssuePhase], ...] = (
    (Label.AI_DONE, IssuePhase.DONE),
    (Label.NEEDS_HUMAN_REVIEW, IssuePhase.NEEDS_HUMAN_REVIEW),
    (Label.NEEDS_DESIGN, IssuePhase.NEEDS_DESIGN),
    (Label.AI_SUGGESTION, IssuePhase.SUGGESTION),
    (Label.AI_PR_OPEN, IssuePhase.PR_OPEN),
    (Label.AI_WORKING, IssuePhase.WORKING),
    (Label.AI_BLOCKED, IssuePhase.BLOCKED),
    (Label.AI_READY, IssuePhase.READY),
    (Label.AI_TRIAGE, IssuePhase.TRIAGE),
)


_PR_PHASES: tuple[tuple[Label, PrPhase], ...] = (
    (Label.NO_AI_REVIEW, PrPhase.NO_REVIEW),
    (Label.NEEDS_HUMAN_REVIEW, PrPhase.BLOCKED),
    (Label.AI_BLOCKED, PrPhase.BLOCKED),
    (Label.AI_WORKING, PrPhase.WORKING),
    (Label.AI_PR_OPEN, PrPhase.REVIEWABLE),
    (Label.AI_REVIEW, PrPhase.EXTERNAL_REVIEW),
)


@dataclass(frozen=True)
class IssueLifecycle:
    labels: frozenset[Label]
    state: str = "open"

    @property
    def phase(self) -> IssuePhase:
        for label, phase in _ISSUE_PHASES:
            if label in self.labels:
                return phase
        return IssuePhase.UNTRACKED

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_ready(self) -> bool:
        return self.is_open and Label.AI_READY in self.labels

    @property
    def is_blocked(self) -> bool:
        return self.is_open and Label.AI_BLOCKED in self.labels

    @property
    def is_working(self) -> bool:
        return Label.AI_WORKING in self.labels

    @property
    def is_pr_open(self) -> bool:
        return Label.AI_PR_OPEN in self.labels


@dataclass(frozen=True)
class PrLifecycle:
    labels: frozenset[Label]
    state: str = "OPEN"

    @property
    def phase(self) -> PrPhase:
        if self.state == "MERGED":
            return PrPhase.DONE
        if self.state == "CLOSED":
            return PrPhase.CLOSED
        for label, phase in _PR_PHASES:
            if label in self.labels:
                return phase
        return PrPhase.UNTRACKED

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def is_reviewable(self) -> bool:
        return self.is_open and Label.AI_PR_OPEN in self.labels

    @property
    def is_blocked(self) -> bool:
        return Label.AI_BLOCKED in self.labels or Label.NEEDS_HUMAN_REVIEW in self.labels

    @property
    def is_working(self) -> bool:
        return Label.AI_WORKING in self.labels

    @property
    def needs_cleanup(self) -> bool:
        return Label.AI_PR_OPEN in self.labels and self.state in ("MERGED", "CLOSED")


@dataclass(frozen=True)
class RetryBreaker:
    """Retry-loop circuit breaker.

    `attempts` is the number already made. `cap <= 0` means unbounded, matching
    the historic watcher behavior.
    """

    attempts: int
    cap: int

    @property
    def tripped(self) -> bool:
        return self.cap > 0 and self.attempts >= self.cap


@dataclass(frozen=True)
class PrEscalation:
    pr: int
    reason: str

    def actions(self) -> list:
        return [
            PostComment(Target.PR, self.pr, f"**Circuit breaker:** {self.reason} Escalating to a human."),
            Relabel(Target.PR, self.pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
        ]


@dataclass(frozen=True)
class IssueEscalation:
    """Issue-side twin of PrEscalation. `from_label` is whatever lifecycle label
    the issue currently carries (AI_READY for the resolve breaker; AI_WORKING
    for contract escalation) — swapping it for NEEDS_HUMAN_REVIEW is what makes
    the escalation one-shot: the issue leaves the pickable set."""

    issue: int
    reason: str
    from_label: Label

    def actions(self) -> list:
        return [
            PostComment(Target.ISSUE, self.issue,
                        f"**Circuit breaker:** {self.reason} Escalating to a human."),
            Relabel(Target.ISSUE, self.issue, self.from_label, Label.NEEDS_HUMAN_REVIEW),
        ]


def retry_breaker(attempts: int, cap: int) -> RetryBreaker:
    return RetryBreaker(attempts=attempts, cap=cap)


def pr_escalation(pr: int, reason: str) -> PrEscalation:
    return PrEscalation(pr=pr, reason=reason)


def issue_escalation(issue: int, reason: str, from_label: Label) -> IssueEscalation:
    return IssueEscalation(issue=issue, reason=reason, from_label=from_label)


def issue_lifecycle(issue) -> IssueLifecycle:
    return IssueLifecycle(labels=issue.labels, state=issue.state)


def pr_lifecycle(pr) -> PrLifecycle:
    return PrLifecycle(labels=pr.labels, state=pr.state)


__all__ = [
    "IssuePhase",
    "PrPhase",
    "IssueLifecycle",
    "PrLifecycle",
    "RetryBreaker",
    "PrEscalation",
    "issue_lifecycle",
    "pr_lifecycle",
    "retry_breaker",
    "pr_escalation",
]
