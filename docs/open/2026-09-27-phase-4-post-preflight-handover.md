# Phase 4 handover after unpaid review preflight

Date: 2026-09-27. Branch `dev`, HEAD `7f8137c` (local, unpushed).
The six-phase architecture goal remains active: phases 1–3 accepted, phase 4
in progress, phases 5–6 pending. Read `CLAUDE.md`, `AGENTS.md`, the
[phase-4 progress](2026-09-27-phase-4-progress.md),
[review execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md),
[execution provenance](2026-09-27-phase-4-execution-provenance.md) and
[post-persistence handover](2026-09-27-phase-4-post-persistence-handover.md).

## Accepted authority

- Checkpoints 1–3 are at `a4eccaf`, `10b31e6`, `bd4cfe0`;
  checkpoint 4 is `7f8137c`. Schema v25 is committed and frozen; use v26 for
  any further schema change.
- `ReviewPreflightSelection` is a typed immutable capture of request identity,
  expected aggregate revision, dual rejection/execution binding, exact parent
  claim occurrence and signed target, complete PR observation, trigger, mode,
  runtime and failure ceiling. `plan_review_preflight` enforces freshness,
  lifecycle holds, allowance and bounded ordered sequence.
- `SQLiteReviewExecutionControl.preflight(selection)` checks exact replay
  first, probes the parent process outside SQL, reads the PR and linked issue
  outside the write transaction, then validates exact claim creation/terminal
  receipts, process identity, remote linkage, SQL issue holds, current
  aggregate revision/binding, unresolved submissions and release projection
  under `BEGIN IMMEDIATE`. It stores an open plan and planning row, increments
  aggregate version and sequence once, and mints a replay receipt. No paid run,
  lease or reservation is created. Exact replay tolerates later append-only
  stage/submission planning while preserving the immutable preflight fields.
- Review dispatch and preflight share transaction-local submission/release
  hold queries in `adapters/review_dispatch_holds.py`; production claim legs
  are `REVIEW` and `RE_REVIEW`. Integration tests cover both through real
  `DispatchPathReservations` in the app test tier.
- Independent review reproduced and verified fixes for production leg spelling,
  missed lifecycle labels, unresolved submission/release holds, replay after
  stage planning, and a retained claim whose parent process had exited.
  Final `make test`: 3,093 domain, 2,521 adapter, 2,863 app, hooks, governor,
  parity, four import contracts, both Pyrefly passes green; ten expected strict
  xfails. Ephemeral log: `/tmp/phase4-preflight-final-full.log`.

## Next authority boundary

1. Add transaction-local stage registration. It must append one validated
   `ReviewStagePlan` with immutable child identities to an open round **before**
   any child reservation. Require exact active dual binding and current shared
   generation, exact live claim occurrence, current aggregate version and
   `ReviewPlanningOpen`. Exact duplicate stage registration is inert; a reused
   stage identity with different children conflicts. Adaptive validator stages
   are allowed only while planning remains open. The `ReviewRoundPlan`
   constructor already checks mode, order, child bounds and identities; use it
   for each append. Preflight replay must remain valid after stage append.
2. Integrate child admission into `SQLiteControlStore._reserve` with that
   planned child and exact claim occurrence. `runs.review_round_id` and
   `runs.review_child_id` exist in v25. The run, budget reservation, recovery
   record and child attachment must commit atomically; reservation replay must
   compare both attachments and dual captured binding. Do not treat reservation
   as launch. Reject stale generation, released/reclaimed parent, sealed
   planning, or absent/mismatched child before paid work.
3. Add guarded launch receipt at `register_process_lease`, returned child
   evidence at accounting settlement, exact abandonment/late-admission fences,
   planning seal, outer finalization, ordered projection and compaction receipt.
   The provenance map names the existing seams and no-launch/dead-lease races.
4. Rebind active execution on rejection generation advances before any real
   activation. Preserve failures/pins/policy, clear session, fence unresolved
   rounds and mint a checkpoint receipt. Blind replacement of the snapshot
   revision currently violates the active component binding invariant.

Later work remains productive submission settlement, execution ceiling
recovery, offline import/shadow/rehearsal/activation, runtime/dashboard
cutover, file API retirement and phases 5–6.

## Restrictions and workflow

No live service restart, target checkout mutation, paid harness run, real
activation or migration, publication/push, or live phase-5 measurement has
been authorized. Full `make test` must pass before each coherent commit; use
uv and Pyrefly. Independent adversarial review is required for nontrivial
transaction and runtime changes. The daemon hot-reloads commits from this
checkout between ticks. Parent owns schema, core structs and hot-path
signatures; tests can be delegated after the contract is fixed.
