"""What a repair leg is handed, as typed values (tauri #1078).

A FIX_CI dispatch carries the CI failures it repairs (`CiFailures`); an
ADDRESS_REVIEW dispatch carries the change request it answers
(`ChangeRequest`). Neither can be built empty, so a repair without evidence
cannot be represented: #1078's agent was told "CI failed" with nothing to
read and watched CI for 45 minutes holding a slot. Both come from the
observation; the agent reads the job logs and inline comments itself.

A failure's identity (`failure_key`) is its check run, so derive hands each
failure of a head to one repair (`new_failures`). Its key also names when its
CI run began (`began`): all that records whether a leg was handed a run caused
after an earlier leg, a new try (`state._attempts`).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from dataclasses import dataclass, field

from .checks import Check, Observed
from .ci import is_pending, parse_timestamp

# Failures one leg is handed, and recorded keys read back from its receipt.
MAX_FAILURES = 256
_MAX_KEY = 512


def failure_key(check: Check) -> str:
    """One failed report's identity, and when its CI run began. A check run's id
    is unique per attempt; a status is re-created (new `createdAt`) when re-posted."""
    return f"{check.kind}:{check.name if check.check_id is None else check.check_id}@{check.started_at}"[:_MAX_KEY]


def began(key: str) -> datetime | None:
    """When the CI run behind a failure key began (`Check.started_at`); None
    when the key names no parseable time."""
    return parse_timestamp(key.rpartition("@")[2]) if "@" in key else None


@dataclass(frozen=True)
class CiFailure:
    """`job_id`/`run_id` locate a GitHub Actions job's log for the agent; both
    are None for a commit status or another app's check."""

    key: str
    name: str
    job_id: int | None = None
    run_id: int | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.key, str) or not self.key or len(self.key) > _MAX_KEY
                or not isinstance(self.name, str) or not self.name
                or any(v is not None and (type(v) is not int or v <= 0) for v in (self.job_id, self.run_id))
                or (self.job_id is None) != (self.run_id is None)):
            raise ValueError("a CI failure needs a bounded key, a name and an optional Actions job and run")


@dataclass(frozen=True)
class CiFailures:
    """The failures a FIX_CI leg repairs: those on its head no earlier repair
    was handed. `handled` names the head's failures earlier legs were handed;
    `running` names its checks still running (both prompt context only)."""

    failures: tuple[CiFailure, ...]
    handled: tuple[str, ...] = ()
    running: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.failures, tuple) or not 0 < len(self.failures) <= MAX_FAILURES
                or any(not isinstance(f, CiFailure) for f in self.failures)
                or len({f.key for f in self.failures}) != len(self.failures)):
            raise ValueError("a CI repair needs between one and MAX_FAILURES distinct failures")
        for names in (self.handled, self.running):
            if not isinstance(names, tuple) or any(not isinstance(n, str) or not n for n in names):
                raise ValueError("handled and running are check names")

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.failures)


def new_failures(observed: Observed, given: Collection[str]) -> CiFailures | None:
    """The failed reports on the observed revision whose keys are not in
    `given`, required ones first; None when every failure was handed already."""
    failed: dict[str, Check] = {}
    for check in sorted((c for c in observed.reports if c.bucket == "fail"), key=lambda c: (not c.required, c.name)):
        failed.setdefault(failure_key(check), check)
    fresh = [(key, check) for key, check in failed.items() if key not in given][:MAX_FAILURES]
    if not fresh:
        return None
    return CiFailures(
        tuple(CiFailure(key, c.name, *((c.check_id, c.run_id) if c.check_id and c.run_id else (None, None)))
              for key, c in fresh),
        handled=tuple(dict.fromkeys(c.name for key, c in failed.items() if key in given)),
        running=tuple(dict.fromkeys(c.name for c in observed.reports if is_pending(c))),
    )


def recorded_keys(raw: object) -> frozenset[str]:
    """The failure keys a leg's receipt says it was handed. The receipt is
    harness-written (the accounting runtime copies the plan the harness
    built); a malformed one counts as nothing handed, so the failure is
    handed again rather than never (on its head and try it is not counted
    again; the lifetime cap bounds it)."""
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(k for k in raw[:MAX_FAILURES] if isinstance(k, str) and 0 < len(k) <= _MAX_KEY)


@dataclass(frozen=True)
class ChangeRequest:
    """The newest CHANGES_REQUESTED review on the head an ADDRESS_REVIEW
    answers: its reviewer, the head it was submitted on, its id when GitHub
    gave one (the agent reads the review's inline comments by it) and its
    body, which an inline-only review leaves empty."""

    author: str
    head: str
    review_id: int | None = None
    body: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.author, str) or not self.author.strip()
                or not isinstance(self.head, str) or not self.head or not isinstance(self.body, str)
                or self.review_id is not None and (type(self.review_id) is not int or self.review_id <= 0)):
            raise ValueError("a change request needs its reviewer, its head and a text body")


__all__ = ["MAX_FAILURES", "ChangeRequest", "CiFailure", "CiFailures", "failure_key", "new_failures",
           "began", "recorded_keys"]
