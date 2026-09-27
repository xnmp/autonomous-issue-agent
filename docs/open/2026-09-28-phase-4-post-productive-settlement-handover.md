# Phase 4 handover after productive round settlement

Date: 2026-09-28 Australia/Sydney. Branch `dev`. The six-phase goal remains
active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending. Read
`CLAUDE.md`, `AGENTS.md`, the [progress record](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md), the
[persistence map](2026-09-27-phase-4-persistence-map.md), and the
[previous handover](2026-09-28-phase-4-post-abandoned-outcomes-handover.md).

## Current checkpoint

Checkpoint 26 joins a confirmed paid or copied review submission to its sealed
outer round in the rejection baseline or ceiling-gate transaction. The writer
reconstructs the exact parent seal, child evidence and effect receipt, retains
closure facts and the immutable outcome receipt, and projects the execution
reset before the later `review-submission-settled` marker. A failed outcome
write rolls back the baseline. A confirmed submission with open planning waits for the parent seal
or abandonment; it does not write a terminal marker. The bounded round sweep
repairs a historical state with a recorded baseline but an open outcome.

An opened ceiling commits its gate and productive outcome together; a ceiling
refused by fresh evidence still closes the confirmed productive round without
a gate. Its decision receipt retains the exact PR read and captured policy
inputs, so canonical reload re-derives the refusal and replay preserves it even
after a generation advance. An older-generation confirmed verdict closes as
audit-only productivity, without restoring its baseline or resetting the
current generation. A same-generation escalated verdict whose head moved or
PR became terminal closes without a new baseline under an immutable, fresh PR observation receipt.
Its confirmed productive work still resets the execution allowance. Canonical
reload requires exactly one recorded-baseline, stale, superseded or bound-ceiling
authority for each productive outcome. It verifies the relevant source receipts
and refuses contradictory or expired evidence. A crash after the round outcome
and before the later settled marker replays without establishing a baseline.

Independent Sol review found the pre-change split-state recovery gap, stale
confirmed submissions, a hot-read query risk, same-generation supersession,
expired observations and the receipt timestamp race. Each was addressed;
focused tests cover atomic rollback, paid and copied outcomes, ceiling gates,
open planning, historical repair, stale generation, supersession, the
settled-marker crash gap and observation expiry. Its final pass found no
remaining medium or higher issue. Full `make test`
passed: 3,123 domain, 2,826 adapter and 2,866 app tests, plus hooks,
governor, gate parity, four import contracts and both Pyrefly checks. Ten
accepted strict xfails remained expected. Log:
`/tmp/review-productive-final-gate.log` (ephemeral).

No execution-family activation, live migration, target mutation, service
restart, paid harness run or push occurred.

## Next implementation boundaries

1. Design source-backed checkpoint compaction with an activation/import anchor,
   an archived-source audit contract and bounded canonical reads. The current
   4,096-round suffix limit remains an activation gate.
2. Add source-specific bounded rereads for `REVISION_DIFF` and `CONTAINMENT`
   preparation outcomes after their adapter sources are minted.
3. Wire real round identity through production review dispatch, child
   reservations and verdict registration. Resolve SDK exception transport
   classification and run composed recovery tests.
4. Complete offline migration, shadow, rehearsal and execution-family
   activation. Phases 5–6 follow; live phase-5 windows require explicit user
   authorization.

Standing restrictions in the [original review handover](2026-09-27-review-slice-handover.md)
apply. Use Sol or Luna for subagents. Run the full `make test` gate before each
coherent commit.
