"""CI-state interpretation — pure ports of watcher-lib.sh.

`reduce_ci`: pending OUTRANKS failure (the #49 anti-loop rule), with
repository-required reports deciding eligibility; an unread checks fetch is
Unavailable, never an answer (#48).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from .checks import BaseHealth, BaseIssue, BaseRun, CIObservation, Observed, RequiredCheck, Unavailable
from collections.abc import Iterable
from datetime import datetime, timezone

from .world import Check


@dataclass(frozen=True)
class Clear:
    pass


@dataclass(frozen=True)
class Queued:
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class Executing:
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class MissingRequired:
    requirements: tuple[RequiredCheck, ...]


@dataclass(frozen=True)
class Waiting:
    reason: Queued | Executing | MissingRequired


@dataclass(frozen=True)
class Failed:
    checks: tuple[Check, ...]

    def __post_init__(self) -> None:
        if not self.checks or any(c.bucket != "fail" for c in self.checks):
            raise ValueError("failed CI requires nonempty failures")


@dataclass(frozen=True)
class BaseRed:
    """Every failure on the PR is red on the base tip too: the base is broken, not the PR (#313)."""


def _gated_checks(observation: Observed) -> tuple[Check, ...]:
    return tuple(c for c in observation.reports if not observation.policy or c.required)


def reduce_ci(observation: CIObservation) -> Clear | Waiting | Failed | Unavailable:
    if isinstance(observation, Unavailable):
        return observation
    eligible = _gated_checks(observation)
    witnesses = {(c.name, c.app_id) for c in eligible if c.required}
    names = {name for name, _ in witnesses}
    missing = tuple(sorted((r for r in observation.policy
        if (r.context not in names if r.app_id is None else (r.context, r.app_id) not in witnesses)),
        key=lambda r: (r.context, r.app_id or 0)))
    if missing:
        return Waiting(MissingRequired(missing))
    pending = tuple(c for c in eligible if is_pending(c))
    if pending:
        return Waiting(Executing(pending) if any(c.state != "QUEUED" for c in pending) else Queued(pending))
    failures = tuple(c for c in eligible if c.bucket == "fail")
    return Failed(failures) if failures else Clear()


def charge(failed: Failed, base: tuple[Check, ...] | None) -> Failed | Waiting | BaseRed:
    """Attribute a PR's settled failures against the base tip's reports (#313).

    A failure is the base's only if every base report of the same check is a
    plain FAILURE/ERROR or still running; no report, a pass, or any other
    conclusion (CANCELLED, TIMED_OUT, STALE: nothing ever re-reads those)
    keeps it the PR's, and one PR-owned failure owes FIX_CI. A running base
    twin outranks failure (#49) but only while IN_PROGRESS, which a job
    timeout bounds; QUEUED or WAITING can sit forever, so it charges the PR.
    An unread base is None and charges the PR, as before.
    """
    if base is None:
        return failed
    outcomes: dict[tuple[str, str, int | None], set[str]] = {}
    for check in base:
        outcomes.setdefault(_identity(check), set()).add(check.state)
    shared = {_identity(c) for c in failed.checks}
    if any(not outcomes.get(key) or not outcomes[key] <= {"FAILURE", "ERROR", "IN_PROGRESS"} for key in shared):
        return failed
    running = tuple(c for c in base if c.state == "IN_PROGRESS" and _identity(c) in shared)
    return Waiting(Executing(running)) if running else BaseRed()


def _identity(check: Check) -> tuple[str, str, int | None]:
    # A check run is its app's; a commit status is matched by context alone,
    # as branch protection does (a required status may carry a policy app id).
    return (check.kind, check.name, check.app_id if check.kind == "check" else None)


def is_pending(check: Check) -> bool:
    return check.bucket == "pending" or check.state in ("QUEUED", "IN_PROGRESS")


def parse_timestamp(raw: str) -> datetime | None:
    """An ISO-8601 GitHub timestamp as an aware UTC datetime, or None.

    Defensive by contract: every caller here treats None as "unknown", never as
    zero/epoch, so a gh version that omits the field, a null, a blank, or a
    format change degrades to "no deadline" rather than to "instantly overdue".
    The trailing `Z` is normalised by hand because `fromisoformat` only learned
    it in 3.11 and this package supports 3.10. A naive timestamp (no offset) is
    read as UTC, which is what GitHub emits.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# A base run's verdict. Cancelled, skipped and every other conclusion count
# neither way: with `cancel-in-progress` on base pushes most runs are cancelled.
RED_CONCLUSIONS = frozenset({"failure", "timed_out"})
GREEN_CONCLUSIONS = frozenset({"success", "neutral"})
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _order(run: BaseRun) -> tuple[datetime, int]:
    return (parse_timestamp(run.created_at) or _EPOCH, run.run_id)


def base_verdicts(runs: Iterable[BaseRun]) -> tuple[tuple[BaseRun, BaseRun, bool], ...]:
    """Per workflow name: its latest verdict run (by creation, then run id; a
    re-run keeps its run), the first run of the streak of that verdict ending
    at it as far back as `runs` reach, and whether an opposite verdict bounds
    that streak (else it may have begun before the earliest listed run)."""
    by_workflow: dict[str, list[BaseRun]] = {}
    for run in sorted(runs, key=_order, reverse=True):
        if run.conclusion in RED_CONCLUSIONS | GREEN_CONCLUSIONS:
            by_workflow.setdefault(run.workflow, []).append(run)
    out = []
    for name in sorted(by_workflow):
        latest, *older = by_workflow[name]
        red = latest.conclusion in RED_CONCLUSIONS
        streak = [latest, *itertools.takewhile(lambda r: (r.conclusion in RED_CONCLUSIONS) == red, older)]
        out.append((latest, streak[-1], len(streak) <= len(older)))
    return tuple(out)



def base_title(base: str, previous: int | None) -> str:
    """The previous issue fixes identity even while more workflows finish red."""
    return f"CI: base failing on {base} " + (f"(after #{previous})" if previous else "(first)")


def base_issues(health: BaseHealth) -> tuple[BaseIssue, ...]:
    """This branch's issues, including the old per-workflow titles, oldest first."""
    shape = re.compile(rf"CI: .+ failing on {re.escape(health.ref)}(?: since \S+| \(first\)| \(after #\d+\))?")
    return tuple(sorted((i for i in health.issues if shape.fullmatch(i.title)), key=lambda i: i.number))


def base_held(health: BaseHealth | None) -> frozenset[int]:
    """Only one issue can own a red base; green issues await closure unpaid."""
    if health is None or health.runs is None:
        return frozenset()
    issues = base_issues(health)
    opened = [i.number for i in issues if not i.closed_at]
    red = any(latest.conclusion in RED_CONCLUSIONS for latest, _, _ in base_verdicts(health.runs))
    return frozenset(i.number for i in issues) - (frozenset(opened[:1]) if red else frozenset())


def base_fix(health: BaseHealth | None) -> frozenset[int]:
    """The single branch repair is first when any red workflow is required."""
    if health is None or health.runs is None or health.required is None:
        return frozenset()
    red = {latest.workflow for latest, _, _ in base_verdicts(health.runs) if latest.conclusion in RED_CONCLUSIONS}
    opened = [i.number for i in base_issues(health) if not i.closed_at]
    return frozenset(opened[:1]) if health.required & red else frozenset()
