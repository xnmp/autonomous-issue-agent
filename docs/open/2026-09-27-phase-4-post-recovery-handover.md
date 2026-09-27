# Phase 4 handover after parent-abandonment recovery wiring

Date: 2026-09-28 Australia/Sydney. Branch `dev`. The persistent six-phase goal
remains active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending.
Read `CLAUDE.md`, `AGENTS.md`, the [phase-4 progress record](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
the [round finalization design](2026-09-27-phase-4-round-finalization-design.md)
and the [previous handover](2026-09-27-phase-4-post-submission-handover.md).

## Accepted work in this continuation

Commit `c5669da` accepted atomic copied/paid submission registration. The
command, evolved round plan, two lookup receipts and aggregate revision
commit together. A copied command must have a covered same-generation patch;
all active execution submission attempts require exact registration.
Independent Sol review confirmed no supported execution-family activation
command exists yet, so the production caller mismatch is a future cutover
gate. Full `make test` passed with 3,121 domain, 2,760 adapter and 2,863 app
tests, plus hooks, governor, parity, import contracts and Pyrefly.

The next checkpoint wires parent-abandonment fencing into the composed review
daemon's recovery callback. The adapter scans a fixed page of at most 64
unfinalized round rows in indexed order; filtering for open planning and
absent parent claims happens only after the page is fetched. It advances the
cursor across sealed-but-unfinalized rows. The existing writer checks the
exact claim occurrence's creation and terminal receipt. Failed proof is
visible in a throttled recovery log and does not create a fence. Adapter and
composed tests cover the resulting durable planning state. Sol review found
an unindexed planning filter in the first draft; the fixed query uses
`review_execution_rounds_open` and `dispatch_claims_occurrence` by
`EXPLAIN QUERY PLAN`. Its fresh pass found no checkpoint blocker.

Final `make test` passed: 3,121 domain, 2,764 adapter and 2,864 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-abandonment-recovery-final-full.log` (ephemeral).

The recovery hook **only fences planning**. Its composed test asserts
`closure_json` and `outcome_json` are still null. An abandoned round remains
an unresolved ordered sequence gap until finalization runs. No live
activation, target mutation, service restart, paid harness run, migration or
push occurred.

## Next implementation boundary

1. Refactor child evidence reading into a transaction-local source reader;
   join the planning seal or abandonment fence, every planned child and every
   registered submission with their exact retained receipts. `ReviewChildNotAdmitted`
   is authorized only by the matching abandonment fence.
2. Call pure `finalize_review_round` on complete facts and commit closure,
   outcome and ordered projection atomically. A confirmed submission's
   productive reset must share the existing `record_review`/`open_ceiling`
   settlement transaction before its later marker. Recovery should retry
   incomplete evidence without inventing an outcome.
3. Resolve transport-death exception/trace classification and Codex's
   multiple supervised attempts; wire exact round context through the
   production review workflow and make its verdict caller use atomic
   registration. No execution-family activation until the composed path is
   accepted.
4. Complete phase-4 offline migration, recovery and composed acceptance, then
   phases 5–6. Live phase-5 windows still need explicit user authorization.

Standing restrictions from the
[original handover](2026-09-27-review-slice-handover.md) apply: no real
activation, live migration, service restart, target mutation, paid run,
push/publication or phase-5 live window without explicit authorization. Run
`make test` before every coherent commit. Use Sol or Luna for subagents.
