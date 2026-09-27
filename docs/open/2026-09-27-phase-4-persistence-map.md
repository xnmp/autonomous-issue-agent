# Phase 4 — review execution persistence map

Date: 2026-09-27. **Implementation recommendation; canonical shape selected,
remaining schema/transaction details pending.** This read-only analysis supports the
[review execution design](2026-09-27-phase-4-review-execution-design.md) and
[implementation conventions](2026-09-27-phase-4-conventions.md). It does not
authorize activation or supersede those documents. The parent owns canonical
values, bindings, codec, schema and transaction signatures.

Parent decision: add the explicit execution field to `ReviewWorkflowSnapshot`
and persist a tagged `ReviewFamilyEpisodes` envelope containing rejection
authority, rejection episode and execution component. Execution is not a field
of the rejection-owned `ReviewEpisode`. Resolve retains its existing pair
encoding. Planned child stages and finalization contracts must be settled before
freezing schema v25; the pure projection checkpoint alone is not cutover.

## Canonical payload

`workflows.episode_json` currently stores the encoded pair `(authority,
episode)`, not a complete `ReviewWorkflowSnapshot`. There are two reasonable
ways to persist the execution component:

- Add `ExecutionNotActivated | ActivatedReviewExecution` to `ReviewEpisode`.
  This preserves the outer pair and changes fewer persistence call sites, but
  makes the existing rejection episode carry the separate execution component.
- Introduce a tagged `ReviewFamilyEpisodes` envelope containing rejection
  authority/episode and the execution component. This makes composition explicit
  but changes PR payload encoding, decoding and insertion paths.

Either choice must persist the active variant canonically, validate its distinct
epoch/episode, retain one workflow generation/version, and preserve resolve's
accepted encoded bytes. The selected snapshot/envelope composition keeps this
boundary explicit; the alternatives above record the design tradeoff.

Legacy PR payloads may decode as explicitly inactive only when execution
activation/component evidence is absent. Schema evolution must not create an
active counter or reusable session from defaults. An active epoch with a
missing or inconsistent component on an existing PR is corrupt authority.

## Proposed v25 projections

Use projections only where indexed authority reads need them:

| Projection | Required keys and purpose |
| --- | --- |
| `workflow_components` | Primary key `(repository, target_kind, target_number, family)`; epoch and episode ID; workflow foreign key; repository/family index. Cross-check against canonical payload. Backfill only validated existing resolve/rejection components. |
| `review_execution_rounds` | Immutable round ID; repository/PR, execution epoch/episode, generation, positive sequence, typed state/outcome payload. Unique episode/sequence; indices for episode ordering and unresolved rounds. |
| Run attachment | Nullable immutable `runs.review_round_id` and `review_child_id`, validated as a pair, with round foreign key and unique round/child identity. Preserve existing parent `workflow_binding_json` and `workflow_episode_key`. |

The round table records evidence for the existing reconciler; it does not
dispatch work or create a second queue. Allocate the preflight sequence and
advance the parent CAS version in one transaction, including free rounds.
Actual child admission remains a separate paid reservation transaction.

Family retention can no longer infer every PR family from any PR row.
`load_family_authority(REVIEW_EXECUTION)` must inspect explicit component,
round, activation/import and related retained evidence. Missing activation
with retained execution evidence fails closed. Target-based dispatch can still
choose the resolve or PR aggregate reader.

An entirely absent PR with no retained target evidence remains
`WorkflowAbsent`; it can later be explicitly initialized under both active
repository components. This differs from an existing PR missing its required
execution component. Missing indexed evidence or disagreement with canonical
state must never silently become absence.

## Existing receipt storage

Reuse indexed `dedupe_keys` records, scoped by repository and immutable IDs:

| Suggested namespace | Identity and evidence |
| --- | --- |
| `review-round-preflight` | Idempotent request token → round and allocated sequence |
| `review-round-stage` | Round/stage → immutable planned child identities |
| `review-execution-admission` | Run → round, child and captured execution binding |
| `review-execution-return` | Run → immutable returned evidence |
| `review-execution-return-disposition` | Run → final classification or stale retirement |
| `review-round-outcome` | Round → sealed outer outcome |
| `review-round-submission` | Command → originating round |

Namespace/scope/key indices serve exact receipt lookups. Round/run projections
serve operational scans; do not scan JSON payloads or sum lifetime owner deaths.
Every authoritative load validates receipt identity against its indexed
round/run facts. Planned stages precede child admission, including adaptive
validator stages.

## Transaction hooks

