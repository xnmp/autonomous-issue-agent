"""Ceiling on consecutive failed PAID steward legs for one issue (ADR 0012).

`triage_candidates`/`grooming_candidates` select an issue whose recorded
baseline differs from its current `updatedAt`. The workflow records that
baseline only after a leg returns a usable verdict, so a leg that returns
nothing leaves the issue selected — and the next sweep buys the identical leg
again. Nothing bounded that loop: budget exhaustion is deliberately not an
infra death (`domain/outcome.py`), so the steward's infra-death breaker never
trips on it, and a $0.75 codex triage that always ends at its cap is bought
once per sweep, forever.

The bound is per ISSUE VERSION and counts attempts:

* an infra death (`KILLED_EXTERNALLY`/`TRANSPORT_DEATH`) is NOT charged — the
  session made no attempt, and the separate infra-death ceiling already covers
  "the session keeps dying";
* an ADMISSION REFUSAL is not charged either. The accounting runtime returns
  `dispatch_claim_unavailable`/`publication_authority_unavailable` with
  `ExitReason.AGENT_ERROR` when a target claim could not be bound or durable
  publication authority was missing — the agent never ran, and the condition is
  usually deployment-wide. Charging it meant three transient claim conflicts
  applied `needs-human-review` (a LIFECYCLE label) and permanently sidelined
  the issue; a deployment-wide occurrence sidelined the whole backlog;
* every other failure to produce a verdict IS charged, budget exhaustion
  included. The fix role treats agent errors, timeouts and empty results as
  strikes against a retry breaker; the steward had no breaker at all, so the
  same taxonomy applies here.

Counters are PER LEG (triage / groom): `grooming_candidates` has no untriaged
filter, so both legs run on the same issue on independent schedules, and a
groom leg reporting "no finding" must not erase the triage strikes that were
about to stop an unbuyable issue.

Once the ceiling trips the caller records the issue's current baseline (the
same durable marker a successful leg writes), which stops re-selection without
a second selection predicate, and surfaces an operator decision. A later human
edit or comment moves `updatedAt`, which re-selects the issue and starts a
fresh series.
"""

from __future__ import annotations

from .actions import Action, Target
from .labels import Label
from .lifecycle import attention_actions
from .outcome import (
    ADMISSION_REFUSAL_SUBTYPES,
    ExitReason,
    infra_death,
)

__all__ = [
    "ADMISSION_REFUSAL_SUBTYPES",
    "DEFAULT_STEWARD_ERROR_CEILING",
    "stall_charge",
    "steward_stall_actions",
    "steward_stall_tripped",
]

# Three, not ten: unlike a denied label write (one gh call), each of these
# attempts is a paid agent leg. Two failures are plausibly bad luck; a third on
# the identical issue text is a pattern no further spend will resolve.
DEFAULT_STEWARD_ERROR_CEILING = 3

def stall_charge(
    exit_reason: ExitReason | None, envelope_subtype: str = ""
) -> int:
    """How much a failed steward leg costs its issue's stall counter.

    The admission-refusal list now lives in `domain.outcome` beside the rest of
    the exit taxonomy: the fix role's quality breakers need the identical
    exemption (keifu #130), and two copies of "which failures are the harness's
    fault" is exactly the drift this module was written to prevent.

    Deliberately NOT exempting `RUNTIME_UNAVAILABLE`, even though it is
    infra-class everywhere in the fix role. The two roles bound their loops
    differently: a fix-role exemption pairs with the per-issue machine backoff
    (`domain.backoff`), while a zero charge here returns without recording the
    issue's baseline, so the issue stays selected and the identical paid leg is
    bought again every sweep — with no counter of its own to stop it (the
    steward's `exhausted()` reads an infra counter that only the RAISE path
    writes). Charging it keeps the loop bounded at the cost of a
    `needs-human-review` on an issue whose only sin was a broken deployment.
    Bounded-and-mislabelled beats unbounded-and-silent; the pre-existing
    `infra_death` exemption has the same hole and wants the same fix, which is
    the steward's own change to make.
    """
    if infra_death(exit_reason):
        return 0
    if envelope_subtype in ADMISSION_REFUSAL_SUBTYPES:
        return 0
    return 1


def steward_stall_tripped(stalls: int, ceiling: int) -> bool:
    """Whether this issue has spent its consecutive-failure budget.

    ``ceiling <= 0`` disables the bound (pre-ceiling behaviour); config
    validation rejects it as operator input.
    """
    return ceiling > 0 and stalls >= ceiling


def steward_stall_actions(
    issue: int,
    leg: str,
    stalls: int,
    *,
    budget: bool,
    detail: str = "",
) -> list[Action]:
    """The operator surface for a stalled issue.

    Budget exhaustion gets the advisory `needs-budget-review` label — the same
    signal the resolve family raises, and the same decision (change the cap or
    accept the stop). Any other repeated failure means the harness cannot
    classify this issue at all, which is what `needs-human-review` says; it is
    a lifecycle label, so the issue also leaves every automated selection set
    until a person removes it.
    """
    why = f" ({detail})" if detail.strip() else ""
    if budget:
        body = (
            f"**Budget review needed:** the {leg} leg reached its configured "
            f"spend cap {stalls} time(s) in a row on this issue without "
            f"returning a verdict{why}. No further {leg} legs will be bought "
            "for this version of the issue. Raise the steward leg budget "
            "(`STEWARD_TRIAGE_BUDGET_USD`) or narrow the issue; editing or "
            "commenting on the issue starts a fresh attempt."
        )
        label = Label.NEEDS_BUDGET_REVIEW
    else:
        body = (
            f"**Steward needs a human:** the {leg} leg failed {stalls} time(s) "
            f"in a row on this issue without returning a verdict{why}. No "
            f"further {leg} legs will be bought for it. Remove "
            f"`{Label.NEEDS_HUMAN_REVIEW.value}` once the cause is addressed "
            "to let the steward try again."
        )
        label = Label.NEEDS_HUMAN_REVIEW
    # `harness_signal=True` on both: these writes report the HARNESS's decision
    # to stop, not a claim about the issue's content. Minted with no expected
    # revision, so the optimistic guard cannot discard them when a human
    # comments in the same tick — the one moment the explanation matters most.
    # The bodies and labels are fixed harness-authored text, never agent
    # output, which is what makes the exemption safe (ADR 0012: a harness-owned
    # signal is minted in the harness's own trust domain). Delivery stays
    # bounded by the durable retry ceiling.
    return attention_actions(Target.ISSUE, issue, None, label, body, harness_signal=True)
