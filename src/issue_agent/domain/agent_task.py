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
    GLM = "glm"


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
class RuntimeCapabilities:
    supports_resume: bool
    resume_preserves_sandbox: bool
    renders_images: bool
    native_budget_enforcement: bool
    requires_prepared_home: bool
    supported_task_fields: frozenset[str]
    def supports(self, field_name: str) -> bool:
        return field_name in self.supported_task_fields


TASK_FIELD_NAMES = frozenset(
    {
        "prompt",
        "schema",
        "append_system_prompt",
        "allowed_tools",
        "disallowed_tools",
        "model",
        "effort",
        "max_budget_usd",
        "cwd",
        "resume",
        "fork_session",
        "env",
        "mcp_servers",
        "subagents",
    }
)

RUNTIME_CAPABILITIES = {
    RuntimeName.CLAUDE: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=True,
        renders_images=True,
        native_budget_enforcement=True,
        requires_prepared_home=False,
        supported_task_fields=TASK_FIELD_NAMES,
    ),
    RuntimeName.CODEX: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=False,
        renders_images=True,
        native_budget_enforcement=False,
        requires_prepared_home=True,
        supported_task_fields=TASK_FIELD_NAMES
        - {
            "allowed_tools",
            "disallowed_tools",
            "fork_session",
            "mcp_servers",
            "subagents",
        },
    ),
    RuntimeName.GLM: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=True,
        renders_images=True,
        native_budget_enforcement=True,
        requires_prepared_home=False,
        supported_task_fields=TASK_FIELD_NAMES,
    ),
}


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
