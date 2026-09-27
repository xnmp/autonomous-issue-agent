# Phase 4 handover after planned-child budget refusal

Date: 2026-09-27. Checkpoint commit `17752f0` on `dev`. The six-phase goal remains active:
phases 1–3 are accepted, phase 4 is in progress, and phases 5–6 are pending.
Read `CLAUDE.md`, `AGENTS.md`, [phase-4 progress](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md), and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted checkpoint

Checkpoint 10 is committed as `17752f0`. `AccountingRuntime` requests a
durable refusal only after a planned child's final `EXHAUSTED` result, including
the bounded recovery sweep/retry. The store repeats parent, round, workflow,
hold and all cap checks under the write transaction. A true trip advances the
aggregate revision and writes `ReviewChildAdmissionRefusal` under a child key.
It allocates no run, reservation, charge, lease or claim attachment. Changed
authority/headroom before the receipt yields a nonterminal authority refusal.

The refusal binds the full child, owner, claim occurrence, request fingerprint,
limiting repository/dimension/period, committed/requested/cap values and
receipt time. Exact replay is inert; changed requests conflict. Admission uses
the same child key under the SQLite lock, and its child index is cross-checked
against the run-keyed admission receipt on replay and launch. The receipt ID
is content-addressed to detect ordinary corruption within trusted SQLite;
it is not an independent tamper seal.

The independent Sol reviewer found two replay gaps, both fixed and re-reviewed:
refusal replay needed current request/preflight/limit checks; admission replay
needed to cross-check its new child index. No further material issue was found.
Focused domain/admission/runtime/launch/stop tests and Pyrefly passed. Final
`make test` passed: 3,094 domain, 2,689 adapter and 2,863 app tests; hooks,
governor, parity, four import contracts and both Pyrefly checks. Ten expected
strict xfails remained. Log: `/tmp/phase4-refusal-full.log` (ephemeral).

No real execution activation, target checkout mutation, service restart, paid
harness run, migration or push occurred. A read-only process listing found no
running issue-agent processes before commit. The user service bus could not be
queried from the sandbox; earlier handover found the services inactive and
disabled. Do not infer live authority from offline tests.

## Next transaction boundary

1. Build a receipt-backed child-evidence reader for one retained round. It
   must derive each `ReviewChildEvidence` variant from its positive source:
   child refusal; unlaunched release; verified preflight stop plus synthetic
   charge and raw return; guarded launch plus terminal return/charge;
   cancellation; pin deferral; or a proven parent abandonment for children
   never admitted. Terminal run state or a missing receipt alone is not proof.
   Keep the query bounded by the round and child indices; do not scan owner
   history. Validate exact child, run, parent occurrence, dual binding,
   generation and receipt payload before constructing trusted domain values.
2. Add a transactionally retained planning seal/abandonment boundary. An open
   round cannot be finalized merely because current children are terminal:
   the parent might still append an adaptive validator stage. A dead/missing
   parent requires its exact occurrence and fenced terminal claim receipt.
   Keep OS liveness probes outside SQL under deployment authority, then
   recheck the observed identity and receipt inside the write transaction.
3. Call the pure `finalize_review_round` only from complete closure facts, and
   retain those facts with the selected outcome in one transaction. Require
   submission confirmation/refusal receipts; uncertainty waits. Project the
   canonical v25 execution component and ordered suffix/prefix atomically.
   Use the existing loader's `closure_json`/`outcome_json` validation as an
   invariant, not as a substitute for proving source receipts at minting.
4. Resolve Codex supervised retry identity before a live cutover. A planned
   child/run currently has one guarded launch, while `_exec_once` can retry
   multiple attempts. Exercise the chosen typed attempt scheme offline.

Tests should assert observed money, aggregate revision, closure facts and
outer outcome under receipt loss, contradictory evidence, stale generation,
parent death, concurrent admission/abandonment, mixed multi-child settlement,
and uncertain submission. Use the current tests under
`tests/adapters/test_review_execution_*.py` and
`tests/domain/test_review_rounds.py` as fixtures/contracts.

Run full `make test` before each coherent commit. Medium/high-risk changes
require a fresh independent adversarial reviewer; use Sol or Luna as requested
by the user. The standing restrictions in the original
[review-slice handover](2026-09-27-review-slice-handover.md) still apply, in
particular no live service restart, target mutation, paid run, real activation,
publication or phase-5 live window without explicit authorization.
