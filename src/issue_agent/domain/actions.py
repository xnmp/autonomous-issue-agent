"""The Action ADT.

Decision functions (`decide_fix`, `decide_bound_review`, `decide_steward`) are pure:
they take a `WorldState` (plus config) and return an ordered list of `Action`s.
The orchestration layer interprets each Action against adapters
(GitHub, AgentRunner, worktree). Keeping the decision as data — not direct
side-effects — is what makes the whole pipeline unit-testable: a test asserts
on the returned Actions, never on a mock call log.

Each Action is a frozen dataclass; the union is `Action`. Pattern-match on type
in the executor. `Relabel`/`PostComment`/`CloseIssue` are leaf side-effects;
`RunAgent` carries an `AgentKind` telling the executor which agent task to build
(the actual SDK invocation lives in the adapter, not here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Union

from .eligibility import IssueCompletionGuard, IssueHoldGuard, LifecycleOperation
from .labels import Label
from .workflow_identity import IssueRef, PullRequestRef, WorkflowBinding


class Target(Enum):
    ISSUE = auto()
    PR = auto()


def _validate_action_workflow(workflow: WorkflowBinding | None, target: Target, number: int) -> None:
    if workflow is None:
        return
    if not isinstance(workflow, WorkflowBinding):
        raise ValueError("action requires a validated workflow binding")
    if target is Target.ISSUE and workflow.fence.key.target != IssueRef(number):
        raise ValueError("issue action disagrees with its captured workflow")


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
    PR_RESUME = auto()       # resume a PR-stage block with its issue session
    ADDRESS_REVIEW = auto()  # address-review.sh
    FIX_CI = auto()          # fix-ci.sh
    REBASE = auto()          # wake the fixer to rebase a CONFLICTING PR onto base
    REVIEW = auto()          # review-pr.sh
    RE_REVIEW = auto()       # re-review-pr.sh
    TRIAGE = auto()          # steward triage_once (per issue)
    GROOM = auto()           # steward bounded issue grooming (per issue)
    STEWARD_DRIVE = auto()   # steward drive_once
    JIRA_SYNC = auto()       # steward: mirror an issue's lifecycle into its Jira ticket


@dataclass(frozen=True)
class Relabel:
    target: Target
    number: int
    remove: Label | None
    add: Label | None
    # None lets compatibility actions bind at command creation. An explicit
    # empty string means the observation lacked a revision and must fail closed.
    expected_revision: str | None = None
    # ADR 0012 — harness-owned signal. This write reports the HARNESS's own
    # state (an escalation, a ceiling trip), not a claim about the target's
    # content, so it is minted WITHOUT an optimistic revision guard: a comment
    # arriving between decide and execute must not silently discard the
    # escalation that explains why the harness stopped. Delivery stays bounded
    # by the durable retry ceiling, and the label/body is fixed text the
    # harness authored — never agent- or user-supplied. Do NOT set it on a
    # write whose correctness depends on the target being unchanged.
    harness_signal: bool = False
    # Additional removals are part of this same durable lifecycle command.
    # This is intentionally not represented as multiple Relabel actions: they
    # would share one observed revision, so the first mutation would stale the
    # second command and could strand a half-completed label transition.
    removes: tuple[Label, ...] = ()
    completion_guard: IssueCompletionGuard | None = None
    workflow: WorkflowBinding | None = None
    adds: tuple[Label, ...] = ()
    comment: str = ""
    # ADR 0015 semantic fence for a workflow hold projection. When present it,
    # not the raw `expected_revision` (kept as the accepted observation for
    # audit), decides whether this write still applies: comments and body
    # edits cannot supersede it, while hold/terminal/intervention labels, a
    # close/reopen or a linked PR do.
    hold_guard: IssueHoldGuard | None = None

    def __post_init__(self) -> None:
        _validate_action_workflow(self.workflow, self.target, self.number)
        if not isinstance(self.adds, tuple) or any(not isinstance(label, Label) for label in self.adds):
            raise ValueError("additional labels must be a tuple of labels")
        if self.hold_guard is not None and (
            not isinstance(self.hold_guard, IssueHoldGuard) or self.target is not Target.ISSUE
            or self.harness_signal or self.completion_guard is not None or self.workflow is None
        ):
            raise ValueError("a hold guard fences only a workflow-bound issue projection")
        if not isinstance(self.comment, str) or len(self.comment) > 65536 or "\x00" in self.comment:
            raise ValueError("projection comment must be bounded text")
        if self.adds or self.comment:
            if self.target is not Target.ISSUE or self.harness_signal or self.completion_guard is None or not self.expected_revision:
                raise ValueError("compound projection requires a guarded issue revision")
            if set(self.added_labels) & set(self.removed_labels):
                raise ValueError("projection cannot add and remove the same label")

    @property
    def removed_labels(self) -> tuple[Label, ...]:
        return ((self.remove,) if self.remove is not None else ()) + self.removes

    @property
    def added_labels(self) -> tuple[Label, ...]:
        return ((self.add,) if self.add is not None else ()) + self.adds


@dataclass(frozen=True)
class PostComment:
    target: Target
    number: int
    body: str
    # See Relabel.harness_signal.
    harness_signal: bool = False
    workflow: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        _validate_action_workflow(self.workflow, self.target, self.number)


@dataclass(frozen=True)
class CloseIssue:
    number: int
    reason: str = "completed"
    comment: str = ""
    expected_revision: str | None = None
    completion_guard: IssueCompletionGuard | None = None
    remove_labels: tuple[Label, ...] = ()
    add_labels: tuple[Label, ...] = ()
    operation: LifecycleOperation = LifecycleOperation.CLOSE
    workflow: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        _validate_action_workflow(self.workflow, Target.ISSUE, self.number)


@dataclass(frozen=True)
class EnableAutoMerge:
    pr: int
    method: str
    head: str


@dataclass(frozen=True)
class UpdateBranch:
    """Bring a BEHIND (clean-but-stale) PR branch up to date with its base, so an
    already-armed auto-merge can complete. Cheap GitHub-side update (no worktree /
    agent). Only for MERGEABLE+BEHIND PRs — a CONFLICTING branch takes the REBASE
    agent path instead."""

    pr: int
    head: str
    # Base tip observed when this refresh was decided ("" = not collected).
    # Part of the once-per-attempt marker key: the advisory-UNSTABLE arm must
    # re-arm when the base advances (that is the event it exists for), and
    # GraphQL's baseRefOid cannot see one.
    base: str = ""


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
    run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CancelWorkflowRuns:
    """Cancel the workflow runs that have WEDGED this PR's CI.

    GitHub refuses `rerun-failed-jobs` on a run that is still `in_progress`
    (HTTP 403 "already running"), so for a wedged run the harness's whole re-run
    capability was dead on arrival: the pending deadline fired, nothing could be
    re-run, and the PR escalated to a human who then did exactly this by hand
    (keifu PR #177 — a `Test windows-latest` job pinned `in_progress` for 60+
    minutes; cancel → wait for completed → rerun-failed-jobs went green).

    Cancelling is the step that makes the run rerunnable. It is NOT a separate
    budget: cancel + the re-run it unblocks together spend the ONE free re-run
    this head is allowed (`FixState.ci_rerun_head`), so a head that wedges twice
    still reaches a human on schedule.

    `head` is the PR head SHA observed when the decision was made and is carried
    in the action rather than re-read at execution time: the adapter refuses to
    cancel any run whose `head_sha` differs, so a push landing mid-tick can never
    have its fresh run cancelled by a decision made about the old commit.
    """

    pr: int
    head: str
    run_ids: tuple[int, ...] = ()
    # The poll clock this cancel was decided on, carried so the marker the
    # executor writes can date it. The wait for GitHub to take the run down is
    # then bounded by time SINCE THE CANCEL — the stall clock cannot bound it,
    # because cancelling a wedged run is exactly what lets follow-on jobs start,
    # and a fresh pending check resets that clock (adversarial review S2).
    decided_at: str = ""


@dataclass(frozen=True)
class EscalateCiPending:
    """One-shot escalation for a stalled CI head.

    Kept as one action so the executor can verify the label write before it
    persists the `(PR, head)` latch. Splitting comment/relabel/marker into
    independent actions lets a failed relabel permanently suppress its retry.
    """

    pr: int
    head: str
    body: str


@dataclass(frozen=True)
class RunAgent:
    """Dispatch a headless agent. `number` is the issue (RESOLVE/RESUME/TRIAGE)
    or PR (PR_RESUME/ADDRESS_REVIEW/FIX_CI/REVIEW/RE_REVIEW); STEWARD_DRIVE
    ignores it."""

    kind: AgentKind
    number: int = 0
    requires: Relabel | None = field(default=None, compare=False, repr=False)
    workflow: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        if self.workflow is None:
            return
        if not isinstance(self.workflow, WorkflowBinding):
            raise ValueError("agent workflow must be a validated binding")
        if self.kind in (AgentKind.RESOLVE, AgentKind.RESUME) and (
            self.workflow.fence.key.target != IssueRef(self.number)
        ):
            raise ValueError("resolve dispatch must retain its workflow issue identity")
        if self.kind in (AgentKind.REVIEW, AgentKind.RE_REVIEW) and (
            self.workflow.fence.key.target != PullRequestRef(self.number)
        ):
            raise ValueError("review dispatch must retain its workflow pull request identity")


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
class ResetCiRetries:
    """Close a red-CI episode: the PR's checks settled GREEN, so the FIX_CI
    circuit breaker starts the next episode from zero.

    Without this the counter was lifetime-per-issue — a PR that went red,
    was repaired, went green, then went red again later (base drift, a new
    review round's push) inherited the old count and could escalate on the
    FIRST attempt of the new episode, contradicting the escalation text
    "without going green" (latent class found via keifu PR #181,
    2026-08-03; `review_retries` solved the same class with its
    submitted_at episode key)."""

    issue: int


@dataclass(frozen=True)
class FinalizeMachineBlockRetirement:
    """Commit local self-clear state after the observed label move landed."""

    issue: int
    target: Target
    number: int
    blocker: str
    blocked_head: str
    observed_head: str


@dataclass(frozen=True)
class FinalizeBudgetBlockRetirement:
    """Commit local budget self-clear state after both labels were removed."""

    issue: int
    target: Target
    number: int
    scope: str
    old_headroom_micros: int | None
    new_headroom_micros: int | None
    reservation_micros: int
    cap_micros: int | None
    currency: str


@dataclass(frozen=True)
class ReportSelfClearCeiling:
    """One-shot journal edge when a fourth automatic clear is withheld."""

    issue: int
    target: Target
    number: int
    blocker: str
    ceiling: int


@dataclass(frozen=True)
class DiscardSelfClearMarker:
    """Retire a marker after presence proves its remote block is already gone."""

    issue: int
    marker: str


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
    CancelWorkflowRuns,
    EscalateCiPending,
    RunAgent,
    RemoveWorktree,
    ClearIssueState,
    ResetCiRetries,
    FinalizeMachineBlockRetirement,
    FinalizeBudgetBlockRetirement,
    ReportSelfClearCeiling,
    DiscardSelfClearMarker,
    NoOp,
]

def is_harness_signal(action: object) -> bool:
    """Whether this action is a harness-owned signal exempt from the optimistic
    revision guard (see `Relabel.harness_signal`). Total by construction: an
    action type that has no such field is never exempt."""
    return bool(getattr(action, "harness_signal", False))


__all__ = [
    "Target",
    "is_harness_signal",
    "AgentKind",
    "Relabel",
    "PostComment",
    "CloseIssue",
    "EnableAutoMerge",
    "UpdateBranch",
    "AddDependency",
    "RerunFailedChecks",
    "CancelWorkflowRuns",
    "EscalateCiPending",
    "RunAgent",
    "RemoveWorktree",
    "ClearIssueState",
    "ResetCiRetries",
    "FinalizeMachineBlockRetirement",
    "FinalizeBudgetBlockRetirement",
    "ReportSelfClearCeiling",
    "DiscardSelfClearMarker",
    "NoOp",
    "Action",
]
