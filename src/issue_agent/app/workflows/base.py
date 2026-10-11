"""The single application path for executing and logging an agent leg."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from contextlib import AbstractContextManager

from ...adapters.runtime_plan import AgentRuntime, RunTelemetryRuntime
from ...domain.leg import Leg
from ...domain.outcome import AgentResult, pre_admission_refusal
from ..planners.run import PlannedRun, compose_runtime_plan


async def run_leg(
    runner: AgentRuntime,
    log: Callable[[str], None],
    leg: Leg,
    task: PlannedRun,
    *,
    scope: AbstractContextManager[object] | None = None,
) -> AgentResult:
    target = leg.number or 0
    label = leg.kind.casefold().replace("_", "-")
    log(f"agent #{target}: {label} — running")
    telemetry_scope = (
        runner.leg_kind(leg.kind)
        if isinstance(runner, RunTelemetryRuntime)
        else contextlib.nullcontext()
    )
    with telemetry_scope, scope or contextlib.nullcontext():
        result = await runner.run(compose_runtime_plan(task))
    status = result.status or ("error" if result.is_error else "ok")
    log(
        f"agent #{target}: {label} — done "
        f"(status={status}{f', ${result.cost_usd:.2f}' if result.cost_usd else ''}"
        # The refusal's summary names the missing authority (#262).
        f"{f', {result.summary[:300]}' if pre_admission_refusal(result) else ''})"
    )
    return result
