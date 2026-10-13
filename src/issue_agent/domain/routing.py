"""Difficulty tiers and the model a tier routes to. Pure.

TRIAGE grades each admitted issue once; every routed fix act (RESOLVE, FIX_CI,
ADDRESS_REVIEW, REBASE) runs on its tier's configured `runtime:model`. The
k-th attempt of an act runs one rung stronger per earlier attempt in its
attempt run (the run derive already counts), capped at XHIGH: escalation is
derived, never stored. REVIEW runs at the tier of the fix leg that produced
the head it reviews (owner, 2026-10-13: the reviewer matches the fixer).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .agent_task import RuntimeName


class Tier(Enum):
    LOW = "low"
    MED = "med"
    HIGH = "high"
    XHIGH = "xhigh"


TIER_LADDER: tuple[Tier, ...] = (Tier.LOW, Tier.MED, Tier.HIGH, Tier.XHIGH)
# What an issue with no usable triage runs at: a misfire degrades to the middle.
DEFAULT_TIER = Tier.MED


def parse_tier(raw: object) -> Tier | None:
    """Total: anything but an exact tier word is None."""
    return next((t for t in TIER_LADDER if raw == t.value), None)


def escalate(tier: Tier, attempts: int) -> Tier:
    """The tier for an act's next attempt, given its `attempts` earlier ones
    in the current attempt run. Never weaker than `tier`; absurd input clamps."""
    steps = attempts if type(attempts) is int and attempts > 0 else 0
    return TIER_LADDER[min(TIER_LADDER.index(tier) + steps, len(TIER_LADDER) - 1)]


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\[\]-]{0,199}")
_CLAUDE_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable"})


@dataclass(frozen=True)
class ModelRoute:
    """One `runtime:model` spec. The explicit runtime prefix keeps a provider
    switch from becoming a model alias the other runtime cannot resolve."""

    runtime: RuntimeName
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("model route requires a supported runtime")
        if not isinstance(self.model, str) or not _MODEL_ID.fullmatch(self.model):
            raise ValueError("model route requires a nonempty model ID without whitespace")
        claude_model = self.model.startswith("claude-") or self.model in _CLAUDE_ALIASES
        if (self.model.startswith("gpt-") and self.runtime is not RuntimeName.CODEX
                or claude_model and self.runtime is not RuntimeName.CLAUDE):
            raise ValueError("model ID belongs to a different runtime")

    @property
    def spec(self) -> str:
        return f"{self.runtime.value}:{self.model}"


def parse_model_route(value: object) -> ModelRoute:
    """`runtime:model`, e.g. `codex:gpt-6-luna` or `claude:claude-haiku-5-5`."""
    if not isinstance(value, str):
        raise ValueError("model route must be runtime:model text")
    runtime, separator, model = value.strip().partition(":")
    if not separator:
        raise ValueError("expected runtime:model (claude or codex)")
    try:
        name = RuntimeName(runtime)
    except ValueError:
        raise ValueError(f"unknown runtime {runtime!r} (claude or codex)") from None
    return ModelRoute(name, model)


__all__ = ["DEFAULT_TIER", "TIER_LADDER", "ModelRoute", "Tier", "escalate", "parse_model_route", "parse_tier"]
