"""Effort and the escalation ladder — how hard a leg thinks, as domain vocabulary.

Harness #16. Two things lived only in adapters before this module:

* **Reasoning effort** was a runtime-specific string (`CODEX_REASONING_EFFORT`
  → `-c model_reasoning_effort=…`), and the Claude SDK path had no notion of it
  at all. It is a property of the WORK ("this issue deserves more thinking"),
  not of the runtime, so it belongs beside `model` on the task and is rendered
  per-runtime at the edge.

* **"Try harder next time"** had nowhere to live. The difficulty router (#8)
  classifies an issue once, from its text, before anything has been attempted.
  When the harness later learns the attempt under-delivered — the issue came
  back (#10), or the resolve leg failed and is being retried — the retry ran
  the identical configuration and re-derived the identical (wrong) verdict.

The ladder here turns that evidence into ONE pure function. `EscalationSignals`
counts the evidence, `escalate` spends it: **effort first (cheap), tier second**,
capped at (strongest tier, HIGH). Nothing in this module does I/O, knows about
GitHub, or knows which runtime will execute the result — `app.workflows.fix`
gathers the signals, `app.planners.tasks` maps a tier to a model, and the two
runner adapters render `Effort` in their own dialect.

**Monotonicity** (the anti-thrash rule from #16) is a property of `escalate`
itself — it can only ever return a stance at least as strong as the one it was
given, for any notch count. It is deliberately NOT a stored high-water mark, so
evidence that is genuinely resolved stops costing money: an APPROVE clears the
outstanding change requests, and the next leg drops back down. What must never
happen is a stance oscillating WITHIN one unresolved attempt, and none of the
inputs move backwards there. The router's verdict is a FLOOR that escalation
raises, never a value escalation can lower.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Effort(Enum):
    """How much reasoning a leg is asked to spend. Ordered LOW < MEDIUM < HIGH
    via `EFFORT_LADDER` — deliberately NOT a `str` mixin, because inheriting
    str's comparisons would order these lexicographically ("high" < "low" <
    "medium"), which is exactly backwards for the two middle cases. Values are
    the wire words both runtimes already speak (codex
    `model_reasoning_effort`, the Claude SDK's `effort` option)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Weakest → strongest. The ONE place either ladder's order is written down.
EFFORT_LADDER: tuple[Effort, ...] = (Effort.LOW, Effort.MEDIUM, Effort.HIGH)

# Difficulty tiers, weakest → strongest. `app.planners.tasks.MODEL_TIERS` maps
# each to a model per fixer runtime; this module only needs their ORDER.
TIER_LADDER: tuple[str, ...] = ("trivial", "standard", "complex")

# What an unclassifiable tier prices as. Same choice `route_model` makes for an
# unknown tier: a classifier misfire must degrade to a sane middle, never raise.
DEFAULT_TIER: str = "standard"

DEFAULT_EFFORT: Effort = Effort.MEDIUM


def parse_effort(raw: object, default: Effort | None = None) -> Effort | None:
    """Total: any junk (typo, None, wrong type) → `default`. Operator knobs are
    free text and a misspelled `FIX_EFFORT` must not take a daemon down."""
    if isinstance(raw, Effort):
        return raw
    try:
        return Effort(str(raw).strip().lower())
    except (ValueError, AttributeError):
        return default


def effort_rank(effort: Effort) -> int:
    """Position on `EFFORT_LADDER`; an unknown member prices as DEFAULT_EFFORT."""
    try:
        return EFFORT_LADDER.index(effort)
    except ValueError:
        return EFFORT_LADDER.index(DEFAULT_EFFORT)


def tier_rank(tier: str) -> int:
    """Position on `TIER_LADDER`; an unknown tier prices as DEFAULT_TIER —
    matching `route_model`'s totality, so a garbage classifier answer escalates
    from the same floor it would have been dispatched on."""
    try:
        return TIER_LADDER.index(tier)
    except ValueError:
        return TIER_LADDER.index(DEFAULT_TIER)


@dataclass(frozen=True)
class Stance:
    """The (difficulty tier, reasoning effort) pair one leg runs at. The router
    produces the initial stance; `escalate` returns a stronger one."""

    tier: str = DEFAULT_TIER
    effort: Effort = DEFAULT_EFFORT


@dataclass(frozen=True)
class EscalationSignals:
    """Evidence that the previous configuration under-delivered.

    All three are already recorded by the harness — no new sensors (#16):
    `recurrence` is the advisory label the steward applies (#10),
    `resolve_retries` is the fixer's own non-infra failure counter, and
    `review_rounds` is how many CHANGES_REQUESTED reviews are outstanding on
    the PR (`domain.review.count_outstanding_change_requests`, derived from
    GitHub state — NOT the store's `review-retries`, which counts attempts at
    one review and re-keys to zero whenever the reviewer comes back). Infra
    deaths are deliberately absent: a session that died before returning made
    no attempt, so it is no evidence at all about difficulty — the same
    exemption the retry breakers make.
    """

    recurrence: bool = False
    resolve_retries: int = 0
    review_rounds: int = 0

    def notches(self) -> int:
        """How many rungs of evidence exist. One per signal (#16).

        `review_rounds` is discounted by one because the FIRST REQUEST_CHANGES
        is the normal path — a review that asks for changes once is the system
        working, not the model under-delivering. A recurrence or a failed
        resolve, by contrast, is evidence from the first occurrence: both mean
        an attempt already ran to completion and did not hold up.
        """
        return (
            (1 if self.recurrence else 0)
            + max(0, self.resolve_retries)
            + max(0, self.review_rounds - 1)
        )


def escalate(stance: Stance, notches: int) -> Stance:
    """Spend `notches` of evidence on `stance`, effort first, then tier.

    Effort is the cheap lever (same model, more thinking), so it is spent
    before the expensive one (a stronger, pricier tier). At the ceiling
    (strongest tier, HIGH) further notches are absorbed: the next escalation
    step past this ladder is a human, which the retry breakers already own.

    Total and never-weakening: a negative or absurd notch count, an unknown
    tier, or an unknown effort all resolve to a stance at least as strong as
    the input's ladder position.
    """
    remaining = max(0, int(notches))

    ei = effort_rank(stance.effort)
    new_ei = min(ei + remaining, len(EFFORT_LADDER) - 1)
    remaining -= new_ei - ei

    ti = tier_rank(stance.tier)
    new_ti = min(ti + remaining, len(TIER_LADDER) - 1)

    return Stance(tier=TIER_LADDER[new_ti], effort=EFFORT_LADDER[new_ei])
