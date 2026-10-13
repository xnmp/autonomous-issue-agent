"""The runtime execution contract: what one paid leg asks a provider to do."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..domain.agent_task import RuntimeName
from ..domain.effort import Effort
from ..domain.outcome import AgentResult


@dataclass(frozen=True)
class RuntimePlan:
    """Provider launch plan produced by the task planners. Wrappers add a
    worktree, environment or resume token with `dataclasses.replace`."""

    prompt: str = ""
    schema: dict | None = None
    append_system_prompt: str = ""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str | None = None
    effort: Effort | None = None
    max_budget_usd: float = 3.0
    cwd: str | None = None
    resume: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    runtime: RuntimeName | None = None
    # Scratch history and repository guidance: execution context around the
    # task, never authoritative task input.
    context_prefix: str = ""
    context_suffix: str = ""

    @property
    def rendered_prompt(self) -> str:
        return self.context_prefix + self.prompt + self.context_suffix


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
        head: str = "",
        resumed: bool = False,
        tier: str = "",
    ) -> None: ...


@runtime_checkable
class RunIdentityRuntime(Protocol):
    def set_run_identity(self, run_id: str, leg_id: str) -> None: ...
