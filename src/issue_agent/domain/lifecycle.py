"""Lifecycle views over GitHub labels.

GitHub labels remain the external coordination channel, but workflow code should
not have to rediscover lifecycle meaning from raw label membership at every call
site. These value objects make the issue/PR lifecycle explicit while preserving
the existing label-backed behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

from .actions import Action, PostComment, Relabel, Target
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


def hold_guard(issue) -> IssueHoldGuard:
    """Capture the hold facts an accepted projection decision depends on."""
    if issue.state != "open":
        raise ValueError("a hold projection guard requires an open issue")
    return IssueHoldGuard(issue.state_reason, frozenset(issue.labels) - ORDERING_LABELS)


def issue_eligibility(issue, operation: LifecycleOperation) -> Eligibility:
    """Interpret reachable partial transitions without erasing explicit holds."""
    if operation not in {
        LifecycleOperation.RESOLVE, LifecycleOperation.RESUME, LifecycleOperation.REPAIR,
        LifecycleOperation.GROOM, LifecycleOperation.CLOSE,
        LifecycleOperation.CLOSE_MERGED, LifecycleOperation.COMPLETE_RESOLVE,
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
    elif operation is LifecycleOperation.CLOSE:
        allowed = not issue.labels & {
            Label.AI_READY, Label.AI_WORKING, Label.AI_PR_OPEN, Label.AI_SUGGESTION,
        }
    else:
        allowed = True
    return (AllowedEligibility(operation) if allowed else
            HeldEligibility(operation, ("workflow position does not permit this operation",)))


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
        return attention_actions(Target.PR, self.pr, self.from_label, Label.NEEDS_HUMAN_REVIEW,
                                 f"**Circuit breaker:** {self.reason} Escalating to a human.")


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
        return attention_actions(Target.ISSUE, self.issue, self.from_label, Label.NEEDS_HUMAN_REVIEW,
                                 f"**Circuit breaker:** {self.reason} Escalating to a human.")


def attention_actions(
    target: Target, number: int, remove: Label | None, add: Label, body: str, *, harness_signal: bool = False,
) -> list[Action]:
    """The producer owns the explicit transition; partial workflow reasons cannot infer label absence."""
    return [PostComment(target, number, body, harness_signal),
            Relabel(target, number, remove, add, harness_signal=harness_signal)]


def retry_breaker(attempts: int, cap: int) -> RetryBreaker:
    return RetryBreaker(attempts=attempts, cap=cap)


def pr_escalation(
    pr: int, reason: str, from_label: Label = Label.AI_PR_OPEN
) -> PrEscalation:
    return PrEscalation(pr=pr, reason=reason, from_label=from_label)


__all__ = [
    "RetryBreaker",
    "PrEscalation",
    "IssueEscalation",
    "retry_breaker",
    "pr_escalation",
    "attention_actions",
]
