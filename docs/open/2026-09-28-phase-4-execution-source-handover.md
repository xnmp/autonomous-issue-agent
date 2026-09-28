# Phase 4 handover — execution activation sources

Date: 2026-09-28 Australia/Sydney. Branch `dev`. The six-phase goal remains
active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending. Start
with `CLAUDE.md`, `AGENTS.md`, the
[phase-4 progress record](2026-09-27-phase-4-progress.md), the
[execution design](2026-09-27-phase-4-review-execution-design.md), and the
[origin and compaction contract](2026-09-28-phase-4-compaction-contract.md).

## Source inventory checkpoints

- `d04070b` added a read-only, bounded file inventory for review
  `pr-N.session`, `pr-N.round`, and both positive-issue and negative-PR
  `infra-deaths` namespaces. It retains malformed names, unreadable files and
  cross-root duplicates rather than selecting or defaulting them.
- `97668c4` added a paged, read-only `runs` scan for one review owner under an
  explicit SQL snapshot. The source record retains the dispatch, lifecycle,
  outcome, binding, episode, and round/child fields; its digest excludes the
  SQLite page cursor. Oversized and invalid UTF-8 fields produce explicit
  unreadable records. Negative and zero rowids are included.
- Checkpoint 32 adds pure classification of an abandoned run's review PR and
  signed counter key. It requires complete dispatch provenance, preserves an
  unknown-attribution result, and verifies that an attributed variant agrees
  with its own source. A positive legacy key must match the recorded issue;
  its issue-to-PR mapping remains a separate activation decision.
- Checkpoint 33 adds a paged, read-only scan of review `submit_review`
  lifecycle commands and their optional effect receipts in one explicit SQL
  snapshot. It retains every current command and receipt column, includes
  negative and zero rowids, and marks oversized or malformed source fields
  unreadable. A source digest excludes its page cursor. It does not interpret
  the command as a productive review or infer a missing receipt.
- Checkpoint 34 adds a paged, read-only scan of the submission registration,
  round index and recorded-review settlement namespaces in `dedupe_keys`.
  These raw rows must still be joined to command/effect, round and verdict
  sources; their presence alone does not prove a productive reset.
- Checkpoint 35 adds a bounded, read-only owner lineage source. It includes
  daemon boot's implicit `repository:review` predecessor, configured prior
  deployments and transitive retirement links. Contradictory or unreadable
  links block a complete lineage claim.
- Checkpoint 36 joins a plain successful review submission to its effect,
  recorded marker and exact accepted workflow transition. Its verified
  result proves a recorded baseline for that command only. It does not certify
  the full historical snapshot, execution productivity or a reset. Escalated
  verdict holds and outer-round ordering remain separate obligations.
- Checkpoint 37 adds a one-pass, bounded, read-only run scan across the
  rechecked owner closure. It blocks unreadable owner sources, changed lineage,
  oversize retained sources or total scan excess. A caller-held complete
  result must be revalidated against SQL under activation barriers before use.
- Checkpoint 38 adds a read-only, source-backed link from an inventoried
  submission command to its exact registration, index, preflight, evolved
  round plan and stage receipts. It rechecks lineage and bounded command/effect
  rows. The verified value contains only the round identity and source digests;
  kind, productivity and reset still need separate proof.
- Checkpoint 39 adds a bounded SQL page scan of every review round row for one
  repository. It preserves all 13 columns, malformed and oversized rows,
  negative/zero rowids and matching BLOB aliases. A page is an observation,
  not a complete round inventory or a productive-outcome proof.
- Checkpoint 40 exhausts those pages within bounded total row and byte limits,
  including a one-million-row ceiling on the shared round table. A second SQL
  read blocks partial or changed caller-held inventories. This proves source
  completeness within the stated bounds, not a productive outcome or reset.
- Checkpoint 41 adds bounded, read-only pages for five durable review-round
  outcome and productivity receipt namespaces. It preserves all `dedupe_keys`
  source columns and explicit malformed or oversized entries. A one-million-row
  shared-table scan ceiling blocks unbounded work. Pages alone do not prove a
  complete receipt inventory, productivity or ordered resets.
