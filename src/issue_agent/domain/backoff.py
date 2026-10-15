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
from itertools import takewhile
from datetime import datetime, timedelta

from .actions import AgentKind
from .outcome import MACHINE_ENDS
from .routing import Tier
from .triage import Steward, Triage

BACKOFF_BASE = timedelta(seconds=60)
BACKOFF_CAP = timedelta(hours=1)
# 60s * 2^6 already exceeds the cap; a longer streak cannot lengthen the wait.
_MAX_DOUBLINGS = 6
# The streak whose wait is the cap: the harness has failed to run the act for
# over an hour, a harness gap it files about itself (`domain.gaps`).
STALL_STREAK = _MAX_DOUBLINGS + 1


# A recorded kind that is no AgentKind (the deleted ROUTE leg's rows) belongs
# to no dispatch, so it can neither extend nor reset a streak.
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
    # The question the agent returned asking a human for judgment (the outcome
    # layer's ASKED class), bounded and neutralised; None when it did not ask.
    # Always None for a machine end.
    question: str | None = None
    # When the leg started, if known. FIX_BOT comments inside a leg's window are
    # the agent's own (it comments without the harness marker), so they never
    # reset attempts; other authors' comments in the window still do.
    started_at: datetime | None = None
    # A TRIAGE agent end's laundered result; None when it returned nothing usable.
    triage: Triage | None = None
    # A STEWARD agent end's laundered result, or the steward half of a legacy
    # combined TRIAGE result; None when it returned nothing usable.
    steward: Steward | None = None
    # The routed tier the leg ran at, when recorded (legs before routing have none).
    tier: Tier | None = None
    # The CI failure keys a FIX_CI agent end was handed (`evidence.failure_key`);
    # derive never hands them to another repair of the same head.
    evidence: frozenset[str] = frozenset()
    # The commit the leg's PR branch was at on the remote when it ended, as the
    # harness read it ("" when unread): not `head` means the leg pushed.
    end_head: str = ""
    # The harness ended this agent end at a per-leg cap (`outcome.leg_cap`, minted
    # by the accounting runtime): it did not finish with the evidence it was handed.
    capped: bool = False
    # The agent reported a harness defect (`domain.gaps`; its text stays in the receipt).
    harness_gap: bool = False
    # The leg's id and runtime, as recorded ("" when unknown): what a gap report names.
    leg_id: str = ""
    runtime: str = ""
    # A machine end's recorded class (`outcome.MACHINE_ENDS`); "" when it left no receipt.
    end: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AgentKind):
            raise ValueError("leg end requires a typed agent kind")
        if not isinstance(self.ended_at, datetime) or self.ended_at.utcoffset() is None:
            raise ValueError("leg end requires an aware end time")
        if type(self.machine) is not bool:
            raise ValueError("leg end requires an exact machine classification")
        if type(self.capped) is not bool or (self.capped and (self.machine or self.question is not None)):
            raise ValueError("only an agent end that asked nothing can be capped")
        if not isinstance(self.head, str) or not isinstance(self.end_head, str):
            raise ValueError("leg end heads must be strings")
        if self.question is not None and (not isinstance(self.question, str) or not self.question or self.machine):
            raise ValueError("only an agent end can carry a question, and it is non-empty text")
        if self.started_at is not None and (
            not isinstance(self.started_at, datetime) or self.started_at.utcoffset() is None
            or self.started_at > self.ended_at
        ):
            raise ValueError("leg start must be an aware time no later than its end")
        if self.triage is not None and (
            not isinstance(self.triage, Triage) or self.kind is not AgentKind.TRIAGE or self.machine
        ):
            raise ValueError("only a TRIAGE agent end carries a triage result")
        if self.steward is not None and (
            not isinstance(self.steward, Steward) or self.kind not in (AgentKind.STEWARD, AgentKind.TRIAGE)
            or self.machine
        ):
            raise ValueError("only a STEWARD or TRIAGE agent end carries a steward result")
        if self.tier is not None and not isinstance(self.tier, Tier):
            raise ValueError("leg tier must be a Tier")
        if not isinstance(self.evidence, frozenset) or any(not isinstance(k, str) or not k for k in self.evidence) or (
            self.evidence and (self.kind is not AgentKind.FIX_CI or self.machine)
        ):
            raise ValueError("only a FIX_CI agent end carries the failure keys it was handed")
        if type(self.harness_gap) is not bool or (self.harness_gap and self.machine):
            raise ValueError("only an agent end reports a harness gap")
        if any(not isinstance(v, str) or len(v) > 64 for v in (self.leg_id, self.runtime)):
            raise ValueError("a leg id and runtime are bounded strings")
        if not isinstance(self.end, str) or (self.end and (not self.machine or self.end not in MACHINE_ENDS)):
            raise ValueError("only a machine end carries an end class, from the fixed vocabulary")


def machine_backoff_until(ends: Iterable[LegEnd], kind: AgentKind) -> datetime | None:
    """When ``kind`` may next dispatch for the item these ends belong to.

    None means no wait: the latest end of that kind was not a machine failure,
    or there is no end at all.
    """
    streak = machine_streak(ends, kind)
    return streak[0].ended_at + machine_backoff_wait(len(streak)) if streak else None


def machine_streak(ends: Iterable[LegEnd], kind: AgentKind) -> tuple[LegEnd, ...]:
    """The consecutive machine ends of ``kind`` since its last agent end, newest first."""
    # Ends sharing a timestamp keep their recorded order, so the later-recorded
    # one counts as newer (a refused submission can land in the same
    # millisecond as the paid leg it follows).
    newest_first = reversed(sorted((end for end in ends if end.kind is kind), key=lambda end: end.ended_at))
    return tuple(takewhile(lambda end: end.machine, newest_first))


def machine_backoff_wait(streak: int) -> timedelta:
    """The wait after the ``streak``-th consecutive machine end (at least one)."""
    return min(BACKOFF_BASE * 2 ** min(max(streak, 1) - 1, _MAX_DOUBLINGS), BACKOFF_CAP)

