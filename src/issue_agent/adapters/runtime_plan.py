"""Runtime execution contract at the infrastructure boundary.

``RunIntent`` is domain vocabulary: prompt, output contract, reasoning policy,
and budget.  Everything that describes *how a provider process is launched*
lives here instead: tool names, MCP configuration, environment, worktree, and
resume state.  Keeping those settings out of ``domain/`` makes provider changes
an adapter/application concern.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.agent_task import RunIntent, RuntimeName
from ..domain.effort import Effort
from ..domain.outcome import AgentResult
from ..domain.workflow_identity import WorkflowBinding
from ..domain.review_execution import ReviewChildIdentity


@dataclass(frozen=True)
class McpServer:
    name: str
    config: dict
    tools: str = ""


@dataclass(frozen=True)
class SubagentDef:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = ()
    model: str | None = None


@dataclass(frozen=True, init=False)
class RuntimePlan:
    """Provider launch plan produced by application planning.

    The explicit constructor keeps call sites concise while storing the
    provider-neutral portion as a real ``RunIntent``.  ``dataclasses.replace``
    remains supported for wrappers that add a worktree, environment, or resume
    token at the execution edge.
    """

    intent: RunIntent
    runtime: RuntimeName | None
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    cwd: str | None
    resume: str | None
    fork_session: bool
    mcp_servers: tuple[McpServer, ...]
    subagents: tuple[SubagentDef, ...]
    env: dict[str, str]
    # Scratch history is execution context, never authoritative task input.
    context_prefix: str
    context_suffix: str

    def __init__(
        self,
        prompt: str | RunIntent = "",
        schema: dict | None = None,
        append_system_prompt: str = "",
        allowed_tools: tuple[str, ...] = (),
        disallowed_tools: tuple[str, ...] = (),
        model: str | None = None,
        effort: Effort | None = None,
        max_budget_usd: float = 3.0,
        cwd: str | None = None,
        resume: str | None = None,
        fork_session: bool = False,
        mcp_servers: tuple[McpServer, ...] = (),
        subagents: tuple[SubagentDef, ...] = (),
        env: dict[str, str] | None = None,
        *,
        intent: RunIntent | None = None,
        runtime: RuntimeName | None = None,
        context_prefix: str = "",
        context_suffix: str = "",
    ) -> None:
        selected_intent = intent
        if selected_intent is None:
            selected_intent = (
                prompt
                if isinstance(prompt, RunIntent)
                else RunIntent(
                    prompt=prompt,
                    schema=schema,
                    system_instruction=append_system_prompt,
                    model=model,
                    effort=effort,
                    maximum_budget_usd=max_budget_usd,
                )
            )
        object.__setattr__(self, "intent", selected_intent)
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "allowed_tools", allowed_tools)
        object.__setattr__(self, "disallowed_tools", disallowed_tools)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "resume", resume)
        object.__setattr__(self, "fork_session", fork_session)
        object.__setattr__(self, "mcp_servers", mcp_servers)
        object.__setattr__(self, "subagents", subagents)
        object.__setattr__(self, "env", dict(env or {}))
        if not isinstance(context_prefix, str) or not isinstance(context_suffix, str):
            raise ValueError("runtime context must be text")
        object.__setattr__(self, "context_prefix", context_prefix)
        object.__setattr__(self, "context_suffix", context_suffix)

    @property
    def prompt(self) -> str:
        return self.context_prefix + self.intent.prompt + self.context_suffix

    @property
    def schema(self) -> dict | None:
        return self.intent.schema

    @property
    def append_system_prompt(self) -> str:
        return self.intent.system_instruction

    @property
    def model(self) -> str | None:
        return self.intent.model

    @property
    def effort(self) -> Effort | None:
        return self.intent.effort

    @property
    def max_budget_usd(self) -> float:
        return self.intent.maximum_budget_usd

    def all_allowed_tools(self) -> list[str]:
        extra = [server.tools for server in self.mcp_servers if server.tools]
        tools = [*self.allowed_tools, *extra]
        if self.subagents and "Task" not in tools:
            tools.append("Task")
        return tools


def git_identity_env(model: str | None) -> dict[str, str]:
    if not model:
        return {}
    name = f"Harness - {model}"
    return {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name}


@runtime_checkable
class AgentRuntime(Protocol):
    async def run(self, task: RuntimePlan) -> AgentResult: ...


@runtime_checkable
class ContextualRuntime(AgentRuntime, Protocol):
    def set_context(
        self,
        role: str,
        kind: str,
        number: int,
        issue: int | None,
        claim_target: int | None = None,
        *,
        workflow: WorkflowBinding | None = None,
        review_child: ReviewChildIdentity | None = None,
        head: str = "",
    ) -> None: ...


@runtime_checkable
class RunIdentityRuntime(Protocol):
    def set_run_identity(self, run_id: str, leg_id: str) -> None: ...


class RouteDecisionRecorder(Protocol):
    def __call__(
        self, model: str | None, effort: object | None, runtime: str | None = None,
        *, tier: str | None = None,
    ) -> None: ...


@runtime_checkable
class RunTelemetryRuntime(ContextualRuntime, Protocol):
    def leg_kind(self, kind: str) -> AbstractContextManager[None]: ...

    def route_receipt(
        self,
    ) -> AbstractContextManager[
        RouteDecisionRecorder
    ]: ...
