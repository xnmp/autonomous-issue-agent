"""Lifecycle labels — the only coordination channel between the daemons.

The bash harness coordinates ONLY through GitHub labels and comments. There is
no shared database; an issue's position in the pipeline IS its label set. This
module names those labels and the legal transitions, so the rest of the domain
can reason about state moves without hard-coding string literals everywhere.

Source of truth in the bash: agent/watcher.sh (relabel calls), cleanup.sh,
steward.sh, bin/bootstrap.sh (the label set installed on a target repo).
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

    # Reviewer trigger for an EXTERNAL repo (PRs not produced by this harness).
    # A separate `review` process can be pointed at any repo and told to look at
    # PRs carrying this tag instead of `ai-pr-open` (REVIEW_TRIGGER_LABEL). Kept
    # out of LIFECYCLE_LABELS below — that set governs the steward's ISSUE triage,
    # not PR selection.
    AI_REVIEW = "ai-review"        # human-tagged: "reviewer-bot, please review this PR"
    NO_AI_REVIEW = "no-ai-review"  # human opt-OUT: never auto-adopt this PR for review

    # ADVISORY marker: this issue re-reports something the harness already shipped
    # (a literal reopen, or a new issue the triage classifier judged a refiling).
    # Deliberately OUT of LIFECYCLE_LABELS: a recurrence is an OBSERVATION, not a
    # pipeline position, so labelling an issue must not make it look "triaged" nor
    # remove it from any selection set. Nothing is ever auto-reopened or
    # auto-dispatched from it — a false positive costs one label removal.
    RECURRENCE = "recurrence"

    # Steward outputs (never auto-promoted; a human disposes).
    AI_TRIAGE = "ai-triage"        # steward has seen it; awaiting author / human promote
    AI_SUGGESTION = "ai-suggestion"  # steward-filed suggestion issue
    NEEDS_DESIGN = "needs-design"  # an ADR must be approved first

    # Escalation.
    NEEDS_HUMAN_REVIEW = "needs-human-review"  # review/address-review exhausted

    # Priority (orthogonal to lifecycle; used only for ordering ai-ready).
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# The labels that mean "this issue is already somewhere in the pipeline". The
# steward's triage candidate set is exactly the OPEN issues carrying NONE of
# these (ai-triage included — see triage.py for why it's allowed back in).
# Mirrors the jq `select(... | not)` in steward.sh:triage_once.
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
    }
)

# Priority order for picking the next ai-ready issue (watcher.sh §1: P1<P2<P3,
# then everything else). Lower sort key = picked first.
_PRIORITY_RANK: dict[Label, int] = {Label.P1: 1, Label.P2: 2, Label.P3: 3}
DEFAULT_PRIORITY_RANK = 4


def priority_rank(labels: frozenset[Label]) -> int:
    """Sort key for an ai-ready issue: P1→1, P2→2, P3→3, none→4. Mirrors the
    jq priority expression in watcher.sh §1."""
    for label, rank in _PRIORITY_RANK.items():
        if label in labels:
            return rank
    return DEFAULT_PRIORITY_RANK
