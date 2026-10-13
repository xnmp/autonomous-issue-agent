"""A routed role's legs each run on the chain of the runtime their plan names."""

import asyncio

from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.adapters.runtime_switch import RuntimeSwitch
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.outcome import AgentResult


class Chain:
    def __init__(self, name):
        self.name, self.ran, self.contexts = name, [], []

    def set_context(self, role, kind, number, issue, claim_target=None, *, head="", resumed=False, tier=""):
        self.contexts.append((kind, number, tier))

    async def run(self, task):
        self.ran.append(task.model)
        return AgentResult(False, summary=self.name)


def test_each_leg_runs_on_its_planned_runtime_and_unplanned_ones_on_the_default():
    claude, codex = Chain("claude"), Chain("codex")
    switch = RuntimeSwitch({RuntimeName.CLAUDE: claude, RuntimeName.CODEX: codex}, RuntimeName.CLAUDE)
    switch.set_context("fix", "RESOLVE", 3, 3, tier="high")
    results = [asyncio.run(switch.run(RuntimePlan(model=m, runtime=r))) for m, r in (
        ("gpt-6-luna", RuntimeName.CODEX), ("claude-opus-5-5", RuntimeName.CLAUDE), ("x", None))]
    assert [r.summary for r in results] == ["codex", "claude", "claude"]
    assert codex.ran == ["gpt-6-luna"] and claude.ran == ["claude-opus-5-5", "x"]
    # Every chain holds the leg's context: which one runs is known only from the plan.
    assert claude.contexts == codex.contexts == [("RESOLVE", 3, "high")]
