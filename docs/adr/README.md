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
| [0005](0005-supervision-and-exit-contract.md) | Process supervision, authority carrier, and exit contract |
| [0006](0006-recovery-trigger-policy.md) | Explicit recovery trigger-to-arm policy |
| [0007](0007-configuration-snapshot-and-reload.md) | Immutable configuration snapshot and coherent reload |
| [0008](0008-control-authority-scope-and-deployment-fencing.md) | Machine-wide control scope, deployment ownership, and repoint fencing |
| [0017](0017-stop-the-line.md) | One base-CI issue per red streak; a required workflow's is readied and fixed first |

The superseded records (0004, 0009–0016) are in
[../archive/adr/](../archive/adr/); the subtractive refactor deleted the
mechanisms they governed. The design they gave way to is
[the minimal harness design](../open/2026-10-12-minimal-harness-design.md).

New ADRs must name governed source files and be referenced from at least one of
those files. A superseded decision moves to `docs/archive/adr/` with an updated
status and a link to its replacement; accepted records are not silently
rewritten into an incompatible decision.