- Checkpoint 42 exhausts those pages within 65,536 matched rows, 64 MiB of
  source text and a one-million-row bound on the shared `dedupe_keys` table.
  Unreadable and duplicate sources block a complete set. A second SQL read
  rejects partial or changed caller-held inventories. This is still raw
  evidence; it does not certify a productive outcome or execution reset.
- Checkpoint 43 joins one sealed round with its exact permanent outcome receipt
  after revalidating both complete SQL inventories. It checks canonical plan,
  planning, closure and outcome encodings and independently re-derives the
  outcome from closure facts. The proof is per round and does not yet establish
  productivity, baseline pairing, outer-round order or reset.
- Checkpoint 44 revalidates each complete SQL set once, then accounts for all
  sealed/open rounds and every outcome/productivity receipt key. Preparation
  and stale receipts must match exact closure facts; superseded/ceiling tuples
  must be canonical and typed. Contradictory or orphaned auxiliary receipts
  block. Their external authority, baseline and reset order remain unproved.
- Checkpoint 45 joins one confirmed plain verdict to its exact sealed
  productive round and accepted recorded baseline. It requires the active
  execution epoch/episode, effect receipt identity, round disposition and
  rejection fence to agree, requires permanent baseline receipts with valid
  claim times, and rejects stale, superseded or ceiling
  productivity receipts for that round. This does not account for every
  command or prove ordered resets.
- Checkpoint 46 exhausts the review submission command pages within a
  one-million-row shared-table scan ceiling, 65,536 matching commands and
  64 MiB of source fields. Matching BLOB repository/kind aliases block as
  malformed evidence. A second SQL read rejects partial or changed
  caller-held inventories. Submission receipt completeness and outcome joins
  remain separate.
- Checkpoint 47 exhausts registration, round-index and recorded-review
  receipt pages within the same one-million-row shared-table, 65,536
  matching-row and 64 MiB limits. Matching BLOB scope/namespace aliases
  block. SQL revalidation rejects changed or partial caller-held sets. Raw
  receipt presence does not yet establish the exact command/round authority.
- Checkpoint 48 revalidates complete command, submission receipt and
  round/outcome storage sets in one SQL snapshot. Canonical round plans and
  registrations pair every registered command to its permanent derived index;
  every planned submission and marker is accounted for. Historical commands
  without registration remain explicit. This is storage accounting, not a
  baseline, productivity or reset proof.
- Checkpoint 49 revalidates those complete sets before joining one succeeded
  registered command, digest-checked effect, confirmed sealed outcome and
  exact stale-productivity receipt, with no recorded marker or conflicting
  productivity receipt. It requires a unique remote review ID and verifies the
  command payload's PR/head and matching outcome/stale claim times. The typed
  result proves a recorded receipt path;
  historical generation ordering and reset order remain separate source-backed
  obligations.
- Checkpoint 50 inventories every generation receipt in one bounded SQL
  snapshot, with repository-alias refusal and rowid-aware revalidation. A
  source matcher checks one permanent receipt against its accepted transition,
  derived boundary identity, mutation-specific audit and command shape. The
  result is an observation
  that must be rechecked in SQL before authority use. It does not prove that
  accepted commands were persisted, that arbitrary imported rowids establish
  chronology, or that a stale productivity receipt crossed a generation.
- Checkpoint 51 writes a typed stale-productivity receipt with the current
  workflow revision, execution binding and digest of the complete intervening
  generation-boundary ID chain in the same transaction as round closure. The
  writer reloads the current workflow and requires the accepted contiguous
  chain; the hot read checks its digest and revision. Existing two-field
  receipts remain readable but have no historical crossing proof. The
  generation source matcher accepts zero/negative SQL rowids and makes no
  chronological claim from their order.
- Checkpoint 52 revalidates complete submission settlement storage and
  generation receipts in one explicit SQL transaction, then joins a typed
  stale receipt to every intervening accepted generation boundary. The join
  checks the ordered generation chain, writer digest, final binding and
  current workflow authority; missing or changed rows, aliases and historical
  two-field receipts block. Accepted-row candidates use bounded indexed
  namespace/key reads that include TEXT and BLOB aliases. The matched value
  remains an observation and needs this SQL recheck at authority use. It
  neither classifies all successful commands nor proves ordered resets.
