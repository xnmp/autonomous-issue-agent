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
from .eligibility import (
    AllowedEligibility, Eligibility, HeldEligibility, InvalidEligibility,
    LifecycleOperation, IssueCompletionGuard, IssueHoldGuard, ORDERING_LABELS,
)


_EXPLICIT_HOLDS = frozenset({
    Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_BUDGET_REVIEW,
    Label.AI_PARKED, Label.NEEDS_DESIGN,
})


def completion_guard(issue, operation: LifecycleOperation) -> IssueCompletionGuard:
    return IssueCompletionGuard(
        operation, issue.title, issue.body, issue.author, issue.state_reason,
        issue.labels, tuple((c.author, c.body, c.created_at) for c in issue.comments),
    )


def _labels_within(before: frozenset[Label], current: frozenset[Label],
                   removed: tuple[Label, ...], added: tuple[Label, ...]) -> bool:
    """Only this command's own declared label effects may separate the two sets."""
    return not (current - before - set(added) or before - current - set(removed))


def completion_matches(
    guard: IssueCompletionGuard, issue, *, removed: tuple[Label, ...] = (),
    added: tuple[Label, ...] = (), comment: str = "",
) -> bool:
    """Accept only the original facts plus this command's declared partial effects."""
    if issue.state != "open" or (
        issue.title, issue.body, issue.author, issue.state_reason
    ) != (guard.title, guard.body, guard.author, guard.state_reason):
        return False
    if not _labels_within(guard.labels, issue.labels, removed, added):
        return False
    current = [(c.author, c.body, c.created_at) for c in issue.comments]
    for original in guard.comments:
        if original not in current:
            return False
        current.remove(original)
    return not current or (bool(comment) and len(current) == 1 and current[0][1] == comment)


def hold_guard(issue) -> IssueHoldGuard:
    """Capture the hold facts an accepted projection decision depends on."""
    if issue.state != "open":
        raise ValueError("a hold projection guard requires an open issue")
    return IssueHoldGuard(issue.state_reason, frozenset(issue.labels) - ORDERING_LABELS)


def hold_guard_applies(
    guard: IssueHoldGuard, issue, *, linked_pr: int | None,
    removed: tuple[Label, ...] = (), added: tuple[Label, ...] = (),
) -> bool:
    """Whether the accepted hold facts still hold, modulo this command's own effects.

    A comment or body edit leaves every one of these facts unchanged; a new
    hold, recurrence or terminal label, a close/reopen, or a linked PR does not.
    """
    if not isinstance(guard, IssueHoldGuard):
        raise ValueError("hold guard evaluation requires a typed guard")
    if issue.state != "open" or issue.state_reason != guard.state_reason or linked_pr is not None:
        return False
    return _labels_within(guard.labels, frozenset(issue.labels) - ORDERING_LABELS, removed, added)


def completion_eligibility(issue, operation: LifecycleOperation, action) -> Eligibility:
    if isinstance(action, Relabel) and action.add in {
        Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW, Label.NEEDS_HUMAN_REVIEW,
    } and not set(action.removed_labels) & (_EXPLICIT_HOLDS | {Label.AI_BLOCKED}):
        return AllowedEligibility(operation)
    return issue_eligibility(issue, operation)


