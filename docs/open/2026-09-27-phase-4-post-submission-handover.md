# Phase 4 handover after atomic submission registration

Date: 2026-09-27. Branch `dev`; checkpoint 17 is the coherent implementation
in this continuation. The persistent six-phase goal remains active: phases
1–3 accepted, phase 4 in progress, phases 5–6 pending. Read `CLAUDE.md`,
`AGENTS.md`, the [phase-4 progress record](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
the [round finalization design](2026-09-27-phase-4-round-finalization-design.md)
and the [previous handover](2026-09-27-phase-4-post-abandonment-handover.md).

## Accepted work in this continuation

An active execution-family `SUBMIT_REVIEW` now has one atomic registration
path. `SQLiteReviewExecutionControl.register_submission` commits its
command, round plan, two immutable lookup receipts and aggregate revision
together. Enqueue, begin and retry reject a missing or altered registration.
The loader rechecks the command and round indices against the preflight,
claim, plan and command payload. Copy registration requires a same-generation
baseline covering the patch. A paid verdict requires a planned stage. A
refused copy may open an explicit paid fallback; pending, uncertain or
successful commands cannot. Replay remains inert after claim release or
fallback planning. The write transaction rolls back on receipt failure.

Independent Sol review found that the production review verdict path still
calls ordinary enqueue. This is an expected cutover gap, not a supported
active execution deployment: no execution-family activation command exists
yet. The outbox guard remains fail-closed for an execution-active row. Do not
activate execution authority until the production caller carries exact round
context, registers every copied or paid verdict, and composed tests prove the
path. The current registration API is exercised offline by adapter tests.

Final `make test` passed: 3,121 domain, 2,760 adapter and 2,863 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-submission-final-full.log` (ephemeral). No live activation,
target mutation, service restart, paid harness run, migration or push occurred.

## Next implementation boundary

1. Add an indexed production recovery scan for open rounds and call
   `seal_abandoned_planning` only after the exact parent claim occurrence has
   its terminal receipt. This writer is still called only by tests. Preserve
   successor-owner lineage and do not infer abandonment from absent claims or
   terminal children alone.
2. Build transaction-local complete `ReviewRoundFacts` from registered
   children, accounting, launch/return/refusal/release receipts, the planning
   seal or abandonment fence, and exact command-specific submission evidence.
   Commit closure, outcome and ordered projection together. Productive reset
   must share the rejection settlement transaction before its later marker.
3. Resolve transport-death exception/trace classification and Codex's
   multiple supervised attempts. Wire round identity through the real review
   workflow and make the production verdict caller use atomic registration.
   The Sol finding is the explicit activation gate for this step.
4. Finish phase-4 offline migration, recovery and composed acceptance, then
   phases 5–6. In particular, no active execution repository may run the old
   verdict caller.

Standing restrictions from the
[original handover](2026-09-27-review-slice-handover.md) still apply: no real
activation, live migration, service restart, target mutation, paid run,
push/publication or phase-5 live window without explicit authorization. Run
`make test` before every coherent commit. Use Sol or Luna for subagents.
