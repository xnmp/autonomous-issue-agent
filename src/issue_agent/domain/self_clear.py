"""Pure, observed-world decisions for bounded machine-checkable block retirement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from zoneinfo import ZoneInfo

from .actions import (
    Action,
    DiscardSelfClearMarker,
    FinalizeBudgetBlockRetirement,
    FinalizeMachineBlockRetirement,
    Relabel,
    ReportSelfClearCeiling,
    Target,
)
from .ci import ci_state
from .contracts import Money, BudgetScope, daily_budget_scope
from .labels import Label
from .world import CIState, WorldState, PRView
from .workflow_identity import IssueRef, WorkflowRevision, WorkflowBinding, WorkflowFamily, validate_workflow_integer, validate_workflow_text

SELF_CLEAR_CEILING = 3


class MachineBlocker(str, Enum):
    PUBLICATION = "publication"
    CHECKS = "checks"


@dataclass(frozen=True)
class MachineBlock:
    issue: int
    target: Target
    number: int
    pr: int
    blocker: MachineBlocker
    blocked_head: str

    def __post_init__(self) -> None:
        for value in (self.issue, self.number, self.pr):
            validate_workflow_integer(value, "machine block target", minimum=1)
        if self.number != self.pr:
            raise ValueError("machine block mutation must name its selected PR")
        if self.target is not Target.PR:
            raise ValueError("machine-checkable fixer blocks currently target PRs")
        if not isinstance(self.blocker, MachineBlocker):
            raise ValueError("machine block requires a recognized blocker selector")
        validate_workflow_text(self.blocked_head, "machine blocked head", limit=256)


@dataclass(frozen=True)
class BudgetBlock:
    issue: int
    target: Target
    number: int
    reservation_micros: int
    scope: str
    cap_micros: int
    old_headroom_micros: int | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        for value in (self.issue, self.number):
            validate_workflow_integer(value, "budget block target", minimum=1)
        for value in (self.reservation_micros, self.cap_micros):
            validate_workflow_integer(value, "budget block money")
        if self.old_headroom_micros is not None:
            validate_workflow_integer(self.old_headroom_micros, "budget block headroom")
        validate_workflow_text(self.scope, "budget block scope", limit=512)
        if not isinstance(self.target, Target) or self.target is Target.ISSUE and self.number != self.issue:
            raise ValueError("budget block mutation must name its selected target")
        if not isinstance(self.currency, str):
            raise ValueError("budget block requires a typed currency")
        Money(self.currency, 0)


@dataclass(frozen=True)
class BudgetLimitObservation:
    scope: str
    disposition: str
    committed_micros: int
    cap_micros: int
    currency: str = "USD"
    budget_scope: BudgetScope | None = None

    def __post_init__(self) -> None:
        validate_workflow_integer(self.committed_micros, "budget observation committed amount")
        validate_workflow_integer(self.cap_micros, "budget observation cap")
        validate_workflow_text(self.scope, "budget observation scope", limit=512)
        validate_workflow_text(self.disposition, "budget observation disposition", limit=64)
        Money(self.currency, 0)
        if self.budget_scope is not None and (not isinstance(self.budget_scope, BudgetScope)
                or self.budget_scope.dimension != self.scope or self.budget_scope.currency != self.currency):
            raise ValueError("budget observation requires its exact accounting scope")


@dataclass(frozen=True)
class BudgetWorld:
    """Complete current admission limits for one issue; absent is unknown."""

    limits: tuple[BudgetLimitObservation, ...] = ()
    complete: bool = False
    config_version: str = ""

    def __post_init__(self) -> None:
        if (type(self.complete) is not bool or not isinstance(self.limits, tuple)
                or any(not isinstance(limit, BudgetLimitObservation) for limit in self.limits)):
            raise ValueError("budget world requires typed limits and completeness")
        validate_workflow_text(self.config_version, "budget world config", limit=512, empty=True)


def budget_recovery_limits(repository: str, issue: int, now: datetime,
        repository_cap: Money | None, global_cap: Money | None,
        issue_cap: Money | None) -> tuple[tuple[BudgetScope, Money], ...]:
    limits = []
    for repo, dimension, cap in ((repository, "repository", repository_cap), ("*", "global", global_cap)):
        if cap is not None:
            limits.append((daily_budget_scope(repo, dimension, cap.currency, now, ZoneInfo("UTC")), cap))
    if issue_cap is not None:
        limits.append((BudgetScope(repository, f"issue:{issue}", issue_cap.currency, f"lifetime:{issue}",
            datetime(1970, 1, 1, tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc), ZoneInfo("UTC")), issue_cap))
    return tuple(limits)


@dataclass(frozen=True)
class MachineClearProjection:
    revision: WorkflowRevision
    block: MachineBlock
    observed_head: str
    ignored_checks: str = ""
    binding: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        _validate_clear_binding(self.revision, self.binding)
        if not isinstance(self.revision, WorkflowRevision) or not isinstance(self.block, MachineBlock):
            raise ValueError("machine clear requires selected typed recovery evidence")
        if (not isinstance(self.revision.fence.key.target, IssueRef)
                or self.revision.fence.key.target.number != self.block.issue or self.observed_head != self.block.blocked_head):
            raise ValueError("machine clear evidence does not match its issue/head")
        validate_workflow_text(self.ignored_checks, "machine clear ignored check policy", empty=True)
        try:
            re.compile(self.ignored_checks)
        except re.error as exc:
            raise ValueError("machine clear ignored check policy is malformed") from exc


@dataclass(frozen=True)
class BudgetClearProjection:
    revision: WorkflowRevision
    block: BudgetBlock
    budget: BudgetWorld
    binding: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        _validate_clear_binding(self.revision, self.binding)
        if not isinstance(self.revision, WorkflowRevision) or not isinstance(self.block, BudgetBlock):
            raise ValueError("budget clear requires selected typed recovery evidence")
        if not isinstance(self.revision.fence.key.target, IssueRef) or self.revision.fence.key.target.number != self.block.issue:
            raise ValueError("budget clear evidence does not match its issue")
        if not isinstance(self.budget, BudgetWorld) or not _budget_clearable(self.budget, self.block.reservation_micros)[0]:
            raise ValueError("budget clear lacks complete fitting captured limits")


def _validate_clear_binding(revision, binding) -> None:
    if binding is not None and (not isinstance(binding, WorkflowBinding) or not isinstance(revision, WorkflowRevision)
            or binding.fence != revision.fence or binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
        raise ValueError("recovery projection binding must name its selected issue family and generation")


def machine_clear_evidence(pr: PRView, block: MachineBlock, ignored_checks: str) -> bool:
    if pr.state != "OPEN" or not pr.head_ref_oid or pr.head_ref_oid != block.blocked_head:
        return False
    return block.blocker is MachineBlocker.PUBLICATION or ci_state(
        pr.checks, ignored_checks, known=pr.checks_known) is CIState.OK


def _machine_clearable(
    world: WorldState, block: MachineBlock, ignored_checks: str
) -> tuple[bool, str]:
    pr = next(
        (
            candidate
            for candidate in world.prs
            if candidate.number == block.pr and candidate.state == "OPEN"
        ),
        None,
    )
    if pr is None or not pr.has(Label.AI_BLOCKED) or not pr.head_ref_oid:
        return False, ""
    return machine_clear_evidence(pr, block, ignored_checks), pr.head_ref_oid


def _budget_clearable(
    budget: BudgetWorld, reservation_micros: int
) -> tuple[bool, BudgetLimitObservation | None, int | None]:
    if not budget.complete or not budget.limits:
        return False, None, None
    tightest: BudgetLimitObservation | None = None
    tightest_headroom: int | None = None
    for limit in budget.limits:
        if limit.disposition != "available":
            return False, None, None
        headroom = max(0, limit.cap_micros - limit.committed_micros)
        if tightest_headroom is None or headroom < tightest_headroom:
            tightest = limit
            tightest_headroom = headroom
        if limit.committed_micros + reservation_micros > limit.cap_micros:
            return False, tightest, tightest_headroom
    return True, tightest, tightest_headroom


def self_clear_actions(
    world: WorldState,
    machine_blocks: dict[int, MachineBlock],
    budget_blocks: dict[int, BudgetBlock],
    budget_worlds: dict[int, BudgetWorld],
    clear_counts: dict[int, int],
    ceiling_reported: frozenset[int],
    ignored_checks: str = "",
    ceiling: int = SELF_CLEAR_CEILING,
    machine_revisions: dict[int, WorkflowRevision] | None = None,
    budget_revisions: dict[int, WorkflowRevision] | None = None,
    self_clear_bindings: dict[int, WorkflowBinding] | None = None,
) -> list[Action]:
    """Return only clears proven by the current snapshot; absence never clears."""

    actions: list[Action] = []
    for issue, block in machine_blocks.items():
        observed_target = next(
            (candidate for candidate in world.prs if candidate.number == block.pr),
            None,
        )
        if observed_target is not None and not observed_target.has(Label.AI_BLOCKED):
            actions.append(DiscardSelfClearMarker(issue, "machine", (machine_revisions or {}).get(issue)))
            continue
        clearable, observed_head = _machine_clearable(world, block, ignored_checks)
        if not clearable:
            continue
        count = max(0, clear_counts.get(issue, 0))
        if count >= ceiling:
            if issue not in ceiling_reported:
                actions.append(ReportSelfClearCeiling(
                    issue, block.target, block.number, block.blocker.value, ceiling,
                    (machine_revisions or {}).get(issue),
                ))
            continue
        selected_revision = (machine_revisions or {}).get(issue)
        actions.append(Relabel(
                block.target,
                block.number,
                Label.AI_BLOCKED,
                Label.AI_PR_OPEN,
                observed_head,
                self_clear=MachineClearProjection(selected_revision, block, observed_head, ignored_checks,
                    (self_clear_bindings or {}).get(issue))
                if selected_revision is not None else None,
            ))
        if selected_revision is None:
            actions.append(FinalizeMachineBlockRetirement(
                issue,
                block.target,
                block.number,
                block.blocker.value,
                block.blocked_head,
                observed_head,
                (machine_revisions or {}).get(issue),
            ))

    # Both causes share one component revision and allowance. A second mutation
    # needs a fresh snapshot of the first retirement's revision and count.
    machine_selected = {action.issue for action in actions if isinstance(action, (
        FinalizeMachineBlockRetirement, DiscardSelfClearMarker, ReportSelfClearCeiling,
    ))}
    machine_selected.update(action.self_clear.block.issue for action in actions
        if isinstance(action, Relabel) and action.self_clear is not None)
    issues = {candidate.number: candidate for candidate in world.issues}
    prs = {candidate.number: candidate for candidate in world.prs}
    for issue, block in budget_blocks.items():
        if issue in machine_selected:
            continue
        sibling = machine_blocks.get(issue)
        if sibling is not None and sibling.target is block.target and sibling.number == block.number:
            # ai-blocked is shared remote projection. Budget recovery cannot
            # remove an unresolved machine cause on the same target.
            continue
        observed = (
            issues.get(block.number)
            if block.target is Target.ISSUE
            else prs.get(block.number)
        )
        if observed is None:
            continue
        blocked = observed.has(Label.AI_BLOCKED)
        budget_label = observed.has(Label.NEEDS_BUDGET_REVIEW)
        if not blocked and not budget_label:
            actions.append(DiscardSelfClearMarker(issue, "budget", (budget_revisions or {}).get(issue)))
            continue
        clearable, tightest, new_headroom = _budget_clearable(
            budget_worlds.get(issue, BudgetWorld()), block.reservation_micros
        )
        if not clearable:
            continue
        count = max(0, clear_counts.get(issue, 0))
        if count >= ceiling:
            if issue not in ceiling_reported:
                actions.append(ReportSelfClearCeiling(
                    issue, block.target, block.number, "budget", ceiling,
                    (budget_revisions or {}).get(issue),
                ))
            continue
        revision = observed.updated_at if block.target is Target.ISSUE else None
        resume_label = (
            Label.AI_READY if block.target is Target.ISSUE else Label.AI_PR_OPEN
        )
        removes = tuple(
            label
            for label, present in (
                (Label.NEEDS_BUDGET_REVIEW, budget_label),
                (Label.AI_BLOCKED, blocked),
            )
            if present
        )
        selected_revision = (budget_revisions or {}).get(issue)
        actions.append(Relabel(
            block.target,
            block.number,
            None,
            resume_label,
            revision,
            removes=removes,
            self_clear=BudgetClearProjection(selected_revision, block, budget_worlds[issue],
                (self_clear_bindings or {}).get(issue)) if selected_revision is not None else None,
        ))
        if selected_revision is None:
            actions.append(FinalizeBudgetBlockRetirement(
            issue,
            block.target,
            block.number,
            tightest.scope if tightest is not None else block.scope,
            block.old_headroom_micros,
            new_headroom,
            block.reservation_micros,
            tightest.cap_micros if tightest is not None else None,
            tightest.currency if tightest is not None else block.currency,
            (budget_revisions or {}).get(issue),
            ))
    return actions