def issue_eligibility(issue, operation: LifecycleOperation) -> Eligibility:
    """Interpret reachable partial transitions without erasing explicit holds."""
    if operation not in {
        LifecycleOperation.RESOLVE, LifecycleOperation.RESUME, LifecycleOperation.REPAIR,
        LifecycleOperation.TRIAGE, LifecycleOperation.GROOM, LifecycleOperation.CLOSE,
        LifecycleOperation.CLOSE_MERGED,
        LifecycleOperation.COMPLETE_RESOLVE, LifecycleOperation.COMPLETE_TRIAGE,
    }:
        return InvalidEligibility(operation, "operation does not apply to an issue")
    if issue.state not in {"open", "closed"}:
        return InvalidEligibility(operation, "issue state is unavailable or malformed")
    if issue.state != "open" or (
        Label.AI_DONE in issue.labels and operation is not LifecycleOperation.CLOSE_MERGED
    ):
        return HeldEligibility(operation, ("issue is terminal",))
    if operation in {LifecycleOperation.CLOSE, LifecycleOperation.CLOSE_MERGED} and getattr(
        issue, "state_reason", ""
    ).strip().casefold() == "reopened":
        return HeldEligibility(operation, ("issue was reopened",))
    holds = issue.labels & _EXPLICIT_HOLDS
    if holds:
        return HeldEligibility(operation, tuple(sorted(label.value for label in holds)))
    if Label.AI_BLOCKED in issue.labels and operation is not LifecycleOperation.RESUME:
        return HeldEligibility(operation, (Label.AI_BLOCKED.value,))
    if operation is LifecycleOperation.CLOSE_MERGED:
        allowed = Label.AI_DONE in issue.labels
    elif operation is LifecycleOperation.RESOLVE:
        allowed = Label.AI_READY in issue.labels and not issue.labels & {
            Label.AI_WORKING, Label.AI_PR_OPEN, Label.AI_SUGGESTION,
        }
    elif operation is LifecycleOperation.RESUME:
        allowed = Label.AI_BLOCKED in issue.labels and Label.AI_PR_OPEN not in issue.labels
    elif operation in {
        LifecycleOperation.TRIAGE, LifecycleOperation.COMPLETE_TRIAGE,
        LifecycleOperation.CLOSE,
    }:
        allowed = not issue.labels & {
            Label.AI_READY, Label.AI_WORKING, Label.AI_PR_OPEN, Label.AI_SUGGESTION,
        }
    else:
        allowed = True
    return (AllowedEligibility(operation) if allowed else
            HeldEligibility(operation, ("workflow position does not permit this operation",)))


def pr_eligibility(pr, operation: LifecycleOperation) -> Eligibility:
    if operation not in {
        LifecycleOperation.PR_RESUME, LifecycleOperation.REVIEW, LifecycleOperation.REPAIR,
        LifecycleOperation.MERGE, LifecycleOperation.UPDATE_BRANCH, LifecycleOperation.REMEDIATE_CI,
    }:
        return InvalidEligibility(operation, "operation does not apply to a PR")
    if pr.state not in {"OPEN", "CLOSED", "MERGED"}:
        return InvalidEligibility(operation, "PR state is unavailable or malformed")
    if pr.state != "OPEN" or Label.AI_DONE in pr.labels:
        return HeldEligibility(operation, ("PR is terminal",))
    holds = pr.labels & _EXPLICIT_HOLDS
    if operation is LifecycleOperation.REVIEW:
        holds = holds | (pr.labels & {Label.NO_AI_REVIEW, Label.AI_WORKING})
    if Label.AI_BLOCKED in pr.labels and operation is not LifecycleOperation.PR_RESUME:
        holds = holds | {Label.AI_BLOCKED}
    if holds:
        return HeldEligibility(operation, tuple(sorted(label.value for label in holds)))
    if operation is LifecycleOperation.PR_RESUME and Label.AI_BLOCKED not in pr.labels:
        return HeldEligibility(operation, ("PR is not awaiting a reply",))
    return AllowedEligibility(operation)


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
    # `ai-closed` is deliberately absent: IssuePhase.DONE means "a harness PR
    # merged for this issue", which a steward triage close never did. A
    # steward-closed issue is UNTRACKED here, which is the truthful answer for a
    # view over PIPELINE position. The Jira mapping answers a different question
    # ("may the ticket rest?") and does map it — see domain/jira.py.
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
        return isinstance(issue_eligibility(self, LifecycleOperation.RESOLVE), AllowedEligibility)

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
        return Label.AI_PR_OPEN in self.labels and isinstance(
            pr_eligibility(self, LifecycleOperation.REVIEW), AllowedEligibility
        )

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
    """`from_label` is the lifecycle label the PR currently carries — normally
    AI_PR_OPEN, but the reviewer's trigger label is configurable (AI_REVIEW on a
    repo where another bot owns ai-pr-open), and removing the wrong one leaves
    the PR in the reviewer's pickable set forever (review M10/D4)."""

    pr: int
    reason: str
    from_label: Label = Label.AI_PR_OPEN

    def actions(self) -> list:
        return [
            PostComment(Target.PR, self.pr, f"**Circuit breaker:** {self.reason} Escalating to a human."),
            Relabel(Target.PR, self.pr, self.from_label, Label.NEEDS_HUMAN_REVIEW),
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


def pr_escalation(
    pr: int, reason: str, from_label: Label = Label.AI_PR_OPEN
) -> PrEscalation:
    return PrEscalation(pr=pr, reason=reason, from_label=from_label)


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
    "IssueEscalation",
    "issue_lifecycle",
    "pr_lifecycle",
    "retry_breaker",
    "pr_escalation",
    "issue_escalation",
]
