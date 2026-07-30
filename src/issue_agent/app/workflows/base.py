"""The single application path for executing and logging an agent leg."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from contextlib import AbstractContextManager

from ...adapters.runtime_plan import AgentRuntime, RuntimePlan
from ...domain.outcome import AgentResult


async def run_leg(
    runner: AgentRuntime,
    log: Callable[[str], None],
    label: str,
    target: int,
    task: RuntimePlan,
    *,
    scope: AbstractContextManager[object] | None = None,
) -> AgentResult:
    log(f"agent #{target}: {label} — running")
    with scope or contextlib.nullcontext():
        result = await runner.run(task)
    status = result.status or ("error" if result.is_error else "ok")
    log(
        f"agent #{target}: {label} — done "
        f"(status={status}{f', ${result.cost_usd:.2f}' if result.cost_usd else ''})"
    )
    return result
