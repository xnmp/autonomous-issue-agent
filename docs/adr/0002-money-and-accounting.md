# ADR 0002 — Money and accounting semantics

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/domain/pricing.py src/issue_agent/adapters/accounting_runtime.py src/issue_agent/adapters/control_store.py

## Decision

Money is an integer number of currency micros. Token counters are disjoint and
carry source provenance. Charges declare their basis:
`provider_reported`, `configured_rate_card`, or `learned_estimate`; quota is a
separate unit and is never summed with money.

Every paid run reserves its quoted maximum transactionally before execution.
Repository, global, and issue limits are checked in the same SQLite
transaction. Settlement is idempotent by run/provider receipt; a conflicting
repeat is an error. A hard-death reservation stays active until recovery can
settle it or release it as visibly unknown.
Codex persists the run id, rollout discovery coordinates, pre-run baseline,
and locked rates before provider launch. Recovery prices the surviving rollout
delta with `basis=reconciled` before releasing capacity.
The runtime's process supervisor—not the long-lived daemon PID—owns the paid
child. An exception that returns through completed child teardown reconciles
available Codex evidence immediately, otherwise releases the reservation as
`runtime_exception_spend_unknown`; a hard daemon death still leaves the active
reservation for boot recovery.

Armed caps fail closed when accounting or required pricing is unavailable.

## Consequences

- Rate-card identity/version is historical evidence and cannot be rewritten.
- Lowering a cap blocks future admission; it does not alter settled history.
- JSONL spend output is a projection, never admission authority.
