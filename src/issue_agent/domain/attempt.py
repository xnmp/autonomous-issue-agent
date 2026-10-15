"""Whether a returned leg is an attempt, and how an item's next leg starts. Pure.

The one rule (#441); derive and the dashboard read it through `tick.leg_end`,
the fix role through `next_leg`. A cut-off end (`outcome.CUT_OFF_ENDS`: a
per-leg cap, or the provider ending the turn) leaves its session intact, so
the item resumes it once at the same tier, free (#411, #418); a cut-off of
that resume counts, or a permanent provider error would retry free forever
(#421). STEWARD, TRIAGE and REVIEW never resume, so their cut-off counts at
once. A counted cut-off did not finish with its evidence (`LegEnd.capped`, #415).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .actions import ROUTED_ACTS, AgentKind
from .backoff import LegEnd


@dataclass(frozen=True)
class Fresh:
    """A new session, briefed like a first leg."""


@dataclass(frozen=True)
class Continue:
    """Resume the session a cap or the provider cut off: the item's free resume."""

    session: str


@dataclass(frozen=True)
class AnswerResume:
    """Resume the session that asked the question a human has since answered."""

    session: str


@dataclass(frozen=True)
class NextLeg:
    start: Fresh | Continue | AnswerResume
    # The item's newest agent end, when it asked: the next leg carries the
    # human's reply since it ended, however it starts.
    answering: LegEnd | None = None


def latest_session(ends: Iterable[LegEnd], runtime: str) -> str:
    """The newest session these ends left on `runtime`: a leg routed to
    another runtime, or recorded on none known (""), starts fresh rather than
    resume a transcript it cannot read."""
    if not runtime:
        return ""
    return next((end.session for end in reversed(tuple(ends)) if end.session and end.runtime == runtime), "")


def next_leg(ends: Sequence[LegEnd], runtime: str) -> NextLeg:
    """How the item's next routed leg on `runtime` starts, from the item's
    ended legs, oldest first. Only routed acts continue sessions, so the
    other kinds' ends are not read. Machine ends after the newest agent end
    neither drop its question nor spend the free resume: the newest of them
    that left a session is continued."""
    own = [end for end in ends if end.kind in ROUTED_ACTS]
    last = next((i for i in range(len(own) - 1, -1, -1) if not own[i].machine), -1)
    asked = own[last] if last >= 0 and own[last].question is not None else None
    if cut := latest_session(own[last + 1:], runtime):
        return NextLeg(Continue(cut), asked)
    if asked is not None and (session := latest_session((asked,), runtime)):
        return NextLeg(AnswerResume(session), asked)
    return NextLeg(Fresh(), asked)


def cut_off_counts(kind: AgentKind, runtime: str, earlier: Sequence[LegEnd]) -> bool:
    """Whether a cut-off end of a `kind` leg on `runtime`, after the item's
    `earlier` ends (oldest first), is an attempt: the leg was the free resume,
    or its kind never resumes."""
    return kind not in ROUTED_ACTS or isinstance(next_leg(earlier, runtime).start, Continue)
