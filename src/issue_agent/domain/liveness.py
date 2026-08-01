"""Stall detection (ADR 0012): no target may be silently stuck forever.

2026-08-01's supervised run surfaced a family of failures with one shape — a
target sat in a non-terminal state indefinitely while every per-path guard
believed things were fine: an APPROVED PR whose advisory-failed check could
never re-run (UNSTABLE masks BEHIND), a visual-AC resolve dead-ended on a
runtime that cannot mint receipts, a review deferral loop with no counter.
Each got a targeted fix, but the class is open-ended: the ways to get stuck
grow faster than the paths anyone remembers to bound.

This module measures stuckness at the STATE BOUNDARY instead: a target whose
observable signature (labels, head, review, checks, merge state) has not
changed for a threshold emits an escalating stall signal, regardless of why.
Telemetry only — it never mutates lifecycle — so a false positive costs one
glanceable journal line, never a blocked leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping

from .labels import Label
from .world import IssueView, PRView, WorldState

# One signal per crossed multiple of this threshold (level 1 at 30 min,
# level 2 at 60, …), so a long stall stays visible without per-poll spam.
STALL_THRESHOLD_SECONDS = 1800.0

# Issue labels that mean the harness (or a human it explicitly asked) still
# owes this target progress. An unlabeled open issue is simply not ours.
_WATCHED_ISSUE_LABELS = frozenset(
    {
        Label.AI_READY,
        Label.AI_WORKING,
        Label.AI_BLOCKED,
        Label.AI_PR_OPEN,
        Label.NEEDS_HUMAN_REVIEW,
    }
)


@dataclass(frozen=True)
class Stall:
    kind: str  # "pr" | "issue"
    number: int
    age_seconds: float
    level: int  # how many whole thresholds have elapsed (≥ 1)
    signature: str


def pr_signature(pr: PRView) -> str:
    """Everything observable that changes when a PR makes ANY progress."""
    labels = ",".join(sorted(label.value for label in pr.labels))
    reviews = ";".join(
        f"{r.state}@{r.commit_id or ''}" for r in pr.reviews[-4:]
    )
    checks = ",".join(
        sorted(f"{c.name}:{c.bucket}" for c in pr.checks)
    )
    return "|".join(
        (
            pr.state,
            pr.head_ref_oid,
            labels,
            reviews,
            checks,
            pr.merge_state,
        )
    )


def issue_signature(issue: IssueView) -> str:
    labels = ",".join(sorted(label.value for label in issue.labels))
    # updated_at moves on comments — conversation IS progress for an issue
    # (an ai-blocked issue waiting on a human reply is only stalled while
    # nobody has replied).
    return "|".join((issue.state, labels, issue.updated_at))


def _watched(world: WorldState) -> list[tuple[str, int, str]]:
    targets: list[tuple[str, int, str]] = []
    for pr in world.prs:
        if pr.state == "OPEN":
            targets.append(("pr", pr.number, pr_signature(pr)))
    for issue in world.issues:
        if issue.state == "open" and issue.labels & _WATCHED_ISSUE_LABELS:
            targets.append(("issue", issue.number, issue_signature(issue)))
    return targets


def _age_seconds(now: str, since: str) -> float | None:
    try:
        n = datetime.fromisoformat(now.replace("Z", "+00:00"))
        s = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (n - s).total_seconds()


def detect_stalls(
    world: WorldState,
    seen: Mapping[str, tuple[str, str]],
    threshold_seconds: float = STALL_THRESHOLD_SECONDS,
) -> tuple[tuple[Stall, ...], dict[str, tuple[str, str]]]:
    """Return (stalls, next_seen).

    `seen` maps "pr:100"/"issue:7" → (signature, since_iso): the signature
    last observed and WHEN it first appeared. A changed signature resets the
    clock; an unchanged one past N whole thresholds yields a Stall at level N.
    Targets that left the watched set fall out of `next_seen` (their marker
    is retired by the caller's normal cleanup). An unstamped world (now="")
    tracks signatures but never ages them — a missing clock must not read as
    "infinitely stalled"."""
    stalls: list[Stall] = []
    next_seen: dict[str, tuple[str, str]] = {}
    for kind, number, signature in _watched(world):
        key = f"{kind}:{number}"
        prior = seen.get(key)
        if prior is None or prior[0] != signature:
            next_seen[key] = (signature, world.now)
            continue
        next_seen[key] = prior
        if not world.now or not prior[1]:
            continue
        age = _age_seconds(world.now, prior[1])
        if age is None or age < threshold_seconds:
            continue
        stalls.append(
            Stall(
                kind=kind,
                number=number,
                age_seconds=age,
                level=int(age // threshold_seconds),
                signature=signature,
            )
        )
    return tuple(stalls), next_seen
