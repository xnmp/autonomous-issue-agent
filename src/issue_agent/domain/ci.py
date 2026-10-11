"""CI-state and review interpretation — pure ports of watcher-lib.sh.

These functions carry hard-won lessons and are tested in isolation:
  * `ci_state` — pending OUTRANKS failure (the #49 anti-loop rule), with
    repository-required reports deciding eligibility.
  * `pending_deadline_breached` — the BOUND on how long that pending state may
    last (#30). It does not reorder the precedence above; it says when waiting
    on it has stopped being waiting and become a deadlock.
  * `latest_other_review` — the latest review by a DISTINCT actor (the
    no-self-approval invariant: a fixer never reviews/merges its own PR).
"""

from __future__ import annotations

from dataclasses import dataclass

from .checks import CIObservation, Observed, RequiredCheck, Unavailable
from collections.abc import Iterable
from datetime import datetime, timezone

from .identity import login_eq
from .world import Check, Review, CIState


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


def repair_evidence_incomplete(observation: CIObservation) -> bool:
    result = reduce_ci(observation)
    return isinstance(result, Unavailable) or (isinstance(result, Waiting) and isinstance(result.reason, MissingRequired))


def gated_checks(observation: CIObservation | Iterable[Check]) -> tuple[Check, ...]:
    if isinstance(observation, Unavailable):
        return ()
    if isinstance(observation, Observed):
        return tuple(c for c in observation.reports if not observation.policy or c.required)
    return tuple(observation)


def reduce_ci(observation: CIObservation) -> Clear | Waiting | Failed | Unavailable:
    if isinstance(observation, Unavailable):
        return observation
    eligible = gated_checks(observation)
    witnesses = {(c.name, c.app_id) for c in eligible if c.required}
    names = {name for name, _ in witnesses}
    missing = tuple(sorted((r for r in observation.policy
        if (r.context not in names if r.app_id is None else (r.context, r.app_id) not in witnesses)),
        key=lambda r: (r.context, r.app_id or 0)))
    if missing:
        return Waiting(MissingRequired(missing))
    pending = tuple(c for c in eligible if _is_pending(c))
    if pending:
        return Waiting(Executing(pending) if any(c.state != "QUEUED" for c in pending) else Queued(pending))
    failures = tuple(c for c in eligible if c.bucket == "fail")
    return Failed(failures) if failures else Clear()


def cancelled_ci_unverified(observation: CIObservation, cancelled_head: str, rerun_head: str) -> bool:
    # Empty-policy green after our cancellation can mean every job was skipped;
    # only spending the existing rerun relinquishes the cancellation's authority.
    return (isinstance(observation, Observed) and not observation.policy
        and observation.head == cancelled_head and observation.head != rerun_head
        and isinstance(reduce_ci(observation), Clear))


def _is_pending(check: Check) -> bool:
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


def pending_checks(checks: CIObservation | Iterable[Check]) -> tuple[Check, ...]:
    """The gating checks currently holding `ci_state` at PENDING."""
    if isinstance(checks, Observed) and isinstance((result := reduce_ci(checks)), Waiting) and isinstance(result.reason, MissingRequired):
        return ()
    return tuple(c for c in gated_checks(checks) if _is_pending(c))


def executing_pending_checks(checks: CIObservation | Iterable[Check]) -> tuple[Check, ...]:
    """Pending checks that are not QUEUED: the ones a stall deadline may judge.

    A QUEUED check has no runner yet. Saturated GitHub-hosted runners held tauri
    jobs runnerless for 67-80 min (PRs #1065/#1070, 2026-10-11); timing that wait
    cancelled healthy runs, lost their queue position, and escalated them to a
    human. Every other pending state keeps its deadline and ceiling.
    """
    return tuple(c for c in pending_checks(checks) if c.state != "QUEUED")


def failed_checks(
    checks: CIObservation | Iterable[Check]
) -> tuple[Check, ...]:
    """Settled failures covered by the repository CI policy."""

    if isinstance(checks, (Observed, Unavailable)):
        result = reduce_ci(checks)
        return result.checks if isinstance(result, Failed) else ()
    return tuple(c for c in checks if c.bucket == "fail")


def pending_age_seconds(
    checks: CIObservation | Iterable[Check], now: str
) -> float | None:
    """Seconds since the NEWEST pending check started, or None when unknowable.

    Measured from the newest (not oldest) start on purpose: a push adds fresh
    check runs, so the newest start is the last moment CI demonstrably made
    progress. That makes the clock reset on a new head commit for free, with no
    stored per-SHA state — and it means a long, healthy pipeline whose jobs
    start in sequence is judged on its most recent job, not on its first.

    None when there is nothing pending, when no pending check has a parseable
    `started_at`, or when `now` is unstamped/unparseable. A negative age (a
    start in the future — clock skew between GitHub and this host) is returned
    as-is and simply never breaches.
    """
    at = parse_timestamp(now)
    if at is None:
        return None
    starts = [
        t for c in executing_pending_checks(checks)
        if (t := parse_timestamp(c.started_at)) is not None
    ]
    if not starts:
        return None
    return (at - max(starts)).total_seconds()


def pending_deadline_breached(
    checks: CIObservation | Iterable[Check], now: str, max_seconds: float
) -> bool:
    """True iff CI has been pending past `max_seconds` (#30).

    `ci_state` returns PENDING while any check is queued/in-progress, and
    PENDING deliberately OUTRANKS FAILURE (the #49 anti-loop rule). Nothing
    bounded that wait: one job stuck IN_PROGRESS pins a PR forever, with no
    escalation and no journal event — observed live, a windows job sitting 30+
    minutes where the identical previous run took 4.

    This adds the bound and nothing else; the precedence above is untouched.
    `max_seconds <= 0` disables it, like every other breaker in the domain.
    The comparison is `>=` so a deadline of exactly N fires at N.
    """
    if max_seconds <= 0:
        return False
    age = pending_age_seconds(checks, now)
    return age is not None and age >= max_seconds


def ci_state(checks: CIObservation | Iterable[Check]) -> CIState:
    """Compact telemetry vocabulary for the typed CI reduction."""
    if not isinstance(checks, (Observed, Unavailable)):
        checks = Observed("legacy", "legacy", frozenset(), tuple(checks))
    result = reduce_ci(checks)
    if isinstance(result, Unavailable):
        return CIState.UNKNOWN
    if isinstance(result, Waiting):
        return CIState.PENDING
    if isinstance(result, Failed):
        return CIState.FAILURE
    return CIState.OK if checks.reports else CIState.NONE


def latest_other_review(reviews: Iterable[Review], fix_bot: str) -> Review | None:
    """The most recently submitted review by someone OTHER than the fixer.

    Port of watcher-lib.sh:latest_other_review. The fixer must never act on
    (approve/merge against) its own review — GitHub rejects self-approval 422,
    and auto-merge requires a genuinely independent approver. Reviews by the
    fix bot are filtered out; the latest remaining one (by submitted_at) wins.
    Returns None when no distinct actor has reviewed.

    Identity comparison goes through `login_eq`, never `==`: a `FIX_BOT` written
    with the `[bot]` suffix would otherwise never match the bare login gh reports
    in the review author field, and the fixer would treat its OWN approval as an
    independent one (review C1).
    """
    others = [r for r in reviews if not login_eq(r.author, fix_bot)]
    if not others:
        return None
    return max(others, key=lambda r: r.submitted_at)
