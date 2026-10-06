"""Select a governed provider stack before its accounting admission."""

from collections.abc import Mapping
from contextvars import ContextVar
from types import MappingProxyType

from .runtime_plan import AgentRuntime, ContextualRuntime, RunIdentityRuntime, RuntimePlan
from ..domain.agent_task import RuntimeName
from ..domain.workflow_identity import WorkflowBinding
from ..domain.review_execution import ReviewChildIdentity
from ..domain.outcome import AgentResult


class SelectingRuntime:
    def __init__(self, runtimes: Mapping[RuntimeName, AgentRuntime], default: RuntimeName):
        if default not in runtimes:
            raise ValueError("default runtime must be available")
        self.runtimes = MappingProxyType(dict(runtimes))
        self.default = default
        self._workflow = ContextVar[WorkflowBinding | None](f"selected_workflow_{id(self)}", default=None)

    @property
    def workflow_binding(self) -> WorkflowBinding | None:
        return self._workflow.get()

    def set_context(self, role: str, kind: str, number: int, issue: int | None,
                    claim_target: int | None = None, *, workflow: WorkflowBinding | None = None,
                    review_child: ReviewChildIdentity | None = None) -> None:
        self._workflow.set(workflow)
        for runtime in self.runtimes.values():
            if isinstance(runtime, ContextualRuntime):
                runtime.set_context(role, kind, number, issue, claim_target,
                                    workflow=workflow, review_child=review_child)

    def set_run_identity(self, run_id: str, leg_id: str) -> None:
        for runtime in self.runtimes.values():
            if isinstance(runtime, RunIdentityRuntime):
                runtime.set_run_identity(run_id, leg_id)

    async def run(self, task: RuntimePlan) -> AgentResult:
        provider = task.runtime or self.default
        try:
            runtime = self.runtimes[provider]
        except KeyError as exc:
            raise ValueError(f"runtime {provider.value} is unavailable") from exc
        return await runtime.run(task)
