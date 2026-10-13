"""The Action ADT: what `domain.policy` emits for a derived state, plus the
paid dispatch. Pure data; `adapters.act` executes each against GitHub, and a
`RunAgent` goes to the role's legs. A test asserts on the returned Actions,
never on a mock call log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Union

from .labels import Label


class Target(Enum):
    ISSUE = auto()
    PR = auto()


class AgentKind(Enum):
    """The paid acts. RESOLVE runs on an issue; the rest on a PR head."""

    RESOLVE = auto()
    ADDRESS_REVIEW = auto()
    FIX_CI = auto()
    REBASE = auto()
    REVIEW = auto()


@dataclass(frozen=True)
class Relabel:
    """Add and/or remove one label. Naturally idempotent; act reads it back."""

    target: Target
    number: int
    remove: Label | None
    add: Label | None


@dataclass(frozen=True)
class PostComment:
    target: Target
    number: int
    body: str
    # Idempotency key: act posts at most one comment per key. Empty = no dedupe.
    key: str = ""


@dataclass(frozen=True)
class MergePullRequest:
    """Merge an approved, green PR now (refactor decision Q10). `head` is the
    SHA the decision saw; act passes it as --match-head-commit so a push landing
    after the decision is never merged unreviewed."""

    pr: int
    head: str

    def __post_init__(self) -> None:
        if type(self.pr) is not int or self.pr <= 0 or not isinstance(self.head, str) or not self.head:
            raise ValueError("merge requires a PR number and the decided head")


@dataclass(frozen=True)
class OpenPullRequest:
    """Open the harness PR for a pushed `fix/<issue>-` branch owed one
    (refactor decision Q11: the agent pushes, the harness opens). Idempotent
    by lookup: act opens only when no open PR has this head."""

    issue: int
    branch: str

    def __post_init__(self) -> None:
        if type(self.issue) is not int or self.issue <= 0 or not isinstance(self.branch, str) or not self.branch:
            raise ValueError("opening a PR requires the issue and its pushed branch")


@dataclass(frozen=True)
class UpdateBranch:
    """Bring a BEHIND (clean-but-stale) PR branch up to date with its base
    (GitHub's update-branch: free, no agent). Bound to the decided head; a
    CONFLICTING branch takes the REBASE leg instead."""

    pr: int
    head: str


@dataclass(frozen=True)
class RunAgent:
    """Dispatch one paid leg. `number` is the issue for RESOLVE, else the PR."""

    kind: AgentKind
    number: int = 0
    # The PR head the dispatch was decided on ("" for issue-stage legs), so the
    # leg and its idempotency key bind to the exact head (e.g. review:{pr}:{head}).
    head: str = ""


Action = Union[
    Relabel,
    PostComment,
    MergePullRequest,
    OpenPullRequest,
    UpdateBranch,
    RunAgent,
]


__all__ = [
    "Action",
    "AgentKind",
    "MergePullRequest",
    "OpenPullRequest",
    "PostComment",
    "Relabel",
    "RunAgent",
    "Target",
    "UpdateBranch",
]
