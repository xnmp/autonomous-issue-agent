"""First-class description of one independently executed workflow leg."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LegTarget(str, Enum):
    ISSUE = "issue"
    PR = "pr"
    REPOSITORY = "repository"


@dataclass(frozen=True)
class Leg:
    kind: str
    target: LegTarget
    number: int | None
    billable: bool
    counts_against: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("leg kind is required")
        if self.target is LegTarget.REPOSITORY:
            if self.number is not None:
                raise ValueError("repository legs do not carry an issue/PR number")
        elif self.number is None or self.number <= 0:
            raise ValueError("issue/PR legs require a positive number")
        if self.billable and not self.counts_against:
            raise ValueError("billable legs must declare their budget scopes")
