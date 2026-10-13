"""One role's runtimes behind one `AgentRuntime`: each leg runs on the chain
(journal -> accounting -> provider) of the runtime its plan names, so a routed
fix role runs Claude and Codex legs side by side, each reserved, priced,
settled and resumed under its own runtime."""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.agent_task import RuntimeName
from ..domain.outcome import AgentResult
from .runtime_plan import AgentRuntime, ContextualRuntime, RuntimePlan


class RuntimeSwitch:
    def __init__(self, chains: Mapping[RuntimeName, AgentRuntime], default: RuntimeName) -> None:
        if default not in chains:
            raise ValueError("the default runtime needs a chain")
        self.chains = dict(chains)
        self.default = default

    def __getattr__(self, name: str):
        """The default chain's operational surface (preflight, identity)."""
        return getattr(object.__getattribute__(self, "chains")[object.__getattribute__(self, "default")], name)

    def set_context(self, role: str, kind: str, number: int, issue: int | None,
                    claim_target: int | None = None, *, head: str = "", resumed: bool = False,
                    tier: str = "") -> None:
        # Every chain: the leg's runtime is known only once its plan is built.
        for chain in self.chains.values():
            if isinstance(chain, ContextualRuntime):
                chain.set_context(role, kind, number, issue, claim_target=claim_target, head=head, resumed=resumed,
                                  tier=tier)

    async def run(self, task: RuntimePlan) -> AgentResult:
        chain = self.chains.get(task.runtime or self.default)
        if chain is None:
            # Config validation keeps this unreachable; the default chain's
            # accounting refuses the mismatched runtime as a recorded leg.
            chain = self.chains[self.default]
        return await chain.run(task)


__all__ = ["RuntimeSwitch"]
