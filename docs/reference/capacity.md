# Single-host capacity boundary

The harness is intentionally a single-host system. SQLite owns low-volume
operational state; rotated schema-v2 JSONL owns telemetry. There is no broker or
distributed lock.

Each event stream retains at most eight rotated 32 MiB files plus its current
file. Every rotation writes the complete open-level projection into the new
current file and re-emits each subject's latest world observation. Startup
restores both before accepting events. Projection rebuild is therefore bounded
without losing active level state or stable GitHub facts.

Re-run the local control-plane probe with:

```bash
uv run python scripts/benchmark_control_plane.py --iterations 500
```

The probe reports p50/p95/max latency for a complete reserve→outbox→release
cycle, committed-outbox publication delay, projection rebuild time, exhausted
and unavailable admission/tick latency, active-child shutdown time, and
database/event-log growth per transition. The full suite separately measures
two-process final-cap races, hundreds of concurrent accounting transitions,
outbox retry after sink failure, shutdown finalization, and orphan recovery.

Record probe output from deployment-class storage before raising throughput.
Treat the numbers as a local capacity observation, not a portable promise:
filesystem sync behavior dominates SQLite and append-only event latency.

Re-evaluate SQLite or the single-host design when any of these become true:

- admission p95 is persistently above 50 ms on deployment storage;
- control-store lock contention delays the scheduler by more than one tick;
- more than one host must admit paid work for the same repository/budget scope;
- event projection rebuild exceeds the dashboard refresh interval after
  retention/rotation;
- backup/restore time exceeds the operator’s recovery objective.

Until then, a networked database, queue, or message broker would add failure
semantics without improving the current authority model.