- Checkpoint 53 revalidates complete settlement, generation and owner-lineage
  sources in one SQL snapshot, then classifies every submission with either a
  successful command state or effect status. Exact plain and typed stale
  proofs are retained; contradictory, unregistered, escalated and old
  two-field stale cases remain explicitly unresolved. The complete result
  validates candidate coverage, but is observational and cannot reset or
  activate an execution. Immutable indexes and snapshot-local caches keep
  repeated classifications bounded by their relevant sources.
- Checkpoint 54 matches an escalated verdict to the accepted recorded-baseline
  transition, its exact harness-owned pull request observation and the
  durable guarded relabel command enqueued in that transition. It checks the
  initial outbox identity and payload digest. Complete classification now
  has a distinct escalated productive-round variant when its sealed outcome
  agrees. This proves the accepted hold plan, not remote relabel delivery or
  the present GitHub hold state.
- Checkpoint 55 revalidates complete classifications in one SQL snapshot and
  orders every allocated round by execution identity and preflight sequence.
  Open and unproductive rounds remain explicit; each productive sealed outcome
  requires exactly one classified successful command. Conflicting parent
  rejection metadata and missing sequences block. For now, only rounds whose
  binding exactly matches the current activated workflow binding can produce
  complete coverage. Older generations require a complete accepted-boundary
  join before replay or import. The result proves coverage, not reset order.
- Checkpoint 56 separately revalidates complete round/outcome and generation
  inventories in one SQL snapshot, matches each retained boundary to its
  accepted transition, and assigns every allocated round in a current,
  uncompacted episode to the binding active at its sequence. It handles
  repeated advances at the final sequence, and blocks missing receipts,
  accepted-row aliases and unaccepted round generations. This allocation
  proof must still be joined to checkpoint-55 productivity coverage and
  replayed against the execution checkpoint. Compacted and retired episodes
  await source-backed origin/checkpoint receipts and archived audit.
- Checkpoint 57 joins complete success classification, ordered outcome
  coverage and accepted allocation generations in one SQL snapshot, then
  replays the outcomes from the persisted uncompacted checkpoint and compares
  the current execution state. Plain/escalated productive, unproductive and
  open-gap fixtures pass; unresolved historical ceiling submissions block.
  The result is an observation only: no source-backed initial checkpoint or
  origin receipt exists yet, so it cannot certify imported state or activate
  execution.
- Checkpoint 58 classifies an accepted ceiling escalation through its exact
  successful effect, registered and sealed productive round, permanent ceiling
  decision, fresh harness observation, accepted `OPEN_GATE` transition, and
  durable guarded relabel command. It checks the command's initial dedupe
  identity and transition claim time. A real historical ceiling plus stale
  verdict now passes ordered checkpoint replay. Refused ceiling decisions
  remain unresolved until their prior workflow state has independent source
  proof. This is still observational; it does not make a ceiling escalation a
  reviewed baseline or prove remote hold delivery.
- Checkpoint 59 hardens the shared accepted-source reader. Each receipt lookup
  checks all eight TEXT/BLOB variants of its namespace, scope and key through
  the composite SQLite index, requiring exactly one canonical TEXT identity.
  A matching BLOB alias cannot coexist unnoticed with a valid accepted gate,
  registration, stage, baseline or generation source. Focused ceiling-gate
  regressions cover aliases in all three identity columns. This closes source
  ambiguity; it does not establish an execution origin or archived audit.
- Checkpoint 60 adds a typed imported-origin receipt and selected import
  decision. The receipt records the sequence-zero checkpoint, activation-plan
  digest, source and inventory digests, and decision digest. A fixture writes
  the receipt with its execution component in one transaction. Active hot
  reads require the canonical, nonreclaimable receipt and exact activation
  epoch, plan, import decision and claim time; missing, changed, oversized or
  TEXT/BLOB-aliased ancestry blocks. This is a fail-closed read prerequisite,
  not a production activation writer: the fixture's source digests are
  synthetic, and no complete legacy file/run/counter replay yet mints this
  evidence. Initialized and renewed origins remain unsupported. Hot reads do
  not replay retired prefixes after compaction; that requires archived-source
  audit and durable checkpoint receipts.
