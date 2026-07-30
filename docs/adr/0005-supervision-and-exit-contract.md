# ADR 0005 — Supervision and exit contract

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/domain/supervision.py src/issue_agent/adapters/process_supervisor.py src/issue_agent/app/loop.py src/issue_agent/app/main.py ops/

## Decision

The daemon lifecycle is `STARTING → RUNNING → DRAINING → STOPPED`. SIGTERM
initiates bounded drain/cancellation; process groups and registered descendant
trees are reaped on teardown and at boot. PID receipts include process start
identity to prevent PID-reuse kills.

Stable exit codes distinguish clean stop, transient failure, invalid config,
duplicate instance, incompatible schema, corruption, operator repair, and hot
reload. Deployment locks are scoped by role, repository, and absolute target
checkout. Heartbeat files describe scheduler state; systemd readiness is sent
only after config, lock, store recovery, and adapter construction succeed.

## Consequences

Supervisors restart transient failures only. Shutdown is bounded by
`TimeoutStopSec`; the final kill signal is a last resort, not normal teardown.
Hot reload is commit/content-hash gated and takes effect only between ticks.
