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

## Checkpoint 27 — pure generation boundary

The [generation-boundary finding](2026-09-28-phase-4-generation-boundary.md)
records a reproduced public-control failure: rejection rearm advances the
workflow generation but retains the active execution binding. The same
construction applies to rearm continuation and ceiling release. Rebinding
alone would lose already counted outcomes on replay or admit late old outcomes.
The domain now has a typed generation boundary, carry operation and ordered
replay that separates a generation advance from a contiguous checkpoint. The
closed codec preserves its exact revisions and mutation. Sol review found no
remaining medium or high defect in this model scope. Full `make test` passed:
3,128 domain, 2,827 adapter and 2,866 app tests, plus hooks, governor,
gate parity, four import contracts and both Pyrefly checks. Ten accepted
strict xfails remained expected. Log:
`/tmp/phase4-generation-boundary-final-gate.log` (ephemeral).

The adapter does not yet mint or verify boundary receipts or wire the active
aggregate's three generation-advancing transitions. The public-control
regression remains uncommitted until that complete change is implemented.
No live execution-family activation, migration, service restart, target
mutation, paid run or push occurred.

## Checkpoint 28 — generation boundary persistence

The SQLite adapter now wires all three advancing transitions to the active
execution component. It captures every finalized suffix outcome at acceptance,
stores a boundary receipt tied to the accepted workflow transition, and
verifies both receipts and the ordered source projection on canonical reload.
Late old-generation outcomes retain their audit and charge evidence without
changing the new allowance. Public-control regressions cover the three
advances, old open rounds, late paid failures, state-neutral outcomes, exact
replay and rollback. The codec explicitly distinguishes the old active
component tag from the new boundary-bearing tag.

Independent Sol review found no separate high correctness defect. It found
that repeated advances produce quadratic accepted snapshot history and
canonical read work, and the 4,096-boundary cap eventually blocks recovery
even with no new rounds. This remains a hard activation gate alongside the
4,096-round suffix limit. The next compaction design needs a verified
activation/import anchor, an archived-source audit contract, and bounded hot
reads. No execution-family activation, migration, service restart, target
mutation, paid run or push occurred.
Full `make test` passed: 3,129 domain, 2,837 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-generation-adapter-gate.log` (ephemeral).

## Checkpoint 29 — compaction frontier model

The [compaction contract](2026-09-28-phase-4-compaction-contract.md) defines
source-backed origins for activation and renewed episodes, a boundary-aware
checkpoint frontier, bounded canonical reads, and cyclic archived-source
audit. The pure planner now finds a contiguous finalized prefix even when a
later boundary covers an open round. It keeps late old-generation outcomes in
the retired source set while excluding their allowance effect, carries the
last retired boundary cursor into later checkpoints, and verifies replay from
the candidate checkpoint. The codec has an exact legacy checkpoint read rule.
Focused tests cover partial and zero-round compaction, late outcomes,
malformed/duplicate/future sources, and delivery-order independence. Sol's
final pure-scope review found no medium or high issue. The adapter still needs
the origin and checkpoint receipts, bounded hot reads, and archived-source
audit; execution remains inactive.
Full `make test` passed: 3,136 domain, 2,838 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-compaction-model-gate.log` (ephemeral).

## Next implementation boundaries

1. Complete execution activation source inventory: predecessor-owner
   discovery, submission receipts, hold provenance, and explicit issue-counter
   mapping. Legacy files and paged SQL run sources are in checkpoints 30–31 of the
   [progress record](2026-09-27-phase-4-progress.md).
2. Implement the [source-backed origin and compaction contract](2026-09-28-phase-4-compaction-contract.md)
   with bounded canonical reads and an archived-source audit. The current
   4,096-round and boundary suffix limits remain activation gates.
3. Add source-specific bounded rereads for `REVISION_DIFF` and `CONTAINMENT`
   preparation outcomes after their adapter sources are minted.
4. Wire real round identity through production review dispatch, child
   reservations and verdict registration. Resolve SDK exception transport
   classification and run composed recovery tests.
5. Complete offline migration, shadow, rehearsal and execution-family
   activation. Phases 5–6 follow; live phase-5 windows require explicit user
   authorization.

Standing restrictions in the [original review handover](2026-09-27-review-slice-handover.md)
apply. Use Sol or Luna for subagents. Run the full `make test` gate before each
coherent commit.
