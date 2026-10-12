"""Per-item retry backoff for machine failures, derived from ended legs.

A machine failure (provider overload, quota, OOM, ENOSPC, a killed or
unreturned process, a runtime that never started) never counts as an attempt
on the issue: it measures the harness, not the work. It is retried, but not
at poll rate. The next dispatch of the same kind for the same item waits
``min(60s * 2^(n-1), 1h)`` after the last machine end, where ``n`` counts the
consecutive machine ends since the last non-machine end. Nothing else bounds
the retries; the money caps do. Nothing is stored either: the inputs are the
legs the run table already records.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .actions import AgentKind

BACKOFF_BASE = timedelta(seconds=60)
BACKOFF_CAP = timedelta(hours=1)
# 60s * 2^6 already exceeds the cap; a longer streak cannot lengthen the wait.
_MAX_DOUBLINGS = 6


# Only the leg that ended a dispatch may reset its streak: the advisory ROUTE
# leg runs before every unpinned RESOLVE and returns normally, so treating it
# as RESOLVE pinned the wait at 60s however many RESOLVE legs died (slice 3
# review). ROUTE stays its own kind, which nothing dispatches.
def dispatched_kind(run_kind: str) -> AgentKind | None:
    """The dispatch a recorded run kind belongs to, or None if it is no agent kind."""
    try:
        return AgentKind[run_kind]
    except KeyError:
        return None


@dataclass(frozen=True)
class LegEnd:
    """One ended leg for an item: what was dispatched, when it ended, and
    whether the harness (not the agent) ended it."""

    kind: AgentKind
    ended_at: datetime
    machine: bool
    # The PR head the leg was dispatched on ("" for issue-stage legs). Lets
    # attempt counting key REVIEW legs to the head they reviewed.
    head: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AgentKind):
            raise ValueError("leg end requires a typed agent kind")
        if not isinstance(self.ended_at, datetime) or self.ended_at.utcoffset() is None:
            raise ValueError("leg end requires an aware end time")
        if type(self.machine) is not bool:
            raise ValueError("leg end requires an exact machine classification")
        if not isinstance(self.head, str):
            raise ValueError("leg end head must be a string")


def machine_backoff_until(ends: Iterable[LegEnd], kind: AgentKind) -> datetime | None:
    """When ``kind`` may next dispatch for the item these ends belong to.

    None means no wait: the latest end of that kind was not a machine failure,
    or there is no end at all.
    """
    streak = 0
    latest: datetime | None = None
    for end in sorted((end for end in ends if end.kind is kind), key=lambda end: end.ended_at, reverse=True):
        if not end.machine:
            break
        streak += 1
        latest = latest or end.ended_at
    if latest is None:
        return None
    return latest + machine_backoff_wait(streak)


def machine_backoff_wait(streak: int) -> timedelta:
    """The wait after the ``streak``-th consecutive machine end (at least one)."""
    return min(BACKOFF_BASE * 2 ** min(max(streak, 1) - 1, _MAX_DOUBLINGS), BACKOFF_CAP)

