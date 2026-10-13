"""Provider-neutral agent intent and runtime capability vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .effort import Effort


class RuntimeName(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


def run_input_fingerprint(intent: RunIntent) -> str:
    """Identify supplied requirements/instructions, not provider selection or cost."""
    if not isinstance(intent, RunIntent):
        raise ValueError("input fingerprint requires a validated run intent")
    value = json.dumps(
        (intent.prompt, intent.system_instruction, intent.schema),
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    )
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class RunIntent:
    """Provider-neutral request.

    SDK tool names, MCP configuration, environment, worktrees, and resume
    tokens belong to the adapter-layer ``RuntimePlan`` and cannot enter this
    domain object.
    """

    prompt: str
    schema: dict | None = None
    system_instruction: str = ""
    model: str | None = None
    effort: Effort | None = None
    maximum_budget_usd: float = 3.0
