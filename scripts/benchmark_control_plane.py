"""Repeatable single-host capacity probes for the durable control plane."""

from __future__ import annotations

import argparse
import subprocess
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    ReservationRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.app.loop import Daemon
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)
from issue_agent.domain.events import EventEnvelope, Producer, decode_event_line
from issue_agent.domain.projection import fold
from issue_agent.domain.world import WorldState


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _request(
    index: int,
    scope: BudgetScope,
    now: datetime,
    *,
    prefix: str = "",
    cap_micros: int,
) -> ReservationRequest:
    return ReservationRequest(
        ReservationId(f"{prefix}reservation-{index}"),
        RunId(f"{prefix}run-{index}"),
        f"{prefix}dispatch-{index}",
        scope,
        PriceQuote(
            "benchmark",
            "benchmark",
            "1",
            now,
            Money("USD", 1),
        ),
        Money("USD", cap_micros),
        "benchmark",
    )


def _store_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
        )
        if candidate.exists()
    )


class _NoOpExecutor:
    def replay_pending(self) -> bool:
        return True

    def execute(self, _action: object) -> None:
        return


def _tick_latencies(iterations: int, reason: str) -> list[float]:
    daemon = Daemon(
        "benchmark",
        lambda: WorldState(),
        lambda _world: [],
        _NoOpExecutor(),  # type: ignore[arg-type]
        tick_gate=lambda: reason,
    )
    samples: list[float] = []
    for _index in range(iterations):
        started = time.perf_counter_ns()
        daemon.tick()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return samples


def _shutdown_latency(root: Path) -> float:
    supervisor = ProcessSupervisor(str(root / "supervisor"), grace_seconds=0.01)
    run_id = RunId("benchmark-active-child")
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
    )
    try:
        supervisor.register(run_id, process.pid)
        started = time.perf_counter_ns()
        supervisor.kill_tree(run_id)
        process.wait(timeout=5)
        return (time.perf_counter_ns() - started) / 1_000_000
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def run(iterations: int) -> dict[str, float | int]:
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="issue-agent-benchmark-") as raw:
        root = Path(raw)
        control_path = root / "control.db"
        store = SQLiteControlStore(str(control_path))
        store.initialize()
        event_log = EventLog(
            str(root / "events-v2.jsonl"),
            Producer("benchmark", "local/benchmark", "probe", 1, "boot"),
        )
        transition_started_ns: int | None = None
        outbox_delays: list[float] = []

        def publish(event: EventEnvelope) -> None:
            event_log.append_committed(event)
            if transition_started_ns is not None:
                outbox_delays.append(
                    (time.perf_counter_ns() - transition_started_ns) / 1_000_000
                )

        store.bind_event_outbox(
            event_log.prepare,
            publish,
        )
        initial_control_bytes = _store_bytes(control_path)
        scope = BudgetScope(
            "local/benchmark",
            "repository",
            "USD",
            now.date().isoformat(),
            now.replace(hour=0, minute=0, second=0, microsecond=0),
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1),
            ZoneInfo("UTC"),
        )
        latencies: list[float] = []
        for index in range(iterations):
            request = _request(
                index,
                scope,
                now,
                cap_micros=iterations + 1,
            )
            started = time.perf_counter_ns()
            transition_started_ns = started
            result = store.reserve(request)
            if not result.admitted:
                raise RuntimeError(result.detail)
            transition_started_ns = time.perf_counter_ns()
            store.release(request.run_id, "benchmark")
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        events: list[EventEnvelope] = []
        projection_started = time.perf_counter_ns()
        for path in event_log.paths():
            with path.open(encoding="utf-8") as stream:
                events.extend(
                    decode_event_line(line)
                    for line in stream
                    if line.strip()
                )
        read_model = fold(events)
        projection_rebuild_ms = (
            time.perf_counter_ns() - projection_started
        ) / 1_000_000

        exhausted_latencies: list[float] = []
        for index in range(iterations):
            request = _request(
                index,
                scope,
                now,
                prefix="exhausted-",
                cap_micros=0,
            )
            started = time.perf_counter_ns()
            result = store.reserve(request)
            exhausted_latencies.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
            if result.disposition is not AccountingDisposition.EXHAUSTED:
                raise RuntimeError(
                    f"expected exhausted admission, got {result.disposition.value}"
                )

        unavailable_parent = root / "not-a-directory"
        unavailable_parent.touch()
        unavailable_store = SQLiteControlStore(
            str(unavailable_parent / "control.db")
        )
        unavailable_latencies: list[float] = []
        for index in range(iterations):
            request = _request(
                index,
                scope,
                now,
                prefix="unavailable-",
                cap_micros=iterations + 1,
            )
            started = time.perf_counter_ns()
            result = unavailable_store.reserve(request)
            unavailable_latencies.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
            if result.disposition is not AccountingDisposition.UNAVAILABLE:
                raise RuntimeError(
                    f"expected unavailable admission, got {result.disposition.value}"
                )

        exhausted_ticks = _tick_latencies(iterations, "BUDGET EXHAUSTED")
        unavailable_ticks = _tick_latencies(
            iterations, "ACCOUNTING UNAVAILABLE — benchmark"
        )
        final_control_bytes = _store_bytes(control_path)
        transitions = iterations * 2
        return {
            "iterations": iterations,
            "admission_release_p50_ms": statistics.median(latencies),
            "admission_release_p95_ms": _percentile(latencies, 0.95),
            "admission_release_max_ms": max(latencies),
            "outbox_publish_p95_ms": _percentile(outbox_delays, 0.95),
            "projection_rebuild_ms": projection_rebuild_ms,
            "projection_event_count": len(read_model.events),
            "exhausted_admission_p95_ms": _percentile(
                exhausted_latencies, 0.95
            ),
            "unavailable_admission_p95_ms": _percentile(
                unavailable_latencies, 0.95
            ),
            "exhausted_tick_p95_ms": _percentile(exhausted_ticks, 0.95),
            "unavailable_tick_p95_ms": _percentile(unavailable_ticks, 0.95),
            "active_child_shutdown_ms": _shutdown_latency(root),
            "control_db_bytes": final_control_bytes,
            "control_db_growth_bytes_per_transition": (
                final_control_bytes - initial_control_bytes
            )
            / transitions,
            "event_log_bytes": Path(event_log.path).stat().st_size,
            "event_log_bytes_per_transition": (
                Path(event_log.path).stat().st_size / transitions
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=500)
    args = parser.parse_args()
    if args.iterations <= 0:
        raise SystemExit("--iterations must be positive")
    for key, value in run(args.iterations).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
