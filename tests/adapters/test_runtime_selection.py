"""Provider selection precedes admission and preserves task-local correlation."""
import asyncio
from contextvars import ContextVar
from dataclasses import replace

import pytest

from issue_agent.adapters.runtime_selection import SelectingRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.outcome import AgentResult
from tests.adapters.test_accounting_runtime import Runtime, _wrapper, _publication_context_for_accounting_tests


@pytest.mark.parametrize("provider", [RuntimeName.CLAUDE, RuntimeName.CODEX])
def test_selected_provider_is_accounted_before_launch_and_shared_cap_refuses_peer(tmp_path, provider):
    raw = Runtime(AgentResult(False, status="ok", cost_usd=1.25, session_id="receipt",
                              usage={"input_tokens": 100, "output_tokens": 10}))
    wrapper, store = _wrapper(tmp_path, raw, runtime_name=provider.value, cap=2_000_000)
    other = RuntimeName.CODEX if provider is RuntimeName.CLAUDE else RuntimeName.CLAUDE
    peer = Runtime(AgentResult(False, status="ok"))
    runtime = SelectingRuntime({provider: wrapper, other: replace(wrapper, inner=peer, runtime_name=other.value)}, other)
    runtime.set_context("fix", "RESOLVE", 1, 1, workflow=wrapper.workflow_binding)
    first = asyncio.run(runtime.run(RuntimePlan("work", runtime=provider, model="model", max_budget_usd=2)))
    assert first.status == "ok"
    assert raw.calls == 1
    second = asyncio.run(runtime.run(RuntimePlan("work", runtime=other, model="model", max_budget_usd=2)))
    assert second.is_error
    assert peer.calls == 0
    with store.transaction() as db:
        import json
        rows = db.execute("SELECT dispatch_id FROM runs").fetchall()
        assert json.loads(rows[0][0])["runtime"] == provider.value


def test_accounting_refuses_conflicting_provider_without_admission(tmp_path):
    raw = Runtime(AgentResult(False, status="ok"))
    wrapper, store = _wrapper(tmp_path, raw)
    result = asyncio.run(wrapper.run(RuntimePlan("work", runtime=RuntimeName.CLAUDE, max_budget_usd=2)))
    assert result.is_error
    assert raw.calls == 0
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert store.active_reservation_runs() == ()


def test_concurrent_selection_keeps_context_and_run_identity_separate():
    entered = asyncio.Event()
    count = 0
    seen = []
    class Provider:
        def __init__(self, name):
            self.name = name
            self.context = ContextVar(f"context-{name}")
            self.identity = ContextVar(f"identity-{name}")
        @property
        def workflow_binding(self):
            return None
        def set_context(self, role, kind, number, issue, claim_target=None, *, workflow=None, review_child=None):
            self.context.set((number, issue))
        def set_run_identity(self, run_id, leg_id):
            self.identity.set((run_id, leg_id))
        async def run(self, task):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await entered.wait()
            seen.append((self.name, self.context.get(), self.identity.get(), task.model))
            return AgentResult(False, status="ok")
    runtime = SelectingRuntime({provider: Provider(provider) for provider in (RuntimeName.CLAUDE, RuntimeName.CODEX)}, RuntimeName.CODEX)
    async def dispatch(provider, number):
        runtime.set_context("fix", "RESOLVE", number, number)
        runtime.set_run_identity(f"run-{number}", f"leg-{number}")
        await runtime.run(RuntimePlan("work", runtime=provider, model=f"model-{number}"))
    async def both():
        await asyncio.gather(dispatch(RuntimeName.CLAUDE, 1), dispatch(RuntimeName.CODEX, 2))
    asyncio.run(both())
    assert set(seen) == {(RuntimeName.CLAUDE, (1, 1), ("run-1", "leg-1"), "model-1"),
                         (RuntimeName.CODEX, (2, 2), ("run-2", "leg-2"), "model-2")}
