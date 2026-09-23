"""Provider-neutral application plans and the runtime composition mapping."""

from __future__ import annotations

from dataclasses import dataclass

from ...adapters.runtime_plan import McpServer, RuntimePlan, SubagentDef
from ...domain.agent_task import RunIntent, RuntimeName
from ...domain.effort import Effort
from ...domain.resolve_return import ResolveInputs
from ...domain.resolve_split import AdjudicationInput, ReauthorInput, SplitContext, SplitImplement, SplitInput


@dataclass(frozen=True, init=False)
class PlannedRun:
    """Application-owned plan; provider launch details are mapped at execution."""

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
    resolve_inputs: ResolveInputs | None
    split_input: SplitInput | None

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
        resolve_inputs: ResolveInputs | None = None,
        split_input: SplitInput | None = None,
    ) -> None:
        selected_intent = intent or (
            prompt
            if isinstance(prompt, RunIntent)
            else RunIntent(
                prompt,
                schema,
                append_system_prompt,
                model,
                effort,
                max_budget_usd,
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
        if resolve_inputs is not None and not isinstance(resolve_inputs, ResolveInputs):
            raise ValueError("resolve completion inputs must be validated")
        object.__setattr__(self, "resolve_inputs", resolve_inputs)
        if split_input is not None and (resolve_inputs is None or not isinstance(split_input, (SplitContext, SplitImplement, AdjudicationInput, ReauthorInput))):
            raise ValueError("split launch requires validated stage and completion inputs")
        object.__setattr__(self, "split_input", split_input)

    @property
    def prompt(self) -> str:
        return self.intent.prompt

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


def compose_runtime_plan(plan: PlannedRun) -> RuntimePlan:
    """The sole application → provider launch-plan mapping."""

    return RuntimePlan(
        intent=plan.intent,
        runtime=plan.runtime,
        allowed_tools=plan.allowed_tools,
        disallowed_tools=plan.disallowed_tools,
        cwd=plan.cwd,
        resume=plan.resume,
        fork_session=plan.fork_session,
        mcp_servers=plan.mcp_servers,
        subagents=plan.subagents,
        env=plan.env,
        resolve_inputs=plan.resolve_inputs,
        split_input=plan.split_input,
    )
