"""Pure validation and actions for steward GROOM lifecycle dispositions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .actions import Action, CloseIssue, Relabel, Target
from .labels import Label
from .lifecycle import LifecycleOperation, completion_guard
from .world import IssueView


_MAX_REASON_LENGTH = 500
_MAX_ISSUE_NUMBER = 2_147_483_647
_ACTIVE_DUPLICATE_SOURCE_LABELS = frozenset(
    {
        Label.AI_READY,
        Label.AI_WORKING,
        Label.AI_PR_OPEN,
        Label.AI_BLOCKED,
        Label.NEEDS_HUMAN_REVIEW,
        Label.AI_DONE,
    }
)


@dataclass(frozen=True)
class DuplicateOfClaim:
    """An agent-authored claim which has passed shape validation only."""

    issue: int
    reason: str


@dataclass(frozen=True)
class ValidatedDuplicateOf:
    """Proof that a harness-completed issue covers this inactive issue."""

    issue: int
    reason: str


class DuplicateRefusal(str, Enum):
    PROMOTE_DISABLED = "promote_disabled"
    SELF_REFERENCE = "self_reference"
    LIFECYCLE_ACTIVE = "lifecycle_active"
    REOPENED = "reopened"
    REFERENCE_MISSING = "reference_missing"
    REFERENCE_MISMATCH = "reference_mismatch"
    REFERENCE_OPEN = "reference_open"
    REFERENCE_NOT_HARNESS_DONE = "reference_not_harness_done"


@dataclass(frozen=True)
class RefusedDuplicateOf:
    """Harness-owned reason an otherwise well-shaped claim was not trusted."""

    issue: int
    refusal: DuplicateRefusal


DuplicateDecision = ValidatedDuplicateOf | RefusedDuplicateOf


def parse_groom_disposition(value: object) -> DuplicateOfClaim | None:
    """Parse the only supported untrusted GROOM lifecycle claim.

    ``already_fixed`` is deliberately unsupported: the steward's current world
    has no harness-owned link from an arbitrary open defect to a merged PR.
    """
    if not isinstance(value, dict) or set(value) != {"kind", "issue", "reason"}:
        return None
    issue = value.get("issue")
    reason = value.get("reason")
    if (
        value.get("kind") != "duplicate_of"
        or not isinstance(issue, int)
        or isinstance(issue, bool)
        or not 0 < issue <= _MAX_ISSUE_NUMBER
        or not isinstance(reason, str)
    ):
        return None
    reason = reason.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        return None
    return DuplicateOfClaim(issue, reason)


def validate_duplicate_of(
    current: IssueView,
    claim: DuplicateOfClaim,
    referenced: IssueView | None,
    *,
    promote: bool,
) -> DuplicateDecision:
    """Launder an agent claim through harness-owned lifecycle observations."""
    if not promote:
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.PROMOTE_DISABLED)
    if claim.issue == current.number:
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.SELF_REFERENCE)
    if current.labels & _ACTIVE_DUPLICATE_SOURCE_LABELS:
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.LIFECYCLE_ACTIVE)
    if current.state_reason.strip().casefold() == "reopened":
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.REOPENED)
    if referenced is None:
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.REFERENCE_MISSING)
    if referenced.number != claim.issue:
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.REFERENCE_MISMATCH)
    if referenced.state.strip().casefold() != "closed":
        return RefusedDuplicateOf(claim.issue, DuplicateRefusal.REFERENCE_OPEN)
    if Label.AI_DONE not in referenced.labels:
        return RefusedDuplicateOf(
            claim.issue,
            DuplicateRefusal.REFERENCE_NOT_HARNESS_DONE,
        )
    return ValidatedDuplicateOf(claim.issue, claim.reason)


def duplicate_close_actions(
    current: IssueView,
    disposition: ValidatedDuplicateOf,
    *,
    triage_label: Label = Label.AI_TRIAGE,
) -> tuple[Action, ...]:
    """Close a harness-validated duplicate through the durable action rail."""
    # A code span prevents every agent-authored Markdown construct, including
    # mentions, links, and images, from rendering. Removing supplied backticks
    # keeps the attacker from terminating that harness-owned span.
    reason = disposition.reason.replace("`", "")
    body = (
        "**Closing this issue as a validated duplicate 🗂️**\n\n"
        f"@{current.author} the steward validated that #{disposition.issue} "
        "exists and is closed. No separate implementation is needed here.\n\n"
        f"**Groom finding:** `{reason}`\n\n"
        f"**Reopen this issue if:** #{disposition.issue} does not cover the "
        "reported defect.\n\n"
        "_Closed by the steward after harness-side validation. If this is wrong, "
        "just reopen it — a reopened issue is never auto-closed a second time._"
    )
    return (
        CloseIssue(current.number, "completed", body,
                   expected_revision=current.updated_at,
                   completion_guard=completion_guard(current, LifecycleOperation.CLOSE),
                   remove_labels=(triage_label,), add_labels=(Label.AI_CLOSED,)),
    )


__all__ = [
    "DuplicateDecision",
    "DuplicateOfClaim",
    "DuplicateRefusal",
    "RefusedDuplicateOf",
    "ValidatedDuplicateOf",
    "duplicate_close_actions",
    "parse_groom_disposition",
    "validate_duplicate_of",
]
