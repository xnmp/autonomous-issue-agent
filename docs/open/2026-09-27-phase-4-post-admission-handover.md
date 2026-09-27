# Phase 4 handover after paid child admission

Date: 2026-09-27. Branch `dev`, HEAD `12758e7` (local, unpushed).
The six-phase goal remains active: phases 1–3 accepted, phase 4 in progress,
phases 5–6 pending. Read `CLAUDE.md`, `AGENTS.md`, the
[phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md), and
[provenance map](2026-09-27-phase-4-execution-provenance.md).

## Accepted state

- Checkpoint 6, `12758e7`, attaches one typed planned child to a run and
  reservation under an open round, exact preflight/stage receipts, current
  dual binding, live parent claim occurrence and current paid-work allowance.
  It stores the run/child fields, parent claim/run link and immutable
  `review-execution-admission` receipt in the same budget transaction.
- Exact replay compares the round, child, reservation, captured binding and
  receipt against the current canonical revision. Claim release after
  admission does not change an exact replay. Exhaustion and late insertion
  failures roll everything back. Active execution refuses unplanned review;
  retained authority prevents a damaged epoch row from restoring unbound
  legacy admission.
- Independent adversarial review reproduced current failure-ceiling and
  reviewed-head bypasses, a future-version replay receipt and deleted
  authority epochs. All were fixed. Final `make test`: 3,093 domain, 2,577
  adapter and 2,863 app tests, hooks, governor, parity, four import contracts
  and both Pyrefly passes green; ten expected strict xfails. Ephemeral log:
  `/tmp/phase4-admission-verified-full.log`.
- Schema remains frozen at v25. Active execution exists only in isolated
  fixtures. No real activation, daemon cutover or paid harness run occurred.

## Next transaction boundary: guarded child launch

`supervised_exec._register` verifies the launching parent and prepared token,
then calls `SQLiteControlStore.register_process_lease` before provider exec.
That store method already commits lease, run=`running`, `started_at` and
reservation recovery=`running` together. It currently has no review child
launch receipt. `LaunchedReviewChild` must reference a retained, exact SQL
receipt; a reservation, run timestamp, surviving lease or provider receipt
alone cannot prove the registration boundary.

1. Define typed immutable launch registration evidence containing the exact
   admission, child/run, owner, process PID/PGID/start ticks and receipt ID.
   Store it in a run-keyed `review-execution-launch` dedupe namespace without
   changing v25 schema. Validate the receipt against run/admission/lease facts
   on authoritative reads and exact replay.
2. For a new attached-child lease, probe the captured parent PID/start ticks
   outside the write transaction, then recheck exact claim occurrence,
   creation/terminal receipts, current active dual binding, open round/child,
   active reservation and current SQL holds in the same transaction as lease
   and receipt. Join deployment effect authority across the boundary so an
   operator re-arm and launch cannot cross. An existing-lease replay must
   validate its receipt and full dispatch/owner/process identity but remain
   inert after a later parent claim release. Never repair a missing receipt
   from a lease or terminal run.
3. Close alternate running paths for attached review children:
   `register_lease` must refuse a review-attached run; `reserve(lease=)`
   already does. Inspect `start_run` and other direct state mutations so none
   fabricate review launch evidence. A cleared lease or terminal run must not
   permit a second launch receipt.
4. Test atomic accept/replay/conflict/rollback, late release vs registration,
   stale/retired/sealed authority, receipt retention after lease cleanup and
   supervisor wrapper behavior on registration refusal. The receipt proves
   guarded registration and possible provider execution; it does not prove
   provider exec, billing or a reusable session. Preserve a distinct path for
   a settled synthetic runtime failure that returned before any process launch.

After launch, attach typed returned child evidence in the accounting
settlement transaction, then seal outer outcomes from retained facts. Continue
independent adversarial review for these high-risk shared seams.

## Restrictions

No live service restart, target checkout mutation, paid harness run, real
activation or migration, publication/push, or live phase-5 measurement has
been authorized. Run full `make test` before each coherent commit, use uv and
Pyrefly, and keep all construction in isolated fixtures.
