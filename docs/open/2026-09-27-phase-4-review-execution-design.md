# Phase 4 — review execution design

Date: 2026-09-27. **Independently reviewed implementation design.** Implementation
begins after phase-3 acceptance. Companion to the
[continuation inventory](2026-09-27-phase-4-continuation-inventory.md), ADR 0009
and ADR 0015. Real activation and deployment remain paused.
Implementation ownership and checkpoints are in the
[conventions brief](2026-09-27-phase-4-conventions.md).

## Design references

The ordering and duplicate-delivery rules follow the established per-entity
sequence, optimistic concurrency and idempotent-consumer approach described in
Microsoft's [event sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing).
Use those rules within the existing receipt tables and aggregate; no separate
event platform is needed. The same document cautions that complete historical
replay becomes expensive, so operational reads need indexed episode boundaries.
SQLite's [transaction contract](https://sqlite.org/lang_transaction.html)
supports the existing short `BEGIN IMMEDIATE` acceptance transactions. Remote
reads remain outside them under the deployment barrier.

## Domain boundary

Keep the existing PR workflow key and its single generation/version. Add an
explicit execution component to the review snapshot:

- `ExecutionNotActivated` owns no counter or reusable session.
- `ActivatedReviewExecution` carries a separately activated `REVIEW_EXECUTION`
  epoch and a `ReviewExecutionEpisode`.

The rejection episode retains its own authority, counting rule, reviewed
baseline and guidance. An execution reset cannot renew rejection allowance;
a rejection re-arm cannot erase unproductive failures. Each operation advances
the shared generation once and preserves unrelated gates. Do not create a
second PR workflow with independent generation or infer active execution
authority from default-zero fields.

Capture a typed `ReviewExecutionBinding` containing the existing rejection
binding and the execution epoch/episode. Scope it to review rounds and returns;
resolve and unrelated commands retain their current bindings. Post-activation
paid admission refuses a review carrying only the old binding. Lifecycle
commands remain fenced by the parent generation; commands/results that update
execution continuity also carry the immutable round identity and execution
capture.

The execution episode owns the unproductive allowance, runtime-tagged session
continuation, displayed round number, durable pin-deferral state and applicable
admission watermarks. Reusable sessions require a real handle, runtime and
source admission. Legacy `multi`, `github-review` and `codex-review` markers
represent fresh/mode state, never reusable session handles. A fresh review
after operator recovery reads the committed guidance and starts a fresh
session.

## Outer-round outcome contract

One immutable review-round identity groups every paid gate, dimension and
validator invocation. Allocate a monotonic episode round sequence at durable
preflight, including unpaid pin deferrals and productive copied-verdict rounds.
The sequence orders outcomes; a separate paid admission records actual spend
provenance. First paid child admission binds that round in the existing
reservation transaction; subsequent children reuse it. Keep each child's
accounting receipt independently. Never charge one unproductive round for
every abandoned multi-review child.

An idempotent preflight identity covers never-launched and pin-deferral
outcomes without fabricating a paid admission. Preserve the existing pin
policy explicitly rather than treating free failures as paid work.

Use typed outcomes with verified provenance:

- productive submission, bound to its durable command receipt;
- paid no-progress, with launched-run evidence;
- infrastructure death, with its run evidence;
- admission refusal before a reservation exists, with the refusal receipt;
- reserved-but-never-launched release, with its reservation/release evidence;
- pin deferral, with its durable preflight identity.

Preserve deterministic-death classification and its configured allowance
effect: the current rule saturates the count at the captured ceiling using
`max(count, ceiling)`; it does not add the ceiling to the count. Represent
ordinary strike and deterministic saturation as distinct debit variants, and
apply at most one to the outer round. Operator cancellation and shutdown remain explicit non-charging
outcomes, while any actual spend still settles normally.

An uncertain submission remains unresolved until reconciliation decides it.
Record returned evidence before parent orchestration can disappear. After a
parent death, reconcile terminal children to one outer disposition. Terminal
children alone do not prove round completion: persist the selected mode and
each stage's immutable planned child identities before their admission, plus
an explicit sealed outcome/submission linkage. Adaptive validator stages are
recorded before launching them. A round left open requires proven parent/claim
abandonment before it may be finalized; no paid children proves an unpaid
abandonment, while admitted children retain their actual outcome provenance.
Reuse the current claims, leases and run-recovery evidence; this is workflow
state, not a second scheduling queue. Preserve
typed refusal/death provenance through multi-review synthesis; flattening all
errors to an undifferentiated `AgentResult` cannot authorize a counter charge.
Derive launched/refused/dead child facts from durable outcomes when synthesis
has flattened the runtime's exception.

## Ordering and reset

Commit productive reset and applicable session/round updates with bound
submission settlement, alongside the reviewed baseline and projection. The
synchronous `_progress` tail is not authority.

