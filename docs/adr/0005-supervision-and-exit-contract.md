# ADR 0005 — Supervision and exit contract

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/domain/supervision.py src/issue_agent/adapters/process_supervisor.py src/issue_agent/adapters/supervised_exec.py src/issue_agent/app/tick.py src/issue_agent/app/main.py ops/

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

The registered process-group leader is also the paid run's authority carrier.
It retains the compact accounting/publication bundle, forks the provider as a
child with those control-plane variables removed, waits for the provider, and
mirrors its exit status. This preserves ancestor-recoverable durable publication
without turning ordinary agent commands or target tests into control-store
clients.

Leg launchers set Linux's inherited `oom_score_adj` to 1000 before forking
and executing the sandbox/runtime: legs are the preferred victims of cgroup
or host OOM kills and earlyoom, preserving the daemon. The fix, review and
dashboard units use `OOMPolicy=continue`; their existing `MemoryMax` remains
the aggregate bound, with all leg processes inside the service. On hosts
without writable `/proc/self/oom_score_adj` (including macOS/launchd and
restricted containers), launch behaviour is unchanged.

At the Codex classification boundary, exit 137 (`128 + SIGKILL`, as reported
by bubblewrap for a killed runtime) is the same external machine death as a
direct SIGKILL, never an agent attempt. Watchdog and budget stops retain their
own classification; harness drain retains cancellation. Other positive exit
statuses, including 143, keep their existing classification. The Claude SDK
already treats pre-result process errors as transport deaths regardless of
exit status and honours a captured result after a trailing process error.

## Consequences

After earlyoom SIGTERMed the daemons on 2026-10-16 and their clean exit 0 left
them down, supervisors restart clean exits and transient failures, while codes
2–6 stay stopped (launchd's wrapper maps permanent errors to success and clean
daemon exits to restart requests).
Operator stops use `systemctl --user stop` or the installer's `launchctl unload`
for installed services, preventing automatic restart; foreground runs use SIGTERM.
Shutdown is bounded by
`TimeoutStopSec`; the final kill signal is a last resort, not normal teardown.
Hot reload is commit/content-hash gated and takes effect only between ticks.
