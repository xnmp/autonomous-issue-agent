"""Observed issue holds joined into PR review eligibility; ADR 0015."""

from dataclasses import dataclass

from .labels import Label
from .workflow_identity import MAX_DETAIL_CHARS, validate_workflow_text
from .world import IssueView


@dataclass(frozen=True)
class NoLinkedIssue:
    """The complete PR view names no issue under the shared linkage rule."""


@dataclass(frozen=True)
class ObservedLinkedIssue:
    issue: IssueView
    workflow_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.issue, IssueView) or type(self.issue.number) is not int
                or not 0 < self.issue.number < 2**63 or not isinstance(self.issue.state, str)
                or self.issue.state.lower() not in {"open", "closed"}
                or type(self.issue.blocked_by_open) is not bool
                or not isinstance(self.issue.labels, frozenset)
                or any(not isinstance(label, Label) for label in self.issue.labels)):
            raise ValueError("linked issue requires a complete, matching issue observation")
        if not isinstance(self.workflow_holds, tuple) or len(self.workflow_holds) > 32:
            raise ValueError("linked workflow holds must be bounded and immutable")
        for hold in self.workflow_holds:
            validate_workflow_text(hold, "linked workflow hold", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class LinkedIssueUnavailable:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "linked issue read failure", limit=MAX_DETAIL_CHARS)


LinkedIssueRead = NoLinkedIssue | ObservedLinkedIssue | LinkedIssueUnavailable
_HOLD_LABELS = (Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_BUDGET_REVIEW)


def linked_issue_holds(read: NoLinkedIssue | ObservedLinkedIssue) -> tuple[str, ...]:
    if isinstance(read, NoLinkedIssue):
        return ()
    if not isinstance(read, ObservedLinkedIssue):
        raise ValueError("linked issue holds require observed facts; unavailable is not absence")
    issue = read.issue
    holds = tuple(label.value for label in _HOLD_LABELS if label in issue.labels)
    if issue.state.lower() == "closed":
        holds += ("closed",)
    if issue.blocked_by_open:
        holds += ("open dependency",)
    return tuple(f"linked issue #{issue.number}: {hold}" for hold in holds + read.workflow_holds)
