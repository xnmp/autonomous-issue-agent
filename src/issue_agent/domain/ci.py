"""CI-state and review interpretation — pure ports of watcher-lib.sh.

These two functions carry hard-won lessons and are tested in isolation:
  * `ci_state` — pending OUTRANKS failure (the #49 anti-loop rule), with
    CI_IGNORE_CHECKS dropping environmentally-stuck checks first.
  * `latest_other_review` — the latest review by a DISTINCT actor (the
    no-self-approval invariant: a fixer never reviews/merges its own PR).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .world import Check, Review, CIState


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
    pattern = re.compile(ignore_regex) if ignore_regex else None
    kept = [
        c for c in checks
        if pattern is None or not pattern.search(c.name or "")
    ]
    if not kept:
        return CIState.NONE
    if any(
        c.bucket == "pending" or c.state in ("QUEUED", "IN_PROGRESS")
        for c in kept
    ):
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
    """
    others = [r for r in reviews if r.author != fix_bot]
    if not others:
        return None
    return max(others, key=lambda r: r.submitted_at)
