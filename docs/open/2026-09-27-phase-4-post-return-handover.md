# Phase 4 handover after raw review return capture

Date: 2026-09-27. Branch `dev`. The six-phase goal remains active: phases
1–3 accepted, phase 4 in progress, phases 5–6 pending. Read `CLAUDE.md`,
`AGENTS.md`, [progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Current checkpoint

Checkpoint 8 captures bounded unclassified `ReturnedReviewChild` output for
an explicitly attached planned child. Accounting settlement stores the actual
charge and raw return in one SQLite transaction. Uncertain spend retains the
raw return with the active reservation; reconciler-first settlement still
accepts that output against the stored charge. Accounting calculation errors
retain the raw output and reservation. Journal context forwards the child,
and paid admission checks the preflight runtime before execution.

The return reader and writer prove exact run/admission, stage/preflight
runtime, and guarded launch receipt. A run's persisted start time and any
live lease must agree with the full launch receipt even after a lease is
cleaned up. Independent adversarial review reproduced and verified fixes for
reconciler-first output loss, self-validating corrupt runtime, runtime drift at
admission, missing launch after cleanup, accounting calculation failure, and
altered launch identity accepted at return. Focused review admission, launch,
return and runtime suites plus Pyrefly passed. Final `make test` passed with
3,093 domain, 2,641 adapter and 2,863 app tests, hooks, governor, gate
parity, four import contracts, two Pyrefly passes and ten expected strict
xfails. Local log: `/tmp/phase4-return-full.log` (ephemeral).

No raw return is classified as success, strike, reusable session, cancellation
or settled-without-launch. Current no-launch settlement in isolated fixtures
does not yet prove a preflight stop. There has been no real migration,
activation, daemon cutover, paid harness run or live phase-5 measurement.

## Next source proofs and integration

1. Mint a typed, immutable no-launch preflight-stop receipt at the actual
   harness branch that returns before process spawn. In the same terminal
   settlement transaction, require reserved/spawn-pending state, no lease,
   start time, launch or provider receipt, then fence any future registration.
   Absence of a launch receipt alone is not proof. Keep raw output and money
   capture independent of later classification.
2. Persist a pre-reservation refusal receipt without creating a paid run.
   Finalization must verify it for `ReviewChildRefused`.
3. Join actual charge, launch, return, cancellation, release and refusal
   receipts to classify child outcomes and seal an outer round. A returned
   session is reusable only after eligibility and runtime capability proofs.
   Account for provider settlement already won by recovery; do not infer zero
   spend from missing usage or a terminal run.
4. Resolve CodexRunner transport retries before cutover: one admitted child/run
   currently permits one guarded launch, while `_exec_once` may spawn several
   times under the same accounting run ID. Choose distinct admitted runs or
   typed launch attempts, and test real supervised retries offline.

Full `make test` must pass before any coherent commit. No live service restart,
target checkout mutation, paid harness run, real activation or migration,
publication/push, or live phase-5 window is authorized.
