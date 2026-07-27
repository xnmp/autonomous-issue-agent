"""AgentTask — the SDK-agnostic description of one headless agent invocation.

A pure value object: it names WHAT to run (prompt, schema, tool policy, model,
budget, worktree, resume, MCP) without depending on the SDK. The AgentRunner
adapter translates it into `ClaudeAgentOptions`; the domain/orchestration layer
builds it. Mirrors the flags every `claude -p` call in the bash assembles
(common_claude_args + per-script additions).

The result side is `domain.outcome.AgentResult` (already defined) — runner
normalises the SDK `ResultMessage` into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .effort import Effort
from .outcome import AgentResult


@dataclass(frozen=True)
class McpServer:
    """One external MCP server to attach (e.g. SonarQube for CI-fix). `config`
    is the stdio/http/sse server spec as the SDK expects; `tools` is the allow
    glob to add (mcp__sonarqube__*)."""

    name: str
    config: dict
    tools: str = ""


@dataclass(frozen=True)
class SubagentDef:
    """A subagent the running agent may spawn via the Task tool — the SDK-agnostic
    mirror of the SDK's `AgentDefinition`. Lets an expensive top-level model (e.g.
    an Opus reviewer) delegate cheap, high-volume work (codebase search, read-and-
    summarise) to a smaller model (`model`), so the expensive context only ever
    sees compressed summaries instead of raw file bytes. `tools` is the subagent's
    OWN allow-list (independent of the parent's). The AgentRunner translates this
    to `agents={name: AgentDefinition(...)}` and adds the `Task` tool to the
    parent's allow-list so the delegation is actually invokable."""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = ()
    model: str | None = None


@dataclass(frozen=True)
class AgentTask:
    prompt: str
    # Structured-output JSON schema (FIX_SCHEMA / REVIEW_SCHEMA / TRIAGE_SCHEMA …).
    # None → free-form text result.
    schema: dict | None = None
    # Appended to the claude_code preset (the bash --append-system-prompt).
    append_system_prompt: str = ""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str | None = None
    # How hard this leg should think (harness #16). None = "say nothing", which
    # leaves each runtime on whatever default it already had — the codex runner's
    # CODEX_REASONING_EFFORT, the Claude SDK's own. Set only when the harness has
    # an opinion (an operator knob, or the escalation ladder), so adding this
    # field changed no existing dispatch. Rendered per runtime at the edge:
    # `-c model_reasoning_effort=…` for codex, the SDK's `effort` option for Claude.
    effort: Effort | None = None
    max_budget_usd: float = 3.0
    cwd: str | None = None          # worktree path (we create the worktree ourselves)
    resume: str | None = None       # prior session id
    fork_session: bool = False      # bound transcript growth after N resumes
    mcp_servers: tuple[McpServer, ...] = ()
    # Subagents this task may spawn via the Task tool (SDK `agents=`). When
    # non-empty, the runner injects them AND adds `Task` to the allow-list so the
    # parent can actually delegate (see SubagentDef). Empty → no delegation.
    subagents: tuple[SubagentDef, ...] = ()
    # Extra env to export into the agent process (e.g. TEST_CMD so the governor
    # hook subprocess inherits it, CLAUDE_CODE_SUBAGENT_MODEL).
    env: dict[str, str] = field(default_factory=dict)

    def all_allowed_tools(self) -> list[str]:
        """allowed_tools plus each MCP server's tool glob (the bash appended MCP
        globs to permissions.allow), plus `Task` when subagents are attached (so a
        delegating parent can spawn them). De-duplicated, order-preserving."""
        extra = [m.tools for m in self.mcp_servers if m.tools]
        tools = [*self.allowed_tools, *extra]
        if self.subagents and "Task" not in tools:
            tools.append("Task")
        return tools


def git_identity_env(model: str | None) -> dict[str, str]:
    """Commit-attribution identity for an agent leg: author/committer NAME is
    "Harness - <model>" so target-repo history shows which model wrote each
    commit. Emails are deliberately untouched — the ambient email keeps GitHub
    account linkage intact. No model → empty dict (say nothing), so a task that
    never resolved a model changes no environment."""
    if not model:
        return {}
    name = f"Harness - {model}"
    return {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name}


@runtime_checkable
class AgentRuntime(Protocol):
    """The port an agent runtime must satisfy to run a fixer task: take an
    `AgentTask`, run the headless agent to completion, and return an
    `AgentResult` (decision read from the envelope, never the exit code).

    This is the seam that lets the fixer leg swap implementations without the
    orchestration layer changing: `adapters.agent_runner.AgentRunner` (Claude
    Agent SDK) and `adapters.codex_runner.CodexRunner` (OpenAI Codex CLI, e.g.
    GPT-5.5) both conform. It lives in the domain because the vocabulary it
    speaks — AgentTask / AgentResult — already does; the IMPLEMENTATIONS stay in
    adapters (this is a hexagonal port: the core declares the interface, the
    infrastructure provides it). `app/dispatch` + `app/roles` type against this,
    not the concrete classes."""

    async def run(self, task: AgentTask) -> AgentResult: ...
