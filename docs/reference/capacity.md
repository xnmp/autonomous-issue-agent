# Single-host capacity boundary

The harness is intentionally a single-host system. SQLite owns low-volume
operational state, including the legs/money tables metrics derive from; each
role appends a plain JSONL log. There is no broker or distributed lock. (The
schema-v2 event stream, its retention/projection rebuild, and the
`benchmark_control_plane.py` probe were deleted in slice 8a.)

The full suite measures two-process final-cap races, hundreds of concurrent
accounting transitions, shutdown finalization, and orphan recovery. Filesystem
sync behavior dominates SQLite latency.

Re-evaluate SQLite or the single-host design when any of these become true:

- admission p95 is persistently above 50 ms on deployment storage;
- control-store lock contention delays the scheduler by more than one tick;
- more than one host must admit paid work for the same repository/budget scope;
- backup/restore time exceeds the operator’s recovery objective.

Until then, a networked database, queue, or message broker would add failure
semantics without improving the current authority model.
