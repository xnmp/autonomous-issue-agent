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
    # Advisory steward marker: recent merged work may already satisfy this open
    # issue. A human decides whether to close it; it is never lifecycle state.
    MAYBE_RESOLVED = "maybe-resolved"

    # Terminal marker for an issue the STEWARD closed straight out of triage
    # (already fixed elsewhere, duplicate, invalid, resolved by a merged PR).
    # Deliberately NOT ai-done: ai-done means "a harness PR merged for this
    # issue", and `roles._recurrence_reporter` selects exactly open + ai-done +
    # stateReason=reopened to journal a regression of SHIPPED work. A steward
    # close ships no code, so reusing ai-done would book every human reopen of a
    # wontfix as a harness recurrence and corrupt that guardrail metric. Kept out
    # of LIFECYCLE_LABELS below so a reopened issue is an untriaged candidate
    # again — a human putting it back means triage starts over.
    AI_CLOSED = "ai-closed"

    # Steward outputs (never auto-promoted; a human disposes).
    AI_TRIAGE = "ai-triage"        # steward has seen it; awaiting author / human promote
    AI_SUGGESTION = "ai-suggestion"  # steward-filed suggestion issue
    NEEDS_DESIGN = "needs-design"  # an ADR must be approved first

    # Operator parked: a human owns this issue's PR(s) indefinitely, and the
    # queue must stop waiting for them. Removing ai-pr-open is what actually
    # frees the overlap serializer (the fixer's world only contains ai-pr-open
    # PRs); ai-parked records WHY the label went missing, keeps the steward
    # from re-triaging the issue, and gives the dashboard a lane to show it
    # in. Written only by the dashboard's park action (operator-owned,
    # ADR 0012); no daemon ever adds or removes it.
    AI_PARKED = "ai-parked"

    # Escalation.
    NEEDS_HUMAN_REVIEW = "needs-human-review"  # review/address-review exhausted
    # A runtime budget cap needs an operator to change the cap or resume from
    # the handover; it says nothing about the agent's capability.
    NEEDS_BUDGET_REVIEW = "needs-budget-review"

    # Priority (orthogonal to lifecycle; used only for ordering ai-ready).
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# Observations that coexist with pipeline state. A durable receipt for one of
# these must never supersede a lifecycle relabel on the same GitHub target.
ADVISORY_LABELS: frozenset[Label] = frozenset(
    {
        Label.RECURRENCE,
        Label.MAYBE_RESOLVED,
        Label.NEEDS_BUDGET_REVIEW,
    }
)


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
        Label.AI_PARKED,
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
