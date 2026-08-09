"""Per-issue CI failure history — the diagnostic context a repair leg lacked.

Incident (tauri PR #620, 2026-08-09): three FIX_CI legs at ~$6 each each saw
ONLY the current failure, so none could see the pattern a human saw in ten
seconds — the SAME check failing on every attempt with a DIFFERENT symptom each
time, localized to a spec file the PR itself adds. That shape is a PR-authored
flaky test, not a program-logic defect; the next $0.35 round fixed it once the
diagnosis was stated. The legs were not under-reasoning, they were under-informed.

Provenance (ADR 0012): every field here is a harness-observed CI fact — the head
SHA the harness dispatched against and the check names `gh pr checks` bucketed as
failures. Nothing below is agent- or commenter-writable, so this is trusted
harness-owned state, not a claim. It is derived and bounded (last
`CI_HISTORY_LIMIT` attempts): losing it costs context on the next repair, never
correctness, and it grants no authority — the prompt section it feeds is
information, not permission.

Pure: parsing, bounding, and normalization only. The JSON encoding and the file
live in `adapters.store`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Why five: the escalation ceilings that bound a repair loop (`max_ci_retries`,
# defaulting to 3) trip well inside this, so five always covers a whole PR's CI
# life while keeping the prompt section a handful of lines. A longer tail would
# add tokens to every leg for attempts no live loop reaches.
CI_HISTORY_LIMIT = 5

# Per-attempt bounds. A CI configuration with hundreds of jobs, or a check name
# echoing a long branch/matrix expansion, must not be able to grow a prompt
# without limit — the file is trusted, but "trusted" is not "small".
MAX_CHECKS_PER_ATTEMPT = 20
MAX_FIELD_CHARS = 200


@dataclass(frozen=True)
class CiAttempt:
    """One dispatched FIX_CI leg: what it was pointed at, and what was red."""

    head_sha: str
    failed_checks: tuple[str, ...] = ()

    def short_sha(self) -> str:
        return self.head_sha[:7] if self.head_sha else "unknown"


def _clean(value: object) -> str:
    return str(value).strip()[:MAX_FIELD_CHARS] if isinstance(value, str) else ""


def make_attempt(head_sha: object, failed_checks: object) -> CiAttempt:
    """A normalized attempt from whatever the observed world carried.

    Absent/odd inputs degrade to empty strings rather than raising: a missing
    head SHA still makes the attempt COUNT (the pattern that matters is "the
    same check failed again"), and an attempt that cannot be recorded at all
    would silently restore the blind behaviour this module exists to end.
    """
    names: list[str] = []
    if isinstance(failed_checks, str):
        candidates: Sequence[object] = failed_checks.splitlines()
    elif isinstance(failed_checks, Iterable):
        candidates = list(failed_checks)
    else:
        candidates = ()
    for raw in candidates:
        name = _clean(raw)
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_CHECKS_PER_ATTEMPT:
            break
    return CiAttempt(head_sha=_clean(head_sha), failed_checks=tuple(names))


def append_attempt(
    history: Sequence[CiAttempt],
    attempt: CiAttempt,
    limit: int = CI_HISTORY_LIMIT,
) -> tuple[CiAttempt, ...]:
    """`history` plus `attempt`, keeping only the most recent `limit`."""
    kept = (*history, attempt)
    return kept[-limit:] if limit > 0 else ()


def parse_ci_history(raw: object, limit: int = CI_HISTORY_LIMIT) -> tuple[CiAttempt, ...]:
    """Decoded-JSON → attempts, tolerating every corruption shape.

    A history file is an optimization, so any damage reads as "no history": a
    torn write, a hand-edited file, or a shape from a future version must cost
    the leg its extra context, never its dispatch.
    """
    if not isinstance(raw, list):
        return ()
    attempts = [
        make_attempt(entry.get("head_sha"), entry.get("failed_checks"))
        for entry in raw
        if isinstance(entry, dict)
    ]
    return tuple(attempts[-limit:]) if limit > 0 else ()


def ci_history_payload(history: Sequence[CiAttempt]) -> list[dict[str, object]]:
    """JSON-ready mirror of `parse_ci_history`'s input shape."""
    return [
        {"head_sha": a.head_sha, "failed_checks": list(a.failed_checks)}
        for a in history
    ]
