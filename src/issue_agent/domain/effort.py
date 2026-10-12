"""Reasoning effort as domain vocabulary: how hard a leg thinks.

A property of the work, not of the runtime, so it sits beside `model` on the
task and each runner renders it in its own dialect. One configured effort per
role (refactor decision Q12); the evidence-driven escalation ladder was
deleted in refactor slice 5b.
"""

from __future__ import annotations

from enum import Enum


class Effort(Enum):
    """How much reasoning a leg is asked to spend. Deliberately NOT a `str`
    mixin: str comparisons would order these lexicographically. Values are the
    wire words both runtimes speak (codex `model_reasoning_effort`, the Claude
    SDK's `effort` option)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Weakest → strongest; the configurable choices.
EFFORT_LADDER: tuple[Effort, ...] = (Effort.LOW, Effort.MEDIUM, Effort.HIGH)

# Difficulty tiers the deleted router wrote into ROUTE journal events; the
# dashboard still reads them from historical journals.
TIER_LADDER: tuple[str, ...] = ("trivial", "standard", "complex", "exceptional")


def parse_effort(raw: object, default: Effort | None = None) -> Effort | None:
    """Total: any junk (typo, None, wrong type) → `default`. Operator knobs are
    free text and a misspelled `FIX_EFFORT` must not take a daemon down."""
    if isinstance(raw, Effort):
        return raw
    try:
        return Effort(str(raw).strip().lower())
    except (ValueError, AttributeError):
        return default