Maintain immutable ordered outcome evidence and a monotonic last-productive
round boundary. A failure or pin deferral whose round precedes that boundary
remains audit; a later round's failure survives delivery of an older success.
A copied bound verdict is productive without a paid admission and resets only
its applicable earlier debits/pin streak. It preserves the current session and
displayed round number, matching current behaviour.

Continuation updates have their own highest-applicable round watermark and a
typed `Preserve`, `Replace` or `Clear` disposition with eligible producer
provenance. Gate/validator results, free copied verdicts and admission/pin
refusals preserve the reviewer session. Preserve does not advance the session
watermark: a newer free copy cannot suppress a delayed applicable session update
from an earlier real reviewer. Displayed-round updates have a separate
watermark. A newer eligible reviewer invocation
returning no handle clears an older handle; a sandbox-incompatible resume
runtime explicitly requires fresh continuation. Stale generations
still settle their money and receipts without changing the new episode.

Imported inherited failure debit has explicit pre-activation provenance and
clears only at a verified productive boundary. The current owner-wide
`abandoned_dispatch_counts` sum cannot supply this reset semantics.

Keep an aggregate prefix through the largest contiguous finalized round
sequence and project the ordered suffix after it. A missing/open earlier round
prevents prefix advancement even when later outcomes arrived. Persist prefix
advancement, outcome receipts and the canonical projection atomically. Index
the suffix by execution episode and sequence; do not scan owner lifetime
history or truncate unresolved gaps. Recovery must terminalize abandoned
preflights from evidence. Exact duplicate outcomes are inert; contradictory
outcomes for the same immutable round are corruption, never last-write-wins.

## Recovery operations

Add distinct typed causes for verdict escalation (exact submission receipt and
head) and unproductive exhaustion (execution episode and outcome evidence).

Verdict release removes only its selected verdict cause, owes a fresh review
and session, and preserves rejection allowance and any unproductive hold.
Execution-ceiling re-arm renews only the execution episode. Neither operation
dismisses GitHub reviews. Guidance remains on the shared review aggregate.

Extend the aggregate's single pending intent with execution-specific variants;
do not allow concurrent pending intents under separate components. Retain the
existing fixed barrier order, quiescence requirements, typed cancellation and
continuation, and verified projection completion. Completion can still leave
the PR held by another cause.

## Persistence and migration

The parent owns snapshot/binding/codec/schema changes, round identity passed
through runtime context and reservation, ordered admission/return settlement,
and gate/intent dispatch. An additive schema change uses v25 with migration
tests. No new execution engine or command queue is introduced.

Persist the active execution variant in the canonical PR episode payload;
do not hide it only in an optional side table readable by an old rejection-only
writer. Schema v25 fences old binaries, and the strict canonical decoder
rejects unknown execution variants rather than dropping them.

Family retention checks must use explicit component provenance; every PR row
currently implies rejection authority. An activated execution epoch without
its required component is corruption, never an invitation to initialize it.
During construction checkpoints the unmigrated execution reader may remain
explicitly temporary. At execution-slice acceptance, inactive execution
authority means migration-required: delete selected runtime file readers and
writers, retaining an importer only. No accepted deployment selects between
two runtime authorities.

Offline inventory/shadow/rehearsal/activation includes all role roots, sessions,
rounds, both counter key spaces, run/death and submission receipts, and verdict
hold provenance. Ambiguous issue-counter mapping or unknown death/reset
attribution blocks import with reviewable evidence. Never duplicate an issue
counter into several PRs or silently zero it. Rejection epoch and baseline are
retained.

A legacy real session without an identifiable source admission either blocks
import or uses an explicit imported-session provenance variant with its source
evidence. Never fabricate a paid admission to make a legacy handle reusable.

Activation requires full quiescence of outer rounds, child runs, accounting
reservations, unclassified returns, submissions and pending re-arms. It keeps
the shared generation and verified rejection baseline, advances the aggregate
version to invalidate captured operator CAS requests, and records the new
execution epoch/component atomically. Active execution admission requires its
captured binding; every submission that can update execution, including a
free copy, requires its execution-bound round. Historical completed effects
may be read/replayed but acquire no new execution authority. Prove that a
missing capture is refused after activation. No uncertain old command crosses
this boundary silently.

Shadow compares eligibility, inherited failures, held/releasable causes,
session choice and fresh-review obligations under identical observations.
Rollback keeps both activated SQL components and all later receipts; retained
files remain evidence only.

## Acceptance before cutover

Require behavioural tests for reordered outcomes, multi-child deaths, lost
submission responses, process loss at settlement/reset boundaries, stale
generation results, applicable sessionless supersession, runtime switches and
sandbox-incompatible resume, free copied verdicts, terminal/reopened PRs,
unpaid preflight outcomes reordered across free/paid progress, issue relinking, malformed or
ambiguous imports, replay, competing operator recovery, and retained sibling
holds/allowances. Exercise real daemon/accounting/effect composition. Delete
selected Store APIs only after scheduler, operator and dashboard share the
new authority. Full `make test` and independent adversarial acceptance precede
each coherent implementation commit.