- Checkpoint 61 parses observed legacy session, round and counter file values
  into typed candidates, mode markers, numbers and explicit blocked evidence.
  Session candidates retain their source digest and still require admission
  provenance before reuse. Real persisted mode markers cannot become session
  handles. The supplied file tuple is bound to inventory root and PR or
  counter key; duplicates, changed digests, malformed values, disappearing
  listed files and bounded-scan overflow block it. This proves only the
  supplied tuple is internally complete. Activation must re-read every
  configured root under its barriers, join issue counters to PRs and verify
  session provenance before granting authority.

Independent Sol review of checkpoint 61 found that persisted session mode
markers must be retained as distinct nonreusable evidence; this was fixed and
regression-tested. A second pass found no remaining material issue in the
typed parsing or bounded scanner. The scanner also converts a listed file
that disappears during reading into unreadable evidence; a regression covers
the race. The full gate log is
`/tmp/phase4-legacy-file-values-gate.log` (ephemeral).
The gate passed: 3,236 domain, 3,108 adapter and 2,866 app tests, plus
hooks, governor, gate parity, four import contracts and both Pyrefly checks.
Ten strict xfails remained expected.

The checkpoint-60 full `make test` gate passed: 3,202 domain, 3,105 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-imported-origin-final-gate.log` (ephemeral). Independent Sol
review found and verified fixes for decision-semantic, activation-time and
TEXT/BLOB integer-alias gaps. The full gate also exposed older fixtures that
changed initial checkpoints or forged generation without source evidence;
their tests now assert the earlier origin refusal or use an accepted source
transition. The composed recovery fixture now mints its synthetic origin in
the same transaction as its component.

The checkpoint-59 full `make test` gate passed: 3,201 domain, 3,092 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-source-alias-gate.log` (ephemeral). Independent Sol review
confirmed the bounded composite-index lookup and found no blocking issue.

The checkpoint-58 full `make test` gate passed: 3,201 domain, 3,089 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-ceiling-productivity-gate.log` (ephemeral). Independent Sol
review found two source checks missing from the first proof: the relabel
command's persisted dedupe identity and the accepted transition's claim time.
Both are now checked and covered by mutation regressions.

The checkpoint-37 full `make test` gate passed: 3,192 domain, 2,885 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-complete-runs-gate.log` (ephemeral). Independent Sol review
found the initial per-owner scan repeated full table scans and that a
caller-held complete value could be partial. A one-pass total scan limit and
SQL revalidation address both; the second pass found no remaining medium or
high issue. Exact historical workflow snapshots and productive execution
resets remain unproved by the current source readers.

The checkpoint-38 full `make test` gate passed: 3,192 domain, 2,902 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-round-link-gate.log` (ephemeral). Independent Sol review found
unbounded re-reads and a missing exact command comparison; both were fixed,
and the final pass found no remaining medium or high issue.

The checkpoint-39 full `make test` gate passed: 3,192 domain, 2,908 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-round-source-gate.log` (ephemeral). Independent Sol review found
no medium or high issue in the bounded round row reader.

The checkpoint-40 full `make test` gate passed: 3,192 domain, 2,912 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-complete-rounds-gate.log` (ephemeral). Independent Sol review
found the initial scan could traverse unlimited foreign-repository rows;
the global table bound addresses that finding, and its second pass found no
remaining medium or high issue.

Independent Sol review of checkpoint 41 found that an initial indexed query
sorted every matching receipt for each page. A bounded rowid scan replaced it;
the second pass found no remaining medium or high issue. The page reader
retains raw outcome/productivity sources without interpreting them.
The full `make test` gate passed: 3,192 domain, 2,919 adapter and 2,866 app
tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-outcome-receipt-gate.log` (ephemeral).

The checkpoint-42 full `make test` gate passed: 3,198 domain, 2,923 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-complete-outcome-receipt-gate.log` (ephemeral). Independent Sol
review found no medium or high issue in the complete inventory and SQL
revalidation.

The checkpoint-43 full `make test` gate passed: 3,198 domain, 2,934 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-round-outcome-storage-gate.log` (ephemeral). Independent Sol
review found and drove fixes for an outcome that contradicted closure facts,
an unsafe decoder diagnostic and an expiring receipt; its final pass found no
remaining medium or high issue.

