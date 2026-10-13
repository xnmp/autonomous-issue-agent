"""What both roles share in a tick: observe -> derive -> policy -> act.

Each role reads the same GitHub observation and the same local facts (every
ended leg of the repository, from either role, plus the live claims and
leases of both), derives every item's state with `domain.state.derive`, maps
it to Actions with `domain.policy.actions`, and keeps only its own: the
review role runs `RunAgent(REVIEW)`, the fix role everything else. Both
deriving from the same facts is what keeps `needs-human` single-writer and a
leg of one role visible to the other (it holds its item busy and breaks the
other act's attempt run).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone

from ..adapters.control_store import LegRun
from ..adapters.accounting_runtime import LEG_ROLES
from ..domain.actions import Action, AgentKind, RunAgent
from ..domain.backoff import LegEnd, dispatched_kind
from ..domain.outcome import LegClass
from ..domain.policy import question_text
from ..domain.refs import harness_branch_issue
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Local, derive, issue_key, pr_key
from ..domain.world import WorldState


def run_item(run: LegRun) -> ItemKey | None:
    """The item a recorded leg belongs to: its issue for RESOLVE, its PR for
    PR-stage legs. Pre-5b kinds (RESUME, PR_RESUME, ROUTE) belong to no act:
    under the cutover rule those legs simply stop counting."""
    kind = dispatched_kind(run.kind)
    if kind is AgentKind.RESOLVE and run.issue > 0:
        return issue_key(run.issue)
    if kind in DISPATCHABLE and kind is not AgentKind.RESOLVE and run.number > 0:
        return pr_key(run.number)
    return None


def leg_class(run: LegRun) -> LegClass:
    """A leg's recorded class; no readable receipt is a machine end."""
    raw = run.receipt.get("leg") if run.receipt is not None else None
    return next((c for c in (LegClass.AGENT, LegClass.ASKED) if raw == c.value), LegClass.MACHINE)


def leg_end(run: LegRun) -> LegEnd | None:
    """A recorded leg as derive's `LegEnd`. A run without a `leg` receipt
    never returned (died, reclaimed, refused before launch): a machine end."""
    kind = dispatched_kind(run.kind)
    if kind not in DISPATCHABLE or run.completed_at.utcoffset() is None:
        return None
    leg = leg_class(run)
    started = run.started_at
    if started is not None and (started.utcoffset() is None or started > run.completed_at):
        started = None
    question = question_text((run.receipt or {}).get("question")) if leg is LegClass.ASKED else None
    return LegEnd(kind, run.completed_at, leg is LegClass.MACHINE, run.head, question=question, started_at=started)


def local_legs(runs: Iterable[LegRun]) -> dict[ItemKey, tuple[LegEnd, ...]]:
    out: dict[ItemKey, list[LegEnd]] = {}
    for run in runs:
        key, end = run_item(run), leg_end(run)
        if key is not None and end is not None:
            out.setdefault(key, []).append(end)
    return {key: tuple(sorted(ends, key=lambda e: e.ended_at)) for key, ends in out.items()}


def dispatch_item(action: RunAgent) -> ItemKey:
    return issue_key(action.number) if action.kind is AgentKind.RESOLVE else pr_key(action.number)


def leased_items(active: Iterable[RunAgent], held_issues: Iterable[int]) -> frozenset[ItemKey]:
    """Items with a leg in flight: this process's dispatches, plus every issue
    either role (in any process, or an orphaned child) holds a live claim or
    lease on."""
    return frozenset(dispatch_item(a) for a in active) | frozenset(
        issue_key(n) for n in held_issues if type(n) is int and n > 0
    )


def item_issues(world: WorldState) -> frozenset[int]:
    return frozenset(i.number for i in world.issues if i.number > 0) | frozenset(
        n for p in world.prs if (n := harness_branch_issue(p.head_ref_name)) is not None
    )


class Facts:
    """The local facts derive needs, read from SQLite each tick. A failed run
    or lease read raises: the daemon skips the tick rather than deciding
    blind. A failed lifetime-spend read is no headroom instead: paid legs wait
    silently and free work (merge, open, refresh, stops) goes on."""

    def __init__(
        self,
        control,
        repository: str,
        *,
        active: Callable[[], Iterable[RunAgent]],
        headroom: Callable[[], bool],
        lifetime_capped: Callable[[int], bool],
        log: Callable[[str], None],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.control = control
        self.repository = repository
        self.active = active
        self.headroom = headroom
        self.lifetime_capped = lifetime_capped
        self.log = log
        self.now = now

    def held_issues(self) -> set[int]:
        repo = self.repository
        held = {c.target_key for role in LEG_ROLES for c in self.control.active_dispatch_claims(repo, role)}
        for lease in self.control.leases():
            try:
                dispatch = json.loads(lease.dispatch_id)
            except (TypeError, ValueError):
                continue
            if isinstance(dispatch, dict) and dispatch.get("role") in LEG_ROLES and dispatch.get("repository") == repo:
                held.add(dispatch.get("issue"))
        return held

    def __call__(self, world: WorldState) -> Local:
        # `active` is sampled before the run read, so a leg ending between the
        # two reads is busy (still active) rather than free with its end
        # unseen. A leg that ended after GitHub was read is seen here but its
        # push may not be in `world`: derive holds it busy (end > world.now).
        active = tuple(self.active())
        issues = item_issues(world)
        runs = self.control.leg_runs(self.repository, issues)
        try:
            capped: frozenset[int] | None = frozenset(n for n in issues if self.lifetime_capped(n))
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self.log(f"lifetime spend unreadable, paid legs wait this tick: {exc}")
            capped = None
        return Local(
            now=self.now(),
            legs=local_legs(runs),
            leases=leased_items(active, self.held_issues()),
            headroom=capped is not None and self.headroom(),
            lifetime_capped=capped or frozenset(),
        )


Choose = Callable[[WorldState, Mapping[ItemKey, DerivedState], int], Iterable[Action]]


def decide_with(
    role: str,
    facts: Callable[[WorldState], Local],
    harness: Harness,
    choose: Choose,
    slots: Callable[[], int],
    log: Callable[[str], None],
) -> Callable[[WorldState], list[Action]]:
    """derive once, then the role's `choose` keeps its Actions (with at most
    `slots` paid dispatches)."""
    def decide(world: WorldState) -> list[Action]:
        chosen = list(choose(world, derive(world, facts(world), harness), slots()))
        for action in chosen:
            log(f"{role}: {action!r}")
        return chosen
    return decide


def lease_slots(daemon_ref: Mapping[str, object]) -> Callable[[], int]:
    def slots() -> int:
        daemon = daemon_ref.get("daemon")
        return int(getattr(daemon, "available_dispatch_slots", 0)) if daemon is not None else 0
    return slots


__all__ = [
    "Choose", "Facts", "decide_with", "dispatch_item", "item_issues", "lease_slots", "leased_items",
    "leg_class", "leg_end", "local_legs", "run_item",
]
