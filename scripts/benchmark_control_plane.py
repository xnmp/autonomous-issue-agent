"""Repeatable single-host capacity probes for the durable control plane."""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from issue_agent.adapters.control_store import (
    ReservationRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.event_log import EventLog
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)
from issue_agent.domain.events import Producer


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def run(iterations: int) -> dict[str, float | int]:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="issue-agent-benchmark-") as raw:
        root = Path(raw)
        store = SQLiteControlStore(str(root / "control.db"))
        store.initialize()
        event_log = EventLog(
            str(root / "events-v2.jsonl"),
            Producer("benchmark", "local/benchmark", "probe", 1, "boot"),
        )
        store.bind_event_outbox(
            event_log.prepare,
            event_log.append_committed,
        )
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
            started = time.perf_counter_ns()
            request = ReservationRequest(
                ReservationId(f"reservation-{index}"),
                RunId(f"run-{index}"),
                f"dispatch-{index}",
                scope,
                PriceQuote(
                    "benchmark",
                    "benchmark",
                    "1",
                    now,
                    Money("USD", 1),
                ),
                Money("USD", iterations + 1),
                "benchmark",
            )
            result = store.reserve(request)
            if not result.admitted:
                raise RuntimeError(result.detail)
            store.release(request.run_id, "benchmark")
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        return {
            "iterations": iterations,
            "admission_release_p50_ms": statistics.median(latencies),
            "admission_release_p95_ms": _percentile(latencies, 0.95),
            "admission_release_max_ms": max(latencies),
            "control_db_bytes": Path(store.path).stat().st_size,
            "event_log_bytes": Path(event_log.path).stat().st_size,
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
