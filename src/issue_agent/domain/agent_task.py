"""Provider-neutral agent intent and runtime capability vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .effort import Effort


class RuntimeName(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    GLM = "glm"


@dataclass(frozen=True)
class RuntimeCapabilities:
    supports_resume: bool
    resume_preserves_sandbox: bool
    renders_images: bool
    native_budget_enforcement: bool
    requires_external_governor: bool
    supported_task_fields: frozenset[str]

    def supports(self, field_name: str) -> bool:
        return field_name in self.supported_task_fields


_COMMON_TASK_FIELDS = frozenset(
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
    }
)

RUNTIME_CAPABILITIES = {
    RuntimeName.CLAUDE: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=True,
        renders_images=True,
        native_budget_enforcement=True,
        requires_external_governor=False,
        supported_task_fields=_COMMON_TASK_FIELDS
        | {"mcp_servers", "subagents"},
    ),
    RuntimeName.CODEX: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=False,
        renders_images=True,
        native_budget_enforcement=False,
        requires_external_governor=True,
        supported_task_fields=_COMMON_TASK_FIELDS,
    ),
    RuntimeName.GLM: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=True,
        renders_images=True,
        native_budget_enforcement=True,
        requires_external_governor=False,
        supported_task_fields=_COMMON_TASK_FIELDS
        | {"mcp_servers", "subagents"},
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
