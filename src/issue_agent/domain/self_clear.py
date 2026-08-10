"""Pure, observed-world decisions for bounded machine-checkable block retirement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
from .labels import Label
from .world import CIState, WorldState

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
        if min(self.issue, self.number, self.pr) <= 0:
            raise ValueError("machine block identifiers must be positive")
        if self.target is not Target.PR:
            raise ValueError("machine-checkable fixer blocks currently target PRs")
        if not isinstance(self.blocker, MachineBlocker):
            raise ValueError("machine block requires a recognized blocker selector")
        if not self.blocked_head:
            raise ValueError("machine block requires the observed blocked head")


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
        if min(self.issue, self.number) <= 0:
            raise ValueError("budget block identifiers must be positive")
        if self.reservation_micros < 0:
            raise ValueError("budget reservation cannot be negative")
        if not self.scope:
            raise ValueError("budget block requires its admission scope")
        if self.cap_micros < 0:
            raise ValueError("budget cap cannot be negative")
        if self.old_headroom_micros is not None and self.old_headroom_micros < 0:
            raise ValueError("budget headroom cannot be negative")


@dataclass(frozen=True)
class BudgetLimitObservation:
    scope: str
    disposition: str
    committed_micros: int
    cap_micros: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.committed_micros < 0 or self.cap_micros < 0:
            raise ValueError("budget observations cannot be negative")


@dataclass(frozen=True)
class BudgetWorld:
    """Complete current admission limits for one issue; absent is unknown."""

    limits: tuple[BudgetLimitObservation, ...] = ()
    complete: bool = False


def _machine_clearable(
    world: WorldState, block: MachineBlock, ignored_checks: tuple[str, ...]
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
    if pr.head_ref_oid != block.blocked_head:
        return False, ""
    if block.blocker is MachineBlocker.PUBLICATION:
        # The agent-written blocker only selects this predicate. Authorization
        # comes from observing the exact head that the recorded leg could not
        # publish, not from trusting the classification itself.
        return True, pr.head_ref_oid
    return (
        ci_state(pr.checks, ignored_checks, known=pr.checks_known) is CIState.OK,
        pr.head_ref_oid,
    )


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
    ignored_checks: tuple[str, ...] = (),
    ceiling: int = SELF_CLEAR_CEILING,
) -> list[Action]:
    """Return only clears proven by the current snapshot; absence never clears."""

    actions: list[Action] = []
    for issue, block in machine_blocks.items():
        observed_target = next(
            (candidate for candidate in world.prs if candidate.number == block.pr),
            None,
        )
        if observed_target is not None and not observed_target.has(Label.AI_BLOCKED):
            actions.append(DiscardSelfClearMarker(issue, "machine"))
            continue
        clearable, observed_head = _machine_clearable(world, block, ignored_checks)
        if not clearable:
            continue
        count = max(0, clear_counts.get(issue, 0))
        if count >= ceiling:
            if issue not in ceiling_reported:
                actions.append(ReportSelfClearCeiling(
                    issue, block.target, block.number, block.blocker.value, ceiling
                ))
            continue
        actions.extend([
            Relabel(
                block.target,
                block.number,
                Label.AI_BLOCKED,
                Label.AI_PR_OPEN,
                observed_head,
            ),
            FinalizeMachineBlockRetirement(
                issue,
                block.target,
                block.number,
                block.blocker.value,
                block.blocked_head,
                observed_head,
            ),
        ])

    issues = {candidate.number: candidate for candidate in world.issues}
    prs = {candidate.number: candidate for candidate in world.prs}
    for issue, block in budget_blocks.items():
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
            actions.append(DiscardSelfClearMarker(issue, "budget"))
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
                    issue, block.target, block.number, "budget", ceiling
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
        actions.append(Relabel(
            block.target,
            block.number,
            None,
            resume_label,
            revision,
            removes=removes,
        ))
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
        ))
    return actions
