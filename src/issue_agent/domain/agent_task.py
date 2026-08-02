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
    # Whether the runtime can produce TRUSTED screenshot-read receipts (the
    # PostToolUse(Read) hook running in the harness trust domain). Where it
    # can, the PR gate proves the fixer read each evidence image; where it
    # can't (codex — its hooks run inside the paid child, so a receipt would
    # be self-report), the gate passes committed evidence through and the
    # independent reviewer is the sole content verifier
    # (xnmp/autonomous-issue-agent#153; keifu #142/#156). True for every SDK
    # runtime — the flag tracks WHERE hooks run, not the model behind them.
    mints_visual_receipts: bool = False

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
        requires_external_governor=False,
        supported_task_fields=TASK_FIELD_NAMES,
        mints_visual_receipts=True,
    ),
    RuntimeName.CODEX: RuntimeCapabilities(
        supports_resume=True,
        resume_preserves_sandbox=False,
        renders_images=True,
        native_budget_enforcement=False,
        requires_external_governor=True,
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
        requires_external_governor=False,
        supported_task_fields=TASK_FIELD_NAMES,
        # GLM binds through the same SDK runner as claude (_bind_glm_fix_
        # runtime), so its hooks run harness-side and receipts ARE trusted;
        # defaulting False here told the fixer "the reviewer is your only
        # verifier" while the gate enforced reads (adversarial P1-3).
        mints_visual_receipts=True,
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
