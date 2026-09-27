# Phase 4 — continuation implementation record

Phase 3 is accepted at `a7214bf`; ADR/plan acceptance is recorded in `5abacab`.
Phase 4 follows the [review execution design](2026-09-27-phase-4-review-execution-design.md),
[conventions](2026-09-27-phase-4-conventions.md) and
[persistence map](2026-09-27-phase-4-persistence-map.md).
Next-checkpoint preparation is recorded in the
[outer-round finalization design](2026-09-27-phase-4-round-finalization-design.md)
and [execution provenance map](2026-09-27-phase-4-execution-provenance.md).
No real activation, target change, service restart or paid run is authorized.

## Checkpoint 1 — pure execution projection (accepted)

`domain/review_execution.py` defines validated immutable execution bindings,
round/child/launch identities, runtime-tagged session provenance, explicit
session/display updates, finalized outer outcomes and ordered projection.
Projection is independent of delivery order and compacts only a contiguous
finalized prefix. Free copied verdicts reset failures/pin streak while preserving
session/display state. Deterministic death saturates the captured ceiling;
multiple child failures produce one outer debit. Old-generation outcomes are
audit only. Conflicting duplicate outcomes are refused.

Independent review reproduced policy drift between full replay and replay from
a compacted prefix. The checkpoint now retains its episode's pin policy;
projection has no policy override. Independent re-review found no remaining
blocker in this checkpoint's scope.

Verification: 88 new behavioural tests include all 120 delivery permutations
of an ordered history with incremental compaction, explicit expected allowance
and continuation outcomes, conflicting replay, gap retention, policy capture,
stale generation and malformed/bounded inputs. Final `make test` passed with
2,957 domain, 2,401 adapter and 2,861 app tests, plus hooks, governor,
configuration/environment/parity, all four import contracts and both Pyrefly
passes. Ten pre-existing strict-xfail residuals remained expected. Local log:
`/tmp/phase4-execution-domain-final-full.log` (ephemeral). The first full run
caught a missing architecture-manifest entry; the corrected rerun was green.

This checkpoint does not activate execution authority or alter runtime
dispatch. Schema remains v24. It does not yet classify open rounds, uncertain
submissions or returned child evidence. The next boundary must validate actual
receipt provenance before minting these finalized values, including eligible
reviewer returns for session changes; launch alone does not establish eligibility.

## Checkpoint 2 — pure round finalization (accepted)

`domain/review_rounds.py` defines immutable stage and
submission plans, explicit launch/return/refusal/release/no-launch-settlement
facts, planning seals and abandoned-parent fences, and pure finalization.
Independent review reproduced loss of configuration evidence in mixed paid
and unlaunched rounds; the correction retains bounded preparation follow-up
alongside the one outer outcome. Independent re-review accepted the corrected
classifier and its tests. All 180 focused projection/finalization tests pass.
Final `make test` passed with 3,049 domain, 2,401 adapter and 2,861 app tests,
plus hooks, governor, configuration/environment/parity, all four import
contracts and both Pyrefly passes. Ten pre-existing strict-xfail residuals
remained expected. Local log: `/tmp/phase4-review-rounds-full.log` (ephemeral).

This checkpoint remains pure construction: schema v24 and existing runtime
execution authority are unchanged. The next persistence checkpoint must re-derive outcomes from retained
closure facts, never accept a caller-constructed outcome as authority.

## Required subsequent work

- Planned immutable child stages and sealed outer outcomes; evidence-based
  finalization after parent/claim abandonment. Distinguish guarded launch,
  released-before-launch and settled-without-launch evidence; preserve actual
  accounting and bound unpaid preparation faults through evidence holds.
- Canonical inactive/active execution component, explicit family indexing,
  schema v25, indexed round/run receipts and captured-policy persistence.
- Actual admission/return accounting transactions and productive submission
  settlement; real multi-child recovery and free copied-verdict capture.
- Distinct verdict-release and execution-ceiling recovery with one aggregate
  intent, preserving rejection allowance and other holds.
- Offline import/shadow/rehearsal/activation, strict provenance for inherited
  failures and sessions, old-writer fencing and selected Store API retirement.
- Composed acceptance, dashboard authority and independent review before
  execution-family cutover. Other fix/steward continuation families follow.

Phase 5 offline measurement and phase 6 remain pending. Live phase-5 windows
still require explicit user authorization.
