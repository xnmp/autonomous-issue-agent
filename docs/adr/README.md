# Architecture decision records

ADRs record accepted ownership, authority, and failure-posture decisions. The
current component-level design remains in
[the architecture reference](../reference/architecture.md); ADRs explain why
its load-bearing boundaries exist.

| ADR | Decision |
|---|---|
| [0001](0001-trust-boundary-and-effects.md) | Untrusted claims and privileged durable effects |
| [0002](0002-money-and-accounting.md) | Money, reservation, and settlement semantics |
| [0003](0003-control-store.md) | Embedded SQLite operational authority |
| [0004](0004-event-envelope-and-retention.md) | Telemetry envelope, retention, and projection boundary |
| [0005](0005-supervision-and-exit-contract.md) | Process supervision, authority carrier, and exit contract |
| [0006](0006-recovery-trigger-policy.md) | Explicit recovery trigger-to-arm policy |
| [0007](0007-configuration-snapshot-and-reload.md) | Immutable configuration snapshot and coherent reload |
| [0008](0008-control-authority-scope-and-deployment-fencing.md) | Machine-wide control scope, deployment ownership, and repoint fencing |
| [0009](0009-workflow-continuation-authority.md) | Split authority for sessions, branches, replies, and implementation state |
| [0010](0010-reconciliation-loop-and-observation.md) | Poll reconciliation, wake hints, probes, and network fail-soft behavior |
| [0011](0011-trusted-execution-and-gate-evidence.md) | Trusted execution receipts, gate parity, and child authority scrubbing |

New ADRs must name governed source files and be referenced from at least one of
those files. Superseded decisions remain in history with an updated status and
a link to their replacement; accepted records are not silently rewritten into
an incompatible decision.
