# Phase 4 handover after non-striking infrastructure outcome

Date: 2026-09-27. Checkpoint commit `6f02589` on `dev`. The persistent
six-phase objective remains active: phases 1–3 accepted, phase 4 in
progress, phases 5–6 pending. Read `CLAUDE.md`, `AGENTS.md`,
[phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted state

The transaction-local child reader now derives terminal refusal, release,
zero-settled no-launch, normal launched return, shutdown cancellation and
settled launched infrastructure failure from indexed source rows. It leaves
unresolved process/spend/return evidence pending. Pure finalization has a
typed `ReviewInfrastructureDeath` outer disposition for settled launched
`KILLED_EXTERNALLY` or `RUNTIME_UNAVAILABLE` results. The ordered
projection retains launched/spend provenance and leaves the quality
allowance, pin streak, reviewer session and displayed round unchanged.
Ordinary failed siblings and refused paid submissions still charge one
outer strike, behind confirmed productivity, cancellation and pin deferral.

An ordinary `TRANSPORT_DEATH` cannot be put in `ReviewChildFailed` without
positive deterministic classification. The production reader leaves raw
transport death pending. `BUDGET_EXHAUSTED` remains pending too, pending an
explicit operator-visible, non-striking disposition. The live review daemon
does not use these offline APIs. No complete `ReviewRoundFacts` source
builder, closure writer or execution-family cutover exists yet.

Independent Sol review found and verified two fixes: refused paid submission
precedence over infrastructure-only outcome, and strict enum typing for an
infrastructure reason. Its fresh pass found no further material fault.
Final `make test` passed: 3,118 domain, 2,732 adapter, 2,863 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks.
Ten strict xfails remained expected. Log:
`/tmp/phase4-infra-outcome-full.log` (ephemeral). No live service restart,
target mutation, paid harness run, migration, activation or push.

## Next implementation boundaries

1. Resolve `TRANSPORT_DEATH` from the exception/trace source with a durable
   classifier receipt before deterministic saturation; ordinary transport
   death needs its own non-charging infrastructure variant. Preserve actual
   settled money and source run identity. Resolve `BUDGET_EXHAUSTED` with a
   typed non-striking, operator-visible outer disposition; do not silently
   turn it into an ordinary failure or leave a completed round forever open.
2. Prove exact parent/claim abandonment and retain a transactionally
   effective late-admission/submission fence. A live unsealed parent may
   append stages; absent claim or terminal children alone cannot close the
   round. The normal completed planning seal exists as an offline API.
3. Register submission command-to-round relationships before any remote
   write, reconcile uncertain submissions and free-copy refusal followed by
   paid fallback. Build complete `ReviewRoundFacts` from transaction-local
   receipts, then commit closure, outcome and ordered projection atomically.
4. Resolve Codex `_exec_once` multiple supervised attempts under one
   planned child/run before runtime cutover. Complete the remaining phase-4
   migration/recovery/acceptance work, then phases 5–6.

The original [review-slice handover](2026-09-27-review-slice-handover.md)
continues to forbid real activation, migration, service restart, target
mutation, paid harness runs, push/publication and phase-5 live windows
without explicit authorization. Run `make test` before every coherent
commit. Use Sol or Luna for subagents, as the user requested.
