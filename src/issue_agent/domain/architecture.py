"""Architecture passes count from the last pass, never ledger history (ADR 0017)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .identity import account_eq

if TYPE_CHECKING:
    from .world import IssueView

PASS_PREFIX = "Architecture pass"


@dataclass(frozen=True)
class NoPass:
    """The pass search answered that no bot-authored pass exists."""


@dataclass(frozen=True)
class PassInFlight:
    issue: IssueView


@dataclass(frozen=True)
class PassEnded:
    number: int
    merged_prs: int

    def __post_init__(self) -> None:
        if type(self.number) is not int or not 0 < self.number <= 2**63 - 1:
            raise ValueError("a pass requires a bounded positive issue number")
        if type(self.merged_prs) is not int or not 0 <= self.merged_prs <= 2**63 - 1:
            raise ValueError("merged PR count must be a bounded non-negative integer")


ArchitectureObservation = NoPass | PassInFlight | PassEnded


def architecture_pass(issue: IssueView, fix_bot: str) -> bool:
    return (account_eq(issue.author, fix_bot)
            and isinstance(issue.title, str) and len(issue.title) <= 256
            and issue.title.startswith(PASS_PREFIX))


def architecture_owed(observed: object) -> bool:
    """Unknown facts defer; only merges strictly after the last pass count."""
    return isinstance(observed, NoPass) or isinstance(observed, PassEnded) and observed.merged_prs >= 5
