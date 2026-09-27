# Phase 4 — continuation implementation record

Phase 3 is accepted at `a7214bf`; ADR/plan acceptance is recorded in `5abacab`.
Phase 4 follows the [review execution design](2026-09-27-phase-4-review-execution-design.md),
[conventions](2026-09-27-phase-4-conventions.md) and
[persistence map](2026-09-27-phase-4-persistence-map.md).
The [post-stage handover](2026-09-27-phase-4-post-stage-handover.md)
records the next transaction boundary and standing restrictions.
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

## Checkpoint 3 — canonical persistence (accepted)

The checkpoint adds an explicit inactive/active execution component to the
review snapshot, a tagged PR episode envelope, schema v25 component/round/run
indices and durable dispatch-claim occurrence identities. Existing resolve
episode encoding remains unchanged. A frozen v24 observation fixture protects
historical review receipt replay through the new snapshot tag.

The active PR loader joins the canonical envelope, execution activation and
component index. It validates ordered round suffix outcomes against retained
closure facts, refuses missing parents and unresolved compacted prefixes, and
requires an exact retained compaction receipt for a noninitial checkpoint.
Dispatch claim creation, release and reclaim retain occurrence-scoped receipts;
the external liveness probe is revalidated under the write transaction.

Independent review reproduced two corruption paths: an absent parent with
retained component/round rows, and a checkpoint moved over an open round.
Both now refuse, with targeted regressions. The reviewer found no remaining
blocker in the current migration/loader/claim-lifecycle scope. Final `make test`
passed with 3,085 domain, 2,481 adapter and 2,861 app tests, plus hooks,
governor, configuration/environment/parity, four import contracts and both
Pyrefly passes. Ten pre-existing strict-xfail residuals remained expected.
Local log: `/tmp/phase4-persistence-final-full.log` (ephemeral). Runtime round admission,
settlement and real execution activation are later checkpoints; no execution
authority has been activated.

## Checkpoint 4 — unpaid round preflight (accepted)

`domain/review_preflight.py` selects one bounded round from the active dual
binding, complete fresh PR observation, captured failure ceiling, session
capability and current review holds. `adapters/review_execution_control.py`
reads the PR and linked issue outside SQLite's write lock, proves the exact
live parent claim occurrence and creation receipt, then rechecks current
binding, aggregate version, linked issue SQL holds, unresolved submissions and
release projection in one transaction. That transaction allocates one round
sequence, stores its initial plan and planning state, advances the aggregate
version, and records an exact replay receipt. No run, lease or budget
reservation is created. Exact replay remains inert after later child-stage
planning or claim release.

The existing dispatch and the new preflight use one transaction-local source
for unresolved-submission and release holds. Independent review reproduced
uppercase production claim-leg mismatch, missing lifecycle holds, a dead
parent process accepted from a retained claim row, and replay conflict after
stage planning. Each has a behavioral regression; re-review found no remaining
blocker in this unpaid boundary. Final `make test` passed with 3,093 domain,
2,521 adapter and 2,863 app tests, plus hooks, governor, gate parity,
four import contracts and both Pyrefly passes. Ten pre-existing strict xfails
remained expected. Local log: `/tmp/phase4-preflight-final-full.log`
(ephemeral). The first two full runs caught missing architecture-manifest and
test-tier layout entries; the corrected run was green.

This checkpoint does not register child stages, admit paid work, prove launch
or return, seal rounds, activate the execution family or cut over the daemon.

## Checkpoint 5 — immutable planned stages (accepted)

`SQLiteReviewExecutionControl.register_stage` appends one validated stage and
its planned children to an open round under the exact preflight receipt, live
parent claim occurrence, active dual binding and current aggregate revision.
It advances the shared version once, with no paid run or reservation. Exact
replay is inert; changed children, sealed planning and stale authority refuse.
The append and an immutable `review-round-stage` receipt commit together.

The active loader and stage writer both require an exact two-way match between
the bounded stage list and its receipt set. Receipt keys use an indexed
round-scoped range, so one active round does not scan every repository stage.
Independent review reproduced missing prior receipts and orphan receipts that
otherwise permitted an append over altered history; both now refuse. The
reviewer found no remaining blocker in this stage-registration scope. Final
`make test` passed with 3,093 domain, 2,547 adapter and 2,863 app tests, plus
hooks, governor, gate parity, four import contracts and both Pyrefly passes.
Ten pre-existing strict xfails remained expected. Local log:
`/tmp/phase4-stage-final-full.log` (ephemeral).

This checkpoint does not admit or launch any child. The next boundary must
join a planned child and exact parent occurrence to run/budget reservation in
one transaction; reservation alone cannot prove launch.

## Required subsequent work

- Planned immutable child stages and sealed outer outcomes; evidence-based
  finalization after parent/claim abandonment. Distinguish guarded launch,
  released-before-launch and settled-without-launch evidence; preserve actual
  accounting and bound unpaid preparation faults through evidence holds.
- Indexed round/admission/return receipts and transactional projection over
  the canonical v25 component; guarded-launch and exact claim occurrence proof.
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
