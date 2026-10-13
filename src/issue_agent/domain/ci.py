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

from .world import Check, CIState


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


def failed_checks(
    checks: CIObservation | Iterable[Check]
) -> tuple[Check, ...]:
    """Settled failures covered by the repository CI policy."""

    if isinstance(checks, (Observed, Unavailable)):
        result = reduce_ci(checks)
        return result.checks if isinstance(result, Failed) else ()
    return tuple(c for c in checks if c.bucket == "fail")


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
    return CIState.OK if isinstance(checks, Observed) and checks.reports else CIState.NONE


