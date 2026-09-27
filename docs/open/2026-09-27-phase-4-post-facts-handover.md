# Phase 4 handover after source-backed round facts

Date: 2026-09-28 Australia/Sydney. Branch `dev`. The six-phase goal remains
active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending. Read
`CLAUDE.md`, `AGENTS.md`, the [progress record](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
the [round finalization design](2026-09-27-phase-4-round-finalization-design.md),
the [persistence map](2026-09-27-phase-4-persistence-map.md) and the
[previous handover](2026-09-27-phase-4-post-recovery-handover.md).

## Accepted work in this continuation

`c5669da` committed atomic review submission registration, with full
`make test` green. `70672ab` committed the composed parent-abandonment
recovery caller; its indexed 64-row sweep fences planning from exact terminal
claim receipts and deliberately leaves closure/outcome pending. Full
`make test` passed at that commit: 3,121 domain, 2,764 adapter and 2,864 app
tests, plus hooks, governor, parity, import contracts and Pyrefly.

The current facts-reader checkpoint extracts the existing child evidence
source reader for reuse inside another SQL transaction. It joins canonical
preflight and evolved plan, verified completed seal or abandonment fence,
every planned child and registered submission, and the command's exact effect
receipt. A planned child with no admission becomes `ReviewChildNotAdmitted`
only after the matching abandonment fence. Pending/uncertain submissions
remain pending. Successful submission evidence requires a named remote review;
both successful and refused evidence bind the recomputed effect receipt
digest and attempt owner. The returned `ReviewRoundFacts` carry a
deterministic closure identity. No closure or outcome is written yet.

Sol review found an indentation error that made both terminal submission
branches unreachable. It was corrected and all focused facts tests pass;
the fresh review found no further material issue. No live activation,
target mutation, service restart, paid harness run, migration or push occurred.

Final `make test` passed for the facts-reader checkpoint: 3,121 domain,
2,774 adapter and 2,864 app tests; hooks, governor, parity, four import
contracts and both Pyrefly checks. Log:
`/tmp/phase4-round-facts-final-full.log` (ephemeral).

## Next implementation boundary

1. Add a transaction-local round finalization writer that re-reads these
   facts and calls pure `finalize_review_round`. Pending planning, children or
   commands must remain open. Persist closure facts, matching outcome and an
   immutable outcome receipt atomically; make canonical reload re-derive both
   the pure outcome and source receipts, not only the stored closure payload.
2. Project finalized suffix outcomes in one transaction with the canonical
   v25 execution component. Preserve unresolved gaps and bounded suffix
   compaction. Productive copied or paid outcomes need rejection baseline /
   ceiling settlement in the same transaction, before its later settlement
   marker; do not use a separate post-write reset.
3. Resolve transport-death exception/trace classification and Codex's
   multiple supervised attempts. Wire exact round context through real review
   dispatch, child reservations and verdict submission. No execution-family
   activation until this composed path and recovery are accepted.
4. Complete offline migration, shadow/rehearsal/activation, phase-4 acceptance,
   then phases 5–6. Live phase-5 windows need explicit user authorization.

Standing restrictions from the
[original handover](2026-09-27-review-slice-handover.md) apply: no real
activation, live migration, service restart, target mutation, paid run,
push/publication or phase-5 live window without explicit authorization. Run
`make test` before every coherent commit. Use Sol or Luna for subagents.
