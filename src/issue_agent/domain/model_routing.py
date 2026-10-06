"""Validated provider/model selections for the existing difficulty ladder."""

from dataclasses import dataclass
import re

from .agent_task import RuntimeName
from .effort import TIER_LADDER


@dataclass(frozen=True)
class ModelRoute:
    tier: str
    runtime: RuntimeName
    model: str

    def __post_init__(self) -> None:
        if self.tier not in TIER_LADDER or not isinstance(self.runtime, RuntimeName):
            raise ValueError("model route requires a supported tier and runtime")
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\[\]-]{0,199}", self.model):
            raise ValueError("model route requires a nonempty model ID without whitespace")
        if (self.model.startswith("gpt-") and self.runtime is not RuntimeName.CODEX
                or self.model.startswith("claude-") and self.runtime is not RuntimeName.CLAUDE
                or self.model in {"haiku", "sonnet", "opus", "fable"} and self.runtime is not RuntimeName.CLAUDE
                or self.model.startswith("glm-") and self.runtime is not RuntimeName.GLM):
            raise ValueError("model ID belongs to a different runtime")


def parse_model_route(tier: str, value: str) -> ModelRoute:
    """The explicit runtime prefix prevents a provider switch from becoming a model alias."""
    if not isinstance(value, str):
        raise ValueError("model route must be runtime:model text")
    runtime, separator, model = value.strip().partition(":")
    if not separator:
        raise ValueError("expected runtime:model (claude, codex or glm)")
    return ModelRoute(tier, RuntimeName(runtime), model)
