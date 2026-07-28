"""CI-state and review interpretation — pure ports of watcher-lib.sh.

These functions carry hard-won lessons and are tested in isolation:
  * `ci_state` — pending OUTRANKS failure (the #49 anti-loop rule), with
    CI_IGNORE_CHECKS dropping environmentally-stuck checks first.
  * `pending_deadline_breached` — the BOUND on how long that pending state may
    last (#30). It does not reorder the precedence above; it says when waiting
    on it has stopped being waiting and become a deadlock.
  * `latest_other_review` — the latest review by a DISTINCT actor (the
    no-self-approval invariant: a fixer never reviews/merges its own PR).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

from .identity import login_eq
from .world import Check, Review, CIState


def _kept(checks: Iterable[Check], ignore_regex: str) -> list[Check]:
    """Checks surviving CI_IGNORE_CHECKS. Shared by `ci_state` and the pending
    deadline so an operator-ignored check can never drive either — an ignored
    check is, by definition, one the harness has been told not to reason about."""
    pattern = re.compile(ignore_regex) if ignore_regex else None
    return [c for c in checks if pattern is None or not pattern.search(c.name or "")]


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


def pending_checks(checks: Iterable[Check], ignore_regex: str = "") -> tuple[Check, ...]:
    """The non-ignored checks currently holding `ci_state` at PENDING."""
    return tuple(c for c in _kept(checks, ignore_regex) if _is_pending(c))


def pending_age_seconds(
    checks: Iterable[Check], now: str, ignore_regex: str = ""
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
        t for c in pending_checks(checks, ignore_regex)
        if (t := parse_timestamp(c.started_at)) is not None
    ]
    if not starts:
        return None
    return (at - max(starts)).total_seconds()


def pending_deadline_breached(
    checks: Iterable[Check], now: str, max_seconds: float, ignore_regex: str = ""
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
    age = pending_age_seconds(checks, now, ignore_regex)
    return age is not None and age >= max_seconds


def ci_state(checks: Iterable[Check], ignore_regex: str = "") -> CIState:
    """Settled CI state for a PR's checks.

    Port of watcher-lib.sh:ci_state. CI_IGNORE_CHECKS (an extended-regex of
    check NAMES) drops environmentally-stuck checks BEFORE deciding, so a job
    queued forever on a starved runner can't pin us at `pending` and deadlock
    fix-CI + auto-merge. Real failures on any non-ignored check still count.

    Precedence is the whole point:
      empty (after ignore) → none
      any pending/QUEUED/IN_PROGRESS → pending   (OUTRANKS failure)
      any fail bucket → failure
      else → ok
    """
    kept = _kept(checks, ignore_regex)
    if not kept:
        return CIState.NONE
    if any(_is_pending(c) for c in kept):
        return CIState.PENDING
    if any(c.bucket == "fail" for c in kept):
        return CIState.FAILURE
    return CIState.OK


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