**Preflight and admission.** Extend
`SQLiteControlStore._validate_workflow_reservation`,
`_record_workflow_admission` and `_reservation_request_matches` with explicit
review execution handling. Validate the planned child, dual capture, current
generation and linked issue holds. Attach the child and its admission receipt
inside the existing run/budget/lease reservation transaction. A repeated run
must match round, child and execution capture, not merely its rejection binding.
Pre-reservation refusal needs its own durable idempotent receipt, without
fabricating an admitted run.

**Returned evidence.** `AccountingRuntime.run` already builds `ReturnedResolve`;
`_settle_result` delegates to `SQLiteWorkflowControl.settle_return`, which calls
`control._settle` and captures the return in one transaction. Add a typed review
child return and corresponding review-control settlement path at this seam.
Preserve resolve's behavior. Reuse existing run, lease, reservation-recovery and
provider receipts to prove launch/refusal/death and parent abandonment.
Terminal children alone do not authorize sealing an open outer round.

**Productive settlement.** Apply execution reset, applicable continuation
updates and the outer productive disposition in the same transaction as the
existing `record_review`/`open_ceiling` baseline and projection effects.
`settle_submission` currently writes its final settled marker in a later
transaction; adding reset only there would leave a crash gap. Bind settlement
through the immutable command-to-round receipt. Completed historical commands
remain historical; after activation, a new submission or free copied verdict
without its execution capture and round must refuse.

**Recovery and effects.** Keep existing `workflow_gates`, `workflow_rearms`,
`workflow_rearm_commands`, the unique pending-rearm index, lifecycle command
receipts and outbox. Extend typed cause/intent dispatch within one PR aggregate.
Execution recovery preserves rejection allowance; rejection recovery preserves
execution failures. Existing deployment barriers and short SQL transactions
continue to own effect ordering.

## Construction boundary

First define pure execution, round, outcome, return and canonical-composition
values and their behavioral contracts. The parent then selects the exact v25
projection shape before persistence work. Schema migration and strict canonical
decoding fence old binaries; supported rollback retains both SQL authorities.
No runtime cutover or selected file-API removal precedes composed acceptance.

## Migration and codec acceptance checklist

- **Mixed v24 history:** migrate resolve and review rows with baselines,
  guidance, gates/intents, historical runs/commands and activation evidence.
  Resolve payload bytes remain identical. PR rejection authority, episode,
  baseline, generation/version and receipts retain their meaning. Backfill only
  resolve/rejection component indices; execution remains explicitly inactive.
- **Atomic replay:** repeated initialization changes nothing. Interrupt v25
  before commit and verify canonical payload, component indices and schema
  history roll back together; restart applies the migration once. Extend the
  existing migration digest, contiguity and concurrent-boot tests.
- **Legacy/active truth table:** a legacy PR pair with no execution evidence
  loads inactive. An existing inactive/legacy PR under active execution
  authority refuses. Active payload with missing/mismatched execution epoch
  refuses. An entirely absent PR with no retained evidence remains absent;
  empty repository activation remains valid.
- **Corruption:** independently delete or alter parent/component/epoch rows;
  change repository, target, family, epoch or episode identity; add an active
  index to inactive payload or remove it from active payload. Authoritative
  loaders refuse without mutation. Target loads must detect retained canonical
  authority even when its index is missing. Retained rounds/imports/receipts
  must also prevent missing authority from becoming fresh state.
- **Isolation:** one PR or repository's execution history cannot imply
  activation for another. Rejection-only PR rows do not imply execution
  authority. Hot reads use component and episode indices, not historical JSON
  scans or lifetime owner counters.
- **Codec:** round-trip inactive/active envelopes, dual bindings, checkpoints
  with nondefault pin policy, imported/launched sessions and typed session
  updates. Reject unknown variants, missing required execution field, null
  component, extra fields, duplicate JSON keys, wrong nested types,
  boolean/overflow identities and inconsistent source captures. Keep resolve
  byte goldens exact. Read/persist/read must retain execution state through
  every PR writer.
- **Old binary fence:** a schema-v24 reader refuses v25 before writes. Its
  strict codec refuses the new PR envelope instead of dropping execution.
  Supported rollback retains both SQL authorities; it never downgrades schema
  or restores stopped file counters.
- **Transactional projection:** canonical execution state, component index and
  parent CAS advance together; stale CAS leaves all unchanged. Rejection and
  resolve authority survive unrelated execution updates.
- **Later admission checkpoint:** exact reservation replay includes dual
  capture, round and child. Forged attachment, stale generation, duplicate
  child under another run, or mismatched receipt must refuse before paid
  run/lease/reservation creation. These checks belong to the admission seam,
  not a codec-only construction checkpoint.
