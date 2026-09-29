# Phase 4 — implementation conventions

Applies to the [review execution design](2026-09-27-phase-4-review-execution-design.md).
Implementation starts only after phase-3 acceptance. Every implementation
assignment includes this brief and the standing deployment restrictions.

## Ownership and sequencing

The parent owns core immutable values, canonical snapshot composition,
binding/port signatures, codec/schema migrations, runtime admission context,
receipt transaction hooks and shared generation semantics. Agents work on
explicitly assigned policy, adapter, application or test files after these
contracts are fixed. No independent extensions to shared state or signatures.

Build coherent checkpoints in this order:

1. Pure execution values and ordered outcome policy, with behavioural tests.
2. Canonical payload and schema v25 persistence, indexed round/admission/return
   receipts and transactional projection, with migration/replay tests.
3. Real accounting/runtime/review composition, including copied verdicts,
   multi-child recovery and submission settlement.
4. Typed verdict release and execution re-arm through existing operator and
   effect paths, preserving rejection allowance and sibling holds.
5. Offline activation/shadow/rehearsal, old-writer fencing, dashboard authority,
   selected Store API deletion and composed slice acceptance.

Checkpoints are construction steps, not accepted authority cutovers. Until the
final step, document temporary legacy execution readers explicitly. Acceptance
requires migration-required for inactive execution authority and one runtime
authority. Keep unrelated continuation families for their own later slices.

## State and transaction boundaries

Put execution values and pure policy under `domain/`; they perform no reads,
clock access or ID generation. Adapters validate external facts and mint their
typed evidence. App composition supplies dependencies and captures immutable
round identities. Reuse existing runtime, child runs, claims, leases, money
reservations, lifecycle commands and effect receipts. Add no queue or execution
engine.

The canonical PR episode carries the explicit inactive/active execution
component. Rejection and execution authority share the existing workflow
generation/version. The active execution component has its own epoch/episode.
Never initialize active authority from optional default-zero fields or infer
it from a side table that older writers can ignore.

Allocate a durable preflight round sequence for free and paid rounds. Planned
child stages precede admission; first paid child binds actual paid provenance
and siblings share the outer identity. Returned evidence and money settle
together. Receipt settlement applies productive resets and continuation
updates; uncertain remote effects remain unresolved until reconciled. Preserve
actual accounting even when generation fencing rejects lifecycle changes.

Use short existing `BEGIN IMMEDIATE` transactions and the fixed deployment
barrier order. Fresh remote reads occur outside SQL transactions; remote writes
stay inside the existing effect authority span. Operator acceptance validates
the captured binding and shared revision in the write transaction.

## Reuse and verification

Reuse workflow identity validators, the closed workflow codec, family epoch
records, workflow-row helpers, existing effect/retry reconciliation, durable
run outcome provenance and generated API contracts. Preserve resolve's
accepted contracts. Do not infer launched or never-launched work from model
prose or a synthetic aggregate result.

Tests assert externally visible allowance, dispatch, session, hold, projection
and accounting outcomes. Cover malformed input, ordering, exact replay,
conflicting replay, lost replies, real process loss, stale generation and
independent holds. Use isolated databases/repos and actual daemon composition.
Independent adversarial review and full `make test` precede each commit; use
`uv` and Pyrefly. Dashboard changes also require its existing test gates.

No agent commits, service restarts, target checkout changes, paid harness runs,
remote publication, real family activation or live measurement windows.
Worktree agents verify their merge base against the target's real tip before
starting, and the parent verifies it again before integration.

## Empty-history activation and composed rehearsal brief

The parent owns the activation coordinator, configuration/authority binding,
shared domain values, hot reader signatures and documentation. The replay
agent owns only the new SQL replay verifier and its tests; a rehearsal agent
owns only a new composed test module. New authority stays in the existing
epoch, workflow component, imported-origin and receipt tables. No new worker
or queue is introduced: reuse the existing preflight, stage, reservation,
round settlement and recovery compositions. The configured activation service
holds fix, review and steward effect barriers through the acceptance commit;
remote facts are read outside SQL, and SQL sources are revalidated inside it.
Reuse the isolated configuration/source fixture, canonical workflow reader,
closed codec and trusted round controls. Rehearsals must start with an actual
source-revalidated empty-history activation, rather than a synthetic origin.
Modern rounds after that activation prove composed behavior; they do not prove
missing legacy chronology or authorize a historical import. Keep services
paused and use Sol/Luna agents, uv and Pyrefly; agents do not commit.
