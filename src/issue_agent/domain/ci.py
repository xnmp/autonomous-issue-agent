"""CI-state interpretation — pure ports of watcher-lib.sh.

`reduce_ci`: pending OUTRANKS failure (the #49 anti-loop rule), with
repository-required reports deciding eligibility; an unread checks fetch is
Unavailable, never an answer (#48).
"""

from __future__ import annotations

import itertools
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


def base_verdicts(runs: Iterable[BaseRun]) -> tuple[tuple[BaseRun, BaseRun, bool], ...]:
    """Per workflow name: its latest verdict run (by creation, then run id; a
    re-run keeps its run), the first run of the streak of that verdict ending
    at it as far back as `runs` reach, and whether an opposite verdict bounds
    that streak (else it may have begun before the earliest listed run)."""
    by_workflow: dict[str, list[BaseRun]] = {}
    for run in sorted(runs, key=lambda r: (parse_timestamp(r.created_at) or _EPOCH, r.run_id), reverse=True):
        if run.conclusion in RED_CONCLUSIONS | GREEN_CONCLUSIONS:
            by_workflow.setdefault(run.workflow, []).append(run)
    out = []
    for name in sorted(by_workflow):
        latest, *older = by_workflow[name]
        red = latest.conclusion in RED_CONCLUSIONS
        streak = [latest, *itertools.takewhile(lambda r: (r.conclusion in RED_CONCLUSIONS) == red, older)]
        out.append((latest, streak[-1], len(streak) <= len(older)))
    return tuple(out)



def base_title(workflow: str, base: str, head: str = "") -> str:
    """A base-CI issue's title, one per workflow per red streak (#402), named
    by the head of the streak's first failing run. Without `head`: the prefix
    all of the workflow's titles share, and an issue from before #402, which
    counts as its workflow's current streak. The name is cut so the title
    stays within GitHub's 256 characters."""
    name = " ".join(workflow.split())[:max(1, min(200, 226 - len(base)))]
    return f"CI: {name} failing on {base}" + (f" since {head[:7]}" if head else "")


def workflow_issues(health: BaseHealth, workflow: str) -> tuple[BaseIssue, ...]:
    """`workflow`'s base-CI issues, oldest first."""
    prefix = base_title(workflow, health.ref)
    return tuple(sorted((i for i in health.issues if i.title == prefix or i.title.startswith(prefix + " since ")),
                        key=lambda i: i.number))


def base_fix(health: BaseHealth | None) -> frozenset[int]:
    """The open base-CI issues of workflows the base branch requires (#402):
    the harness readies them and serves them first, without STEWARD, TRIAGE
    or a blocker, and their PRs are charged the base's failures. None while
    the required workflows are unread: the issues then wait for a human."""
    if health is None or health.required is None:
        return frozenset()
    return frozenset(i.number for w in health.required for i in workflow_issues(health, w) if not i.closed_at)
