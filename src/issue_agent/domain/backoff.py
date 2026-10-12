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

# Split and routing legs run inside a RESOLVE dispatch, so they back off the
# dispatch that bought them rather than a kind nothing dispatches.
_DISPATCHED_AS: dict[str, AgentKind] = {
    "ROUTE": AgentKind.RESOLVE,
    "AUTHOR": AgentKind.RESOLVE,
    "IMPLEMENT": AgentKind.RESOLVE,
    "ADJUDICATE": AgentKind.RESOLVE,
}


def dispatched_kind(run_kind: str) -> AgentKind | None:
    """The dispatch a recorded run kind belongs to, or None if it is no agent kind."""
    if run_kind in _DISPATCHED_AS:
        return _DISPATCHED_AS[run_kind]
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

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AgentKind):
            raise ValueError("leg end requires a typed agent kind")
        if not isinstance(self.ended_at, datetime) or self.ended_at.utcoffset() is None:
            raise ValueError("leg end requires an aware end time")
        if type(self.machine) is not bool:
            raise ValueError("leg end requires an exact machine classification")


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
    return latest + min(BACKOFF_BASE * 2 ** min(streak - 1, _MAX_DOUBLINGS), BACKOFF_CAP)

