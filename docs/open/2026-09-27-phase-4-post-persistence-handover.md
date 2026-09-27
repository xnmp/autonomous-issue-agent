# Phase 4 handover after canonical persistence

Date: 2026-09-27. Branch `dev`, HEAD `bd4cfe0` (local, unpushed). The active
goal remains the six-phase architecture plan; phases 1–3 are accepted, phase 4
is in progress, phases 5–6 remain. Read `CLAUDE.md`, `AGENTS.md`, the
[phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[conventions](2026-09-27-phase-4-conventions.md),
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[provenance map](2026-09-27-phase-4-execution-provenance.md) first.

## Accepted checkpoints

- Phase 3 review-rejection activation and slice at `a7214bf`, acceptance docs
  at `5abacab`.
- Phase 4 ordered execution projection at `a4eccaf`, pure round finalization
  at `10b31e6`, canonical persistence at `bd4cfe0`.
- Schema v25 is committed. Its migration source digest is immutable: further
  schema changes require v26. The migration backfills validated resolve and
  rejection component indices, but does not activate review execution.
- PR canonical episode writes now use `ReviewFamilyEpisodes` with an explicit
  inactive/active execution component. A strict new snapshot tag requires the
  execution field; exact historical v24 snapshot receipts decode inactive and
  replay idempotently. Resolve wire bytes remain unchanged.
- Active reads join the execution epoch/index, round plan/closure/outcome,
  projected state and exact compaction receipt. Missing parent/index/epoch,
  malformed or contradictory round history, and open compacted prefixes refuse.
- Dispatch claims have stable occurrence IDs and retained created/released/
  reclaimed receipts. Reclamation revalidates the exact occurrence after the
  external process probe. The current normal release API still accepts
  owner/target only; later finalization must match a round's captured occurrence
  to its retained terminal receipt.

The independent adversarial reviewer reproduced a historical replay ordering
defect and two active-loader corruption defects; all were fixed with regressions
and the final review found no remaining blocker in this checkpoint's scope.
Final `make test` at `bd4cfe0`: 3,085 domain, 2,481 adapter, 2,861 app tests,
123 hook checks, 13 governor tests, 10 expected strict xfails, four import
contracts and both Pyrefly passes green. Local log
`/tmp/phase4-persistence-final-full.log` is ephemeral. Working tree was clean
after the commit.

## What is still construction, not accepted runtime authority

The review daemon still reads legacy session, displayed round, unproductive
counter and pin streak. No actual execution-family activation path exists; the
v25 active variant is exercised only in isolated tests. There are no production
writers yet for review round plans, child attachment, launch/return facts,
closure, ordered outcome, compaction receipts or productive reset. The v25
loader proves internal typed consistency, but cannot yet prove external launch,
return, claim abandonment or imported-session receipt provenance. Do not claim
execution cutover or remove the remaining Store APIs until these hooks and
offline activation/composed acceptance exist.

## Next transaction boundary

1. Parent defines the transaction API for one durable preflight. Under the
   existing `BEGIN IMMEDIATE`, verify active execution and current shared
   generation/version, the exact live review claim occurrence, PR head/linkage
   hold facts, and idempotent preflight request identity; allocate sequence
   including free rounds, store initial `ReviewRoundPlan` and advance parent
   CAS once. A missing claim or mismatched occurrence refuses before paid work.
2. Register each immutable planned stage/child before its reservation, allowing
   adaptive validator stages only while planning is open. Admission validates
   the planned child, exact claim occurrence, dual rejection+execution binding
   and generation. Attach `runs.review_round_id` and `review_child_id` in the
   existing run/budget/reservation transaction. Exact reservation replay must
   compare both attachments and dual capture.
3. Mint a guarded-launch registration receipt in
   `SQLiteControlStore.register_process_lease`, tied to the exact run and child.
   Reservation and provider receipt IDs do not prove launch. Preserve explicit
   settled-without-launch synthetic preflight evidence, proven unlaunched
   release, cancellation and dead-lease reconciliation distinctions.
4. Capture typed child return in the accounting settlement transaction. Seal
   planning after parent completion or exact claim abandonment with a late
   admission/submission fence. Bind every proposed submission command to its
   round before a remote write; unresolved remote responses keep the round
   open. Re-derive finalization from retained facts, persist one outer outcome
   and project the ordered suffix atomically. Compaction must mint the exact
   `review-execution-checkpoint` receipt required by the v25 loader.
5. Productive submission settlement must reset execution allowance/pin streak
   with the already durable review baseline and command effect receipt.
   `_progress` cannot own authority. Free copied verdicts get a round and
   settlement receipt but no invented paid child.
6. Before any real activation, handle rejection generation advances when an
   active execution component exists: preserve execution failures/pins/policy,
   clear session for fresh recovery, fence unresolved old rounds, rebind the
   component and mint its checkpoint receipt. Current validated snapshot
   construction rejects an unrebound active component, so this cannot be left
   to a blind `replace(snapshot, revision=...)`.

Then add distinct verdict-release and execution-ceiling recovery, offline
import/shadow/rehearsal/activation, runtime/dashboard cutover and selected file
API retirement. Fix/steward continuation families and phases 5–6 still follow.

## Standing restrictions

No live service restart, target checkout mutation, paid harness run, real
activation or migration, publication/push, or live phase-5 measurement has been
authorized. Full `make test` must pass before each coherent commit; use uv and
Pyrefly. An independent adversarial reviewer is required for the transaction
and runtime cutover. Source of truth remains the six-phase plan and ADR 0015.
