"""Per-issue CI failure history — the diagnostic context a repair leg lacked.

Incident (tauri PR #620, 2026-08-09): three FIX_CI legs at ~$6 each each saw
ONLY the current failure, so none could see the pattern a human saw in ten
seconds — the SAME check failing on every attempt with a DIFFERENT symptom each
time, localized to a spec file the PR itself adds. That shape is a PR-authored
flaky test, not a program-logic defect; the next $0.35 round fixed it once the
diagnosis was stated. The legs were not under-reasoning, they were under-informed.

Provenance (ADR 0012), stated per field because the two halves differ:

  * `head_sha` is harness-observed — the head the harness itself dispatched
    against, read from the PR view;
  * `failed_checks` are NOT harness-owned. A check name is whatever a workflow
    file or an installed GitHub App calls a job, and the workflow files in
    question live on the PR's OWN branch, which a fixer leg writes. They are the
    same strings `workflows.fix` already brands `as_untrusted` before fencing
    them into the live prompt, so they are an untrusted claim here too — with
    the aggravation that these are PERSISTED and replayed into every later leg.
    The prompt builder fences the recorded list exactly like the live one, and
    `_clean` below collapses control characters so a name cannot forge a line of
    the harness's own record.

The record is derived and bounded (last `CI_HISTORY_LIMIT` attempts): losing it
costs context on the next repair, never correctness, and it grants no authority
— the prompt section it feeds is information, not permission.

Pure: parsing, bounding, and normalization only. The JSON encoding and the file
live in `adapters.store`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Why five: the escalation ceilings that bound a repair loop (`max_ci_retries`,
# defaulting to 3) trip well inside this, so five always covers a whole PR's CI
# life while keeping the section short. A longer tail would add tokens to every
# leg for attempts no live loop reaches.
CI_HISTORY_LIMIT = 5

# Per-attempt bounds. A CI configuration with hundreds of jobs, or a check name
# echoing a long branch/matrix expansion, must not be able to grow a prompt
# without limit — and since check names are repository-authored (see the module
# docstring), "bounded" here is a security property, not just a cost one. The
# stated worst case is what a FIX_CI prompt pays: 5 × 10 × 120 ≈ 6 KB of section
# before JSON encoding, against a north star of cost per merged PR.
MAX_CHECKS_PER_ATTEMPT = 10
MAX_FIELD_CHARS = 120


@dataclass(frozen=True)
class CiAttempt:
    """One dispatched FIX_CI leg: what it was pointed at, and what was red."""

    head_sha: str
    failed_checks: tuple[str, ...] = ()

    def short_sha(self) -> str:
        return self.head_sha[:7] if self.head_sha else "unknown"

    def is_empty(self) -> bool:
        """No head, no checks — nothing an attempt list can say about it."""
        return not self.head_sha and not self.failed_checks


def _clean(value: object) -> str:
    """One bounded, single-line field.

    Interior whitespace collapses, not just the ends: a check name containing a
    newline would otherwise render as an extra line of the attempt list, forging
    a harness record entry ("attempt 9 @ deadbee: <instructions>") that reads
    exactly like the lines around it. The fence in the prompt builder is the
    primary guard; this keeps the stored value single-line so no later renderer
    can reintroduce the same hole.
    """
    return " ".join(str(value).split())[:MAX_FIELD_CHARS] if isinstance(value, str) else ""


def make_attempt(head_sha: object, failed_checks: object) -> CiAttempt:
    """A normalized attempt from whatever the observed world carried.

    Absent/odd inputs degrade to empty strings rather than raising: a missing
    head SHA still leaves a usable attempt (the pattern that matters is "the
    same check failed again"), and raising here would restore the blind
    behaviour this module exists to end.
    """
    names: list[str] = []
    if isinstance(failed_checks, str):
        candidates: Iterable[object] = failed_checks.splitlines()
    elif isinstance(failed_checks, Iterable):
        candidates = failed_checks
    else:
        candidates = ()
    for raw in candidates:  # bounded BEFORE materializing — the source may be huge
        if len(names) >= MAX_CHECKS_PER_ATTEMPT:
            break
        name = _clean(raw)
        if name and name not in names:
            names.append(name)
    return CiAttempt(head_sha=_clean(head_sha), failed_checks=tuple(names))


def append_attempt(
    history: Sequence[CiAttempt],
    attempt: CiAttempt,
    limit: int = CI_HISTORY_LIMIT,
) -> tuple[CiAttempt, ...]:
    """`history` plus `attempt`, keeping only the most recent `limit`.

    An empty attempt is dropped rather than stored: it carries no signal, and
    the slots are scarce — five of them would evict a real record and then
    renumber the survivors, which is worse than having no history at all.
    Counting attempts is `ci-retries`' job, not this list's."""
    if attempt.is_empty():
        return tuple(history)[-limit:] if limit > 0 else ()
    kept = (*history, attempt)
    return kept[-limit:] if limit > 0 else ()


def parse_ci_history(raw: object, limit: int = CI_HISTORY_LIMIT) -> tuple[CiAttempt, ...]:
    """Decoded-JSON → attempts, tolerating every corruption shape.

    A history file is an optimization, so any damage reads as "no history": a
    torn write, a hand-edited file, or a shape from a future version must cost
    the leg its extra context, never its dispatch. Individually unreadable
    entries are DROPPED, not kept as blanks — a blank would still occupy a
    numbered line of the rendered list and shift every real attempt's number.
    """
    if not isinstance(raw, list):
        return ()
    attempts: list[CiAttempt] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        attempt = make_attempt(entry.get("head_sha"), entry.get("failed_checks"))
        if not attempt.is_empty():
            attempts.append(attempt)
    return tuple(attempts[-limit:]) if limit > 0 else ()


def ci_history_payload(history: Sequence[CiAttempt]) -> list[dict[str, object]]:
    """JSON-ready mirror of `parse_ci_history`'s input shape."""
    return [
        {"head_sha": a.head_sha, "failed_checks": list(a.failed_checks)}
        for a in history
    ]
