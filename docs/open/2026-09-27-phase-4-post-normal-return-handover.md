# Phase 4 handover after settled normal review returns

Date: 2026-09-27. Checkpoint commit `8ac97be` on `dev`. The persistent
six-phase objective remains active: phases 1–3 accepted, phase 4 in progress,
phases 5–6 pending. Start with `CLAUDE.md`, `AGENTS.md`,
[phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted state

`SQLiteControlStore.read_review_child_evidence` is one transaction-local,
round-bounded source reader. Checkpoint 12 added exact refused, unlaunched
released and Codex zero-settled no-launch variants. Checkpoint 13 adds
`ReviewChildReturned` from exact guarded launch, raw return and actual
settled charge. Reconciliation-first and raw-return-first orders converge.
Pure `classify_normal_review_return` maps normal success and ordinary agent
failure. An eligible reusable normal-reviewer session names the exact
launched child. A shutdown cancellation additionally requires the same-run
persisted intent and exact harness shutdown reason.

Sol found and verified a fix that prevents four non-striking reasons from
becoming ordinary failure strikes: `TRANSPORT_DEATH`,
`KILLED_EXTERNALLY`, `RUNTIME_UNAVAILABLE` and `BUDGET_EXHAUSTED` remain
pending. Unsupported or contradictory normal result shapes and operator
cancellation without positive source proof also remain pending. This
reader is offline and does not build a complete `ReviewRoundFacts`, finalize
an outcome, or activate the live daemon.

Full `make test` passed: 3,107 domain, 2,732 adapter, 2,863 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks.
Ten strict xfails remained expected. Log:
`/tmp/phase4-launched-return-full.log` (ephemeral). No live service restart,
target checkout mutation, paid harness run, migration, activation or push.

## Remaining phase-4 boundaries

1. Give settled special child results typed terminal treatment backed by
   positive source evidence. A deterministic transport death needs a durable
   classifier receipt from the exception/trace source; ordinary infra death
   and budget exhaustion need non-striking outer outcomes. Preserve actual
   settled money. A generic `ReviewChildFailed` currently permits a caller
   to construct ordinary `TRANSPORT_DEATH`; production reader refuses this,
   but the domain contract and finalizer still need a typed resolution before
   cutover. Do not infer death from output prose or a terminal run state.
2. Add exact parent/claim abandonment and a transactionally retained fence
   against later admission and submission registration. A live unsealed
   parent can append stages, so terminal observed children do not close a
   round. Claim absence alone does not prove abandonment. Normal completed
   planning seal from checkpoint 11 is already available for the live path.
3. Register each submission's immutable command-to-round relationship before
   a remote write, reconcile uncertain remote outcomes and distinguish a free
   copy refusal followed by paid fallback. Only complete source-backed facts
   may enter `finalize_review_round`; store closure, selected outcome and
   projection atomically with exact replay and conflicting replay refusal.
4. Resolve Codex `_exec_once` multiple supervised attempts under one planned
   child/run before runtime cutover. Complete the rest of phase 4 and phases
   5–6 under the existing six-phase plan.

The original [review-slice handover](2026-09-27-review-slice-handover.md)
still forbids real activation, migration, service restart, target mutation,
paid harness runs, push/publication or a phase-5 live window without
explicit authorization. Run full `make test` before every coherent commit.
Use Sol or Luna for subagents, as requested by the user.
