"""CI-state interpretation — pure ports of watcher-lib.sh.

`reduce_ci`: pending OUTRANKS failure (the #49 anti-loop rule), with
repository-required reports deciding eligibility; an unread checks fetch is
Unavailable, never an answer (#48).
"""

from __future__ import annotations

from dataclasses import dataclass

from .checks import CIObservation, Observed, RequiredCheck, Unavailable
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


def failed_checks(checks: CIObservation) -> tuple[Check, ...]:
    """Settled failures covered by the repository CI policy."""
    result = reduce_ci(checks)
    return result.checks if isinstance(result, Failed) else ()

