"""The Action ADT.

Decision functions (`decide_fix`, `decide_review`, `decide_steward`) are pure:
they take a `WorldState` (plus config) and return an ordered list of `Action`s.
The orchestration layer (Phase 3+) interprets each Action against adapters
(GitHub, AgentRunner, worktree). Keeping the decision as data — not direct
side-effects — is what makes the whole pipeline unit-testable: a test asserts
on the returned Actions, never on a mock call log.

Each Action is a frozen dataclass; the union is `Action`. Pattern-match on type
in the executor. `Relabel`/`PostComment`/`CloseIssue` are leaf side-effects;
`RunAgent` carries an `AgentKind` telling the executor which agent task to build
(the actual SDK invocation lives in the adapter, not here).
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
    """Which headless agent task the executor should run. Maps 1:1 onto the
    bash scripts being replaced."""

    RESOLVE = auto()         # resolve-issue.sh
    # The cheap difficulty classifier the resolve family runs BEFORE the resolve
    # itself (#8/#16). Never an Action — nothing dispatches it, so it has no
    # handler in `Dispatcher.dispatch`; it exists so the leg has a name of its
    # own in the journal and the ledger. Without it the classify probe recorded
    # `kind=RESOLVE` (it inherits the dispatch's context), which inflated the
    # RESOLVE bucket by one ~$0.07 entry per routed dispatch and made a
    # 4-second classify indistinguishable from a completed resolve (#40).
    ROUTE = auto()
    RESUME = auto()          # resume-issue.sh
    ADDRESS_REVIEW = auto()  # address-review.sh
    FIX_CI = auto()          # fix-ci.sh
    REBASE = auto()          # wake the fixer to rebase a CONFLICTING PR onto base
    REVIEW = auto()          # review-pr.sh
    RE_REVIEW = auto()       # re-review-pr.sh
    TRIAGE = auto()          # steward triage_once (per issue)
    STEWARD_DRIVE = auto()   # steward drive_once
    JIRA_SYNC = auto()       # steward: mirror an issue's lifecycle into its Jira ticket


@dataclass(frozen=True)
class Relabel:
    target: Target
    number: int
    remove: Label | None
    add: Label | None


@dataclass(frozen=True)
class PostComment:
    target: Target
    number: int
    body: str


@dataclass(frozen=True)
class CloseIssue:
    number: int
    reason: str = "completed"
    comment: str = ""


@dataclass(frozen=True)
class EnableAutoMerge:
    pr: int
    method: str = "squash"


@dataclass(frozen=True)
class UpdateBranch:
    """Bring a BEHIND (clean-but-stale) PR branch up to date with its base, so an
    already-armed auto-merge can complete. Cheap GitHub-side update (no worktree /
    agent). Only for MERGEABLE+BEHIND PRs — a CONFLICTING branch takes the REBASE
    agent path instead."""

    pr: int


@dataclass(frozen=True)
class RerunFailedChecks:
    """Ask GitHub to re-run this PR's FAILED check runs, instead of paying for a
    FIX_CI leg (#38). The cheap first hypothesis for a red check: the failure is
    a flake, not the diff.

    Costs one CI cycle and no tokens; a repair leg costs $13–29 plus the review
    tail its push triggers, and ≈$80 of one $188 batch went to two legs that both
    "fixed" the same pre-existing flaky test neither PR had touched.

    `head` is the PR head SHA the re-run is spent on. It is carried in the action
    (not re-read at execution time) so the marker the executor writes keys on the
    SAME commit the decision was made about — a push landing mid-tick then reads
    as a NEW head with its own free re-run, rather than silently consuming the
    new head's."""

    pr: int
    head: str


@dataclass(frozen=True)
class RunAgent:
    """Dispatch a headless agent. `number` is the issue (RESOLVE/RESUME/TRIAGE)
    or PR (ADDRESS_REVIEW/FIX_CI/REVIEW/RE_REVIEW); STEWARD_DRIVE ignores it."""

    kind: AgentKind
    number: int = 0


@dataclass(frozen=True)
class AddDependency:
    """Create a native GitHub 'blocked by' dependency: `issue` is blocked by
    `blocker`. The steward emits this when triage finds `issue` cannot proceed
    until work tracked by an ALREADY-OPEN `blocker` issue lands. The fixer's
    existing dependency gate (has_open_blocker → pick_ready_issue) then holds
    `issue` until `blocker` closes. Adapter is fail-soft (already-linked / missing
    blocker must not crash the poll)."""

    issue: int
    blocker: int


@dataclass(frozen=True)
class RemoveWorktree:
    issue: int


@dataclass(frozen=True)
class ClearIssueState:
    """Retire STATE_DIR files for a resolved PR (review M4/D8: `Store.clear_issue`
    /`clear_pr_review` had ZERO production call sites — a closed-unmerged PR
    requeued its issue while issue-N.session/.phase survived, so the next
    lifecycle resumed a dead PR's session with a preloaded breaker). Emitted by
    `domain.fix.cleanup_actions` alongside RemoveWorktree/Relabel, one per PR
    cleanup, and applied by the Executor through an injected callable — a
    second Action rather than a flag on RemoveWorktree so existing
    RemoveWorktree equality assertions elsewhere are undisturbed.

    `merged=True`  → full retirement: all issue state (incl. infra-deaths) AND
                     the PR's own state (incl. the pr-N.automerge marker).
    `merged=False` → closed-unmerged requeue: only the resume-blocking markers
                     on the issue clear; resolve-retries/infra-deaths persist
                     on purpose (handover §5.7 conservatism).
    `issue` is None for an issueless PR — only the PR's own state is retired.
    `pr` is None for the mirror case (#28): the issue-driven cleanup arm repairs
    an issue whose PR is no longer observable anywhere, so there is no PR number
    to retire state under — only the issue's half runs. Exactly one of the two
    may be None.
    """

    pr: int | None
    issue: int | None
    merged: bool


@dataclass(frozen=True)
class NoOp:
    reason: str = ""


Action = Union[
    Relabel,
    PostComment,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    AddDependency,
    RerunFailedChecks,
    RunAgent,
    RemoveWorktree,
    ClearIssueState,
    NoOp,
]

__all__ = [
    "Target",
    "AgentKind",
    "Relabel",
    "PostComment",
    "CloseIssue",
    "EnableAutoMerge",
    "UpdateBranch",
    "AddDependency",
    "RerunFailedChecks",
    "RunAgent",
    "RemoveWorktree",
    "ClearIssueState",
    "NoOp",
    "Action",
]
