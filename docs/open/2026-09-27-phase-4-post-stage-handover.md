# Phase 4 handover after planned child stages

Date: 2026-09-27. Branch `dev`, HEAD `ebf0d00` (local, unpushed).
The six-phase goal remains active: phases 1–3 accepted, phase 4 in progress,
phases 5–6 pending. Read `CLAUDE.md`, `AGENTS.md`, the
[phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md),
[provenance map](2026-09-27-phase-4-execution-provenance.md), and the
[post-preflight handover](2026-09-27-phase-4-post-preflight-handover.md).

## Accepted state

- Checkpoint 4 unpaid preflight at `7f8137c` and checkpoint 5 immutable stage
  registration at `ebf0d00`. Schema remains v25; its committed migration digest
  is frozen. Active execution exists only in isolated fixtures. No real
  execution activation or runtime cutover has occurred.
- `SQLiteReviewExecutionControl.register_stage(round_id, expected, stage)`
  accepts a typed `ReviewStagePlan` only for an open, unclosed round whose
  preflight receipt, claim occurrence, live parent PID/start ticks, active dual
  binding and shared aggregate version agree. Append validation uses
  `ReviewRoundPlan`; exact replay is inert. Plan, version and immutable
  `review-round-stage` receipt commit together, without paid work.
- `workflow_rows.require_review_stage_receipts` verifies exact two-way
  correspondence between the canonical bounded stage tuple and receipts in
  both the active loader and stage writer. Receipt keys use a fixed hexadecimal
  round hash prefix with an indexed `[prefix, next-prefix)` range, limited to
  33 rows. Missing, changed and orphan receipts refuse. Compacted historical
  prefixes are governed by their checkpoint receipt; active suffix rounds are
  checked on every load.
- Independent review reproduced a missing preceding-stage receipt, an orphan
  receipt after plan erasure, and a repository-wide stage scan; all were fixed.
  Final `make test`: 3,093 domain, 2,547 adapter, 2,863 app tests, hooks,
  governor, gate parity, four import contracts and both Pyrefly passes green;
  ten expected strict xfails. Ephemeral log:
  `/tmp/phase4-stage-final-full.log`.

## Next transaction boundary: paid child admission

`ReservationRequest` in `adapters/control_store.py` currently has only the
review-rejection `workflow` binding. `_validate_workflow_reservation` accepts
bound review work after checking its PR/linked issue but does not join an
execution round. `_reserve` inserts a `runs` row without the v25
`review_round_id`/`review_child_id` fields, then budget reservation and
`spawn_pending` recovery. `_reservation_request_matches` compares review
rejection binding only. These are the shared seams the parent must change.

1. Introduce an explicit typed planned-child capture in reservation requests,
   with runtime checks that it belongs to the captured rejection workflow and
   review leg. Preserve existing inactive-execution review behavior until
   offline activation/cutover. An active execution component must refuse paid
   review without a planned child.
2. Probe parent claim process identity outside SQLite, then revalidate exact
   occurrence, owner, creation/terminal receipts and child membership in the
   current open plan under `_reserve`'s existing `BEGIN IMMEDIATE`. Join the
   active dual binding and current generation; no old child may enter after a
   re-arm, claim replacement or planning seal. The active loader now verifies
   stage receipts. Keep remote reads outside SQL.
3. In the same run/budget transaction, populate both `runs` attachment fields
   and mint exact `review-execution-admission` run receipt. Its replay check
   must compare dual capture, round, child and immutable attachments; a reused
   run ID with changed child conflicts. Accounting exhaustion must leave no
   attachment or admission receipt. Use the existing admission barrier.
4. Admission is not launch. The later guarded launch receipt belongs at
   `register_process_lease`; returned evidence belongs in accounting
   settlement. Preserve no-launch, cancellation and dead-lease distinctions.

The app must eventually pass planned child identity through its execution
context. No real paid harness run is authorized for this offline construction.

## Restrictions

No live service restart, target checkout mutation, paid harness run, real
activation or migration, publication/push, or live phase-5 measurement has
been authorized. Run full `make test` before each coherent commit and use uv
and Pyrefly. Independent adversarial review is required for the reservation
transaction and subsequent runtime cutover. Parent owns shared schema,
request signatures and hot paths; agents can add behavioral tests after those
contracts are fixed.
