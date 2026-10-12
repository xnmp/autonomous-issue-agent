"""Lifecycle labels — the only coordination channel between the daemons.

The bash harness coordinates ONLY through GitHub labels and comments. There is
no shared database; an issue's position in the pipeline IS its label set. This
module names those labels and the legal transitions, so the rest of the domain
can reason about state moves without hard-coding string literals everywhere.

Source of truth for lifecycle labels shared by decisions and setup tooling.
"""

from __future__ import annotations

from enum import Enum


class Label(str, Enum):
    """A pipeline / lifecycle label. `str` mixin so values compare to raw
    GitHub label strings transparently (Label.AI_READY == "ai-ready")."""

    # Issue lifecycle (the "front door" → done path).
    AI_READY = "ai-ready"          # vetted, queued for the fixer
    AI_WORKING = "ai-working"      # a fixer (or resume) holds it right now
    AI_BLOCKED = "ai-blocked"      # waiting on a human reply
    AI_PR_OPEN = "ai-pr-open"      # a PR exists (also applied to the PR itself)
    AI_DONE = "ai-done"            # merged

    # Retired review-selection labels (slice 5c reviews harness branches
    # only); still named so existing GitHub labels parse.
    AI_REVIEW = "ai-review"        # human-tagged: "reviewer-bot, please review this PR"
    NO_AI_REVIEW = "no-ai-review"  # human opt-OUT: never auto-adopt this PR for review

    # ADVISORY marker: this issue re-reports something the harness already shipped.
    # Deliberately OUT of LIFECYCLE_LABELS: a recurrence is an OBSERVATION, not a
    # pipeline position, so labelling an issue must not make it look "triaged" nor
    # remove it from any selection set. Nothing is ever auto-reopened or
    # auto-dispatched from it — a false positive costs one label removal.
    RECURRENCE = "recurrence"
    # Advisory marker: recent merged work may already satisfy this open issue.
    # A human decides whether to close it; it is never lifecycle state.
    MAYBE_RESOLVED = "maybe-resolved"

    # Terminal marker for an issue closed without a harness merge (written by the
    # deleted steward role; still honoured as terminal on existing issues).
    # Deliberately NOT ai-done: ai-done means "a harness PR merged for this issue".
    AI_CLOSED = "ai-closed"

    # Written by the deleted steward role. Kept because issue labels are captured
    # in persisted completion/hold guards, which must keep decoding.
    AI_TRIAGE = "ai-triage"
    AI_SUGGESTION = "ai-suggestion"  # suggestion issue; never auto-promoted
    NEEDS_DESIGN = "needs-design"  # an ADR must be approved first

    # Operator parked: a human owns this issue's PR(s) indefinitely, and the
    # queue must stop waiting for them. Removing ai-pr-open is what actually
    # frees the overlap serializer (the fixer's world only contains ai-pr-open
    # PRs); ai-parked records WHY the label went missing and gives the
    # dashboard a lane to show it in. Written only by the dashboard's park action (operator-owned,
    # ADR 0012); no daemon ever adds or removes it.
    AI_PARKED = "ai-parked"

    # The single stop of the two-label design (docs/open/2026-10-12-minimal-harness-design.md):
    # only policy writes it; a human reply or removing it releases the item.
    NEEDS_HUMAN = "needs-human"

    # Escalation.
    NEEDS_HUMAN_REVIEW = "needs-human-review"  # review/address-review exhausted
    # A runtime budget cap needs an operator to change the cap or resume from
    # the handover; it says nothing about the agent's capability.
    NEEDS_BUDGET_REVIEW = "needs-budget-review"

    # Priority (orthogonal to lifecycle; used only for ordering ai-ready).
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# The labels that mean "this issue is already somewhere in the pipeline".
LIFECYCLE_LABELS: frozenset[Label] = frozenset(
    {
        Label.AI_READY,
        Label.AI_WORKING,
        Label.AI_BLOCKED,
        Label.AI_PR_OPEN,
        Label.AI_DONE,
        Label.AI_SUGGESTION,
        Label.NEEDS_DESIGN,
        Label.NEEDS_HUMAN_REVIEW,
        Label.AI_PARKED,
    }
)