The checkpoint-44 full `make test` gate passed: 3,198 domain, 2,948 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-whole-outcome-storage-gate.log` (ephemeral). Independent Sol
review found an auxiliary receipt gap; the outcome-specific constraints and
regressions address it, and its second pass found no medium or high issue.

Independent Sol review of checkpoint 45 found that an initial plain verdict
join omitted the active execution episode, tolerated contradictory
productivity receipts and accepted reclaimable baseline receipts. The typed
join and source-level regressions address all three;
the final pass found no remaining medium or high issue. The full gate log is
`/tmp/phase4-plain-productivity-gate.log` (ephemeral): 3,198 domain, 2,958
adapter and 2,866 app tests passed, plus hooks, governor, gate parity, four
import contracts and both Pyrefly checks. Ten strict xfails remained expected.

Independent Sol review of checkpoint 46 found no medium or high issue in the
complete command inventory or page query. Focused tests cover changed and
partial source sets, BLOB aliases, malformed rows and scan bounds. The full
gate log is `/tmp/phase4-complete-submissions-gate.log` (ephemeral): 3,198
domain, 2,965 adapter and 2,866 app tests passed, plus hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected.

Independent Sol review of checkpoint 47 found no medium or high issue in the
complete submission receipt inventory. Focused tests cover changed and partial
sets, matching BLOB aliases, malformed rows and scan bounds. The full gate log
is `/tmp/phase4-complete-submission-receipts-gate.log` (ephemeral): 3,198
domain, 2,972 adapter and 2,866 app tests passed, plus hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected.

Independent Sol review of checkpoint 48 found two constructor gaps: derived
index and payload identity, and sealed closure-plan equality. Trusted proof
types now live beside their wire codec in the adapter, with direct forged-value
regressions. The final pass found no medium or high issue. The full gate passed:
3,198 domain, 2,986 adapter and 2,866 app tests, plus hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected. Log:
`/tmp/phase4-submission-settlement-storage-gate.log` (ephemeral).

Independent Sol review of checkpoint 49 found that a stale receipt does not
itself prove the generation crossing, plus gaps in command payload decoding,
remote review ID uniqueness and deeply nested JSON handling. The typed claim
is now limited to receipt-path evidence, the joined sources are checked, and
targeted regressions cover each gap. The final pass found no medium or high
issue. Full gate log: `/tmp/phase4-stale-receipt-path-gate.log` (ephemeral).
The gate passed: 3,198 domain, 3,000 adapter and 2,866 app tests, plus hooks,
governor, gate parity, four import contracts and both Pyrefly checks. Ten
strict xfails remained expected.

Independent Sol review of checkpoint 50 found initial accepted-row alias,
mutation-audit and constructible-authority gaps. The returned value is now an
explicit source match, the bounded alias scan blocks canonical repository
duplicates, and mutation-specific audits and command structure are checked.
Its second pass found no medium or high issue within that observational
scope. Full gate log: `/tmp/phase4-generation-boundary-gate.log` (ephemeral).
The gate passed: 3,198 domain, 3,022 adapter and 2,866 app tests, plus hooks,
governor, gate parity, four import contracts and both Pyrefly checks. Ten
strict xfails remained expected.

Independent Sol review of checkpoint 51 found a missing hot-read check that
the captured settlement revision follows the final boundary revision, and a
positive-rowid assumption in the source matcher. Both were fixed and
regression-tested; its final pass found no further medium or high issue. The
real two-round fixture covers an accepted rearm with an older successful
submission still open, and a failed stale-receipt insert proves transaction
rollback. Full gate log: `/tmp/phase4-stale-generation-witness-gate.log`
(ephemeral). The gate passed: 3,199 domain, 3,031 adapter and 2,866 app
tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly checks. Ten strict xfails remained expected.

Independent Sol review of checkpoint 52 found that the initial join repeated
a whole-table accepted-row scan. Indexed namespace/key batches now retain
TEXT/BLOB alias detection with a total candidate bound; its second pass found
no remaining medium or high issue. Real one- and two-advance fixtures cover
the full chain, missing intermediate receipt, changed transition, forged
future revision, legacy two-field receipt and arbitrary imported rowids. The
full gate log is `/tmp/phase4-stale-crossing-join-gate.log` (ephemeral):
3,199 domain, 3,045 adapter and 2,866 app tests passed, plus hooks, governor,
gate parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected.

Independent Sol review of checkpoint 53 found a crossed-owner plain proof
gap and repeated full-set work. The owner equality requirement, immutable
source indexes and transaction-local caches address both; the final review
found no remaining medium or high issue. Tests cover a crossed owner, a
shared remote review ID, partial or changed source sets, contradictory
success claims, unregistered historical commands, escalated verdicts, old
stale receipts and multiple typed stale paths. Full gate evidence follows.
The log is `/tmp/phase4-success-classification-gate.log` (ephemeral):
3,200 domain, 3,056 adapter and 2,866 app tests passed, plus hooks,
governor, gate parity, four import contracts and both Pyrefly checks. Ten
strict xfails remained expected.

Independent Sol review of checkpoint 54 found a projected-command digest
gap and stale owner lineage in the standalone matcher. Both were fixed, along
with an outbox logical-key/occurrence mismatch, and regression-tested. The
final review found no remaining medium or high issue. The complete classifier
distinguishes plain, escalated-with-plan, typed stale and unresolved
successes. Full gate evidence follows.

The log is `/tmp/phase4-escalated-hold-plan-gate.log` (ephemeral):
3,200 domain, 3,065 adapter and 2,866 app tests passed, plus hooks,
governor, gate parity, four import contracts and both Pyrefly checks. Ten
strict xfails remained expected.

These are offline source readers and classifiers. None creates an execution
episode or changes production review eligibility. The current review runtime
still reads legacy session/round files and counters. No execution-family
activation, live migration, target mutation, service restart, paid run or push
occurred.

## Next required boundary

The next proof must replay ordered outcomes for each execution episode, with
the delivered or otherwise verified current state of escalated holds and
unresolved historical submissions kept explicit. Every potentially successful
submission now has an observational classification against its bound effect,
recorded baseline, sealed outcome and productivity path. An accepted relabel
plan does not establish its remote effect. Raw recorded markers and completed
agent runs are observations, not reset authority.

Legacy stale receipts store only `(facts, outcome)` and the writer's claim
time; they remain unknown for historical crossing. New typed receipts capture
the settlement revision and full crossing-chain digest. The offline join now
revalidates complete generation and settlement source sets together and
matches every intervening accepted boundary. A two-advance fixture proves the
whole chain; removing either source blocks it. Missing or changed source rows,
old two-field receipts and unclassified commands block activation authority.
[SQLite's rowid rules](https://www.sqlite.org/autoinc.html) do not make rowid
order a durable clock.

1. Revalidate the bounded owner closure and run inventory under activation
   barriers before any import commit. Daemon boot's `adopt_legacy_owner`
   rewrites some prior run owners, so neither the current owner nor configured
   owners alone establish complete historical run evidence. Caller-held
   complete values are observational until this SQL recheck succeeds.
2. Classify refused ceiling decisions and other unresolved historical
   commands, then replay every eligible older-generation outcome against a
   source-backed execution origin/checkpoint to establish resets. Verify
   delivered escalated holds or their current observed state, and resolve or
   block historical unregistered and old stale submissions. A completed
   agent run is not itself a productive review; unknown death or reset
   attribution blocks import with reviewable evidence.
3. Re-read and compare the complete legacy file inventory across every
   configured root under activation barriers. Join positive issue-keyed file
   counters to PRs with fresh complete linked-issue observations; shared or
   ambiguous issue counters cannot be copied to multiple PRs. The typed
   session/round/counter parser keeps malformed values and mode markers
   explicit, but a syntactically valid session candidate still needs
   source-backed admission provenance before reuse.
4. Build the production import writer from revalidated legacy sources and
   activation decisions, then mint initialized/renewed origin variants,
   checkpoint receipts and the archived-source audit required by the
   compaction contract. The new imported-origin hot read is a prerequisite;
   it does not certify synthetic or unverified source digests. The 4,096-round
   and generation-boundary suffix limits remain activation gates until that
   work passes.
5. Wire active round identity through production dispatch, child reservation
   and verdict registration; complete composed fault/recovery tests, offline
   shadow, rehearsal and activation. Phase-5 live windows require explicit
   user authorization.

Use Sol or Luna for subagents. Run full `make test` before each coherent
commit. Keep the six-phase progress board updated at accepted checkpoints;
the user explicitly requested it on 2026-09-28.
