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
from .routing import DEFAULT_TIER, Tier


class Target(Enum):
    ISSUE = auto()
    PR = auto()


class AgentKind(Enum):
    """The paid acts. STEWARD, TRIAGE and RESOLVE run on an issue; the rest on a PR head."""

    STEWARD = auto()
    TRIAGE = auto()
    RESOLVE = auto()
    ADDRESS_REVIEW = auto()
    FIX_CI = auto()
    REBASE = auto()
    REVIEW = auto()

    @property
    def holds_dispatch_slot(self) -> bool:
        """Whether a leg of this act occupies and needs a dispatch slot. STEWARD
        and TRIAGE are light (no workspace), so they neither wait behind nor
        crowd out one."""
        return self not in (AgentKind.STEWARD, AgentKind.TRIAGE)


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
class LinkBlockedBy:
    """Write GitHub's "#`issue` is blocked by #`blocker`" link a steward proposed,
    then one comment carrying `reason` (or why act refused the link on its fresh
    reads). Keyed like PostComment: a link a human later deletes is never
    re-added."""

    issue: int
    blocker: int
    reason: str
    key: str

    def __post_init__(self) -> None:
        if (type(self.issue) is not int or type(self.blocker) is not int or self.issue <= 0 or self.blocker <= 0
                or self.issue == self.blocker or not isinstance(self.reason, str) or not self.reason
                or not isinstance(self.key, str) or not self.key):
            raise ValueError("a blocker link needs two distinct issues, a reason and a key")


@dataclass(frozen=True)
class FileIssue:
    """Open one unlabelled issue (whether the harness works on it is the
    owner's call), at most once per key, as PostComment."""

    title: str
    body: str
    key: str


@dataclass(frozen=True)
class SetIssueState:
    """Reopen or close an issue with one comment; act writes only when a fresh
    read shows the other state, then reads it back."""

    number: int
    open: bool
    comment: str


@dataclass(frozen=True)
class RunAgent:
    """Dispatch one paid leg. `number` is the issue for STEWARD, TRIAGE and RESOLVE,
    else the PR."""

    kind: AgentKind
    number: int = 0
    # The PR head the dispatch was decided on ("" for issue-stage legs), so the
    # leg and its idempotency key bind to the exact head (e.g. review:{pr}:{head}).
    head: str = ""
    # The difficulty tier the leg runs at (a fix act's already escalated for
    # this attempt); see `check_tier`.
    tier: Tier | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", check_tier(self.kind, self.tier))


ISSUE_ACTS = frozenset({AgentKind.STEWARD, AgentKind.TRIAGE, AgentKind.RESOLVE})
ROUTED_ACTS = frozenset({AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.ADDRESS_REVIEW, AgentKind.REBASE})


def check_tier(kind: AgentKind, tier: Tier | None) -> Tier | None:
    """A routed fix act always has a tier (DEFAULT_TIER when none is given);
    REVIEW may (the tier of the leg that pushed its head; None falls back to
    REVIEW_MODEL); STEWARD and TRIAGE never do."""
    if tier is not None and not isinstance(tier, Tier):
        raise ValueError("tier must be a Tier")
    if kind in ROUTED_ACTS:
        return tier or DEFAULT_TIER
    if tier is not None and kind is not AgentKind.REVIEW:
        raise ValueError(f"{kind.name} carries no tier")
    return tier


Action = Union[
    Relabel,
    PostComment,
    MergePullRequest,
    OpenPullRequest,
    UpdateBranch,
    LinkBlockedBy,
    FileIssue,
    SetIssueState,
    RunAgent,
]


__all__ = [
    "Action",
    "AgentKind",
    "ISSUE_ACTS",
    "ROUTED_ACTS",
    "check_tier",
    "FileIssue",
    "LinkBlockedBy",
    "MergePullRequest",
    "OpenPullRequest",
    "PostComment",
    "Relabel",
    "RunAgent",
    "SetIssueState",
    "Target",
    "UpdateBranch",
]
