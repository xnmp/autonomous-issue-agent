# Architecture decision records

ADRs record accepted ownership, authority, and failure-posture decisions. The
current component-level design remains in
[the architecture reference](../reference/architecture.md); ADRs explain why
its load-bearing boundaries exist.

| ADR | Decision |
|---|---|
| [0001](0001-trust-boundary-and-effects.md) | Untrusted claims and idempotent GitHub writes (keyed or read-back) |
| [0002](0002-money-and-accounting.md) | Money, reservation, and settlement semantics |
| [0003](0003-control-store.md) | Embedded SQLite operational authority |
| [0004](0004-event-envelope-and-retention.md) | Superseded (slice 8a): telemetry envelope, retention, and projection boundary |
| [0005](0005-supervision-and-exit-contract.md) | Process supervision, authority carrier, and exit contract |
| [0006](0006-recovery-trigger-policy.md) | Explicit recovery trigger-to-arm policy |
| [0007](0007-configuration-snapshot-and-reload.md) | Immutable configuration snapshot and coherent reload |
| [0008](0008-control-authority-scope-and-deployment-fencing.md) | Machine-wide control scope, deployment ownership, and repoint fencing |
| [0009](0009-workflow-continuation-authority.md) | Superseded: split authority for sessions, branches, replies, and implementation state |
| [0010](0010-reconciliation-loop-and-observation.md) | Superseded (slice 5d-1): poll reconciliation, wake hints, probes, and network fail-soft behavior |
| [0011](0011-trusted-execution-and-gate-evidence.md) | Superseded (slice 6a): trusted execution receipts, gate parity, and child authority scrubbing |
| [0012](0012-structural-robustness-invariants.md) | Superseded (slice 5d-1): structural robustness invariants (provenance, bounded loops) |
| [0013](0013-human-gate-ledger.md) | Superseded (slice 5d-1): human gates are a ledger, not labels plus events |
| [0014](0014-reviewer-granted-gate-exemptions.md) | Superseded (#325): reviewer-granted gate exemptions and the exemptable-rule boundary |
| [0015](0015-generation-fenced-gate-rearm.md) | Superseded (slices 5b-5d-1): resolve/rejection gate and continuation authority |
| [0016](0016-phase-4-current-state-authority.md) | Superseded (slice 5c): Phase-4 current SQL authority for review |

New ADRs must name governed source files and be referenced from at least one of
those files. Superseded decisions remain in history with an updated status and
a link to their replacement; accepted records are not silently rewritten into
an incompatible decision.
