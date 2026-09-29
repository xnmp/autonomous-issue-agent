# Phase 4 handover — execution activation sources

Date: 2026-09-29 Australia/Sydney. Branch `dev`. The six-phase goal remains
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
- Checkpoint 62 adds a configured-root file snapshot and revalidation reader.
  Every configured path, resolved path and directory identity (including an
  absent root) is bound to the parsed source digest. A read checks that root
  identities stayed stable while scanning; a later read under caller-held
  activation barriers must match the complete prior snapshot. Directory-entry
  and retained-byte budgets are shared across all roots, so overflow stops the
  scan before collecting every root. Tests cover partial held sets, added,
  removed and changed files, ambiguous duplicates, empty-root changes and a
  symlink retarget to a directory with identical file contents. This remains
  an observational reader; the production activation transaction must hold
  the barriers, resolve positive issue counters and prove session admission.
- Checkpoint 63 adds a complete open-PR linkage reader and pure legacy counter
  attribution. It reads the bounded paginated open-PR list, each PR's validated
  lifecycle/linkage view and its linked issue, then rechecks the listing. A
  fresh re-read must match the held repository-bound linkage source. Positive
  issue keys use the runtime's closing-reference-then-branch rule; negative
  keys name their PR directly. Every parsed file counter is assigned once to
  an open PR or retained as unassigned historical evidence. A positive key
  shared by open PRs, malformed/unavailable linkage or an unrepresentable
  combined PR total blocks. This remains observational: the production writer
  must join it to configured-root revalidation and historical run/reset
  evidence inside activation barriers before importing any debit.
- Checkpoint 64 adds a fail-closed session-file import decision. A configured-root
  re-read retains repository, PR, root, parsed runtime/handle and file digest
  for a real legacy session, but returns an unproven source variant rather than
  a reusable handle. The old writer could leave a prior handle after a newer
  successful multi review or sessionless re-review, so a current reviewed
  baseline cannot identify the handle's producing reviewer. Absent files and
  known mode markers produce no reusable session. Session admission remains
  blocked until a durable producer can be joined; the observational reader
  does not authorize activation.
- Checkpoint 65 composes first observation of a newly discovered PR with a
  fresh review-execution episode when that family is already active. One
  transaction writes the rejection workflow, execution component, harness
  observation and immutable initialized-origin receipt. The receipt binds the
  initial rejection and execution identities, captured policy, activation
  epoch, plan/source/inventory digests and activation time, observation ID and
  sequence-zero state. Hot reads require exact canonical origin, activation
  and observation sources, a complete postactivation observation within the
  freshness bound, and no conflicting import. An activation crossing the
  remote read refuses creation so the next read can retry. This path does not
  activate the family, import an existing PR or prove archived compaction.
- Checkpoint 66 adds a barrier-held, read-only source scope for review
  execution counter import. It reacquires all three deployment effect barriers
  in the established order, revalidates every configured legacy file root and
  the repository-bound complete open-PR linkage, then attributes every parsed
  positive issue and negative PR counter under those barriers. A typed result
  binds the configured-root identities to the mapping digest; changed files,
  changed or foreign PR linkage and ambiguous issue attribution block. The
  context stays open for a future import transaction, but does not authorize
  any inherited debit or reset without SQL owner/run/settlement revalidation.
- Checkpoint 67 composes the held counter sources with complete owner-run,
  submission-settlement and generation-receipt SQL inventories in one explicit
  transaction on the barrier-locked control database. The scope token expires
  when its context exits, so escaped results and another database cannot pass.
  The verifier checks current owner and configured predecessors against the
  caller's declared writer inputs; those inputs still require deployment-config
  provenance in the production writer. This is source observation only. It
  does not establish inherited debit, ordered resets, session reuse or import
  authority.
- Checkpoint 68 derives review owner predecessors from the exact repoint
  configuration helper used by daemon boot's owner adoption. An app-level
  source verifier validates the deployment snapshot, excludes the current and
  implicit owner from the configured predecessor tuple, then calls checkpoint
  67's barrier-held SQL verifier. A prior run and a repository-alias predecessor
  cannot disappear from the complete inventory by passing caller-selected
  owner IDs. This closes configuration provenance for the source check only;
  historical debit, reset and session authority still require proof before a
  production import writer can commit.
- Checkpoint 69 adds pure whole-set attribution of abandoned owner runs and
  configured-root counter files to each currently open PR's signed keys.
  Unattributed abandonments, a positive issue key shared by open PRs, and a
  death whose key now selects another PR block. Closed-PR deaths remain
  unassigned evidence. The current-owner count includes only that owner's
  abandoned runs plus assigned files; predecessor-owner deaths remain separate
  because the live SQL reader selects one owner. This is an observational
  prerequisite, not proof of historical file resets, predecessor adoption,
  session reuse or an inherited execution debit.
- Checkpoint 70 joins that attribution into the barrier-held SQL source
  revalidation. The verifier now blocks an unattributed abandonment before it
  returns, and its minted source result binds the attribution to the exact
  revalidated run inventory and held counter mapping. It retains current-owner,
  predecessor and unassigned deaths distinctly. The result is still an
  observation; a future writer must prove resets and inherited debit before
  committing import authority.
- Checkpoint 71 scopes current review file attribution to the physical
  configured review directory while retaining every configured and declared
  historical root in the revalidated audit. Fix and steward files may have the
  same signed key as review files without adding review deaths. The app source
  verifier binds those other-role exemptions and explicit extra roots to
  deployment configuration. A foreign root with a counter, session or round
  source for an open PR blocks until producer provenance is established;
  arbitrary root labels cannot bypass that check. Direct session planning also
  blocks duplicate PR sources across roots. This prevents an old review root
  from silently zeroing an active value, but does not prove historical resets,
  session producers or debit authority.
- Checkpoint 72 writes new generation-boundary receipts with the accepted
  transition's SHA-256 digest instead of copying its growing workflow snapshot.
  Canonical reads and activation source matchers verify the digest against the
  exact retained accepted row; older full-payload receipts remain readable.
  This reduces duplicate stored payloads and prepares a compact transition
  proof. The hot read still decodes each accepted snapshot, so it is not yet
  bounded by the active suffix and compaction remains unimplemented.
- Design checkpoint 73 fixes the compaction rollout order: a source-backed
  checkpoint writer cannot run before the typed hot reader and durable paged
  audit gate are installed. The checkpoint manifest binds contiguous retired
  round and generation source streams; deterministic generation receipt IDs
  and the episode/sequence round index permit bounded audit pages. An overdue
  audit or discrepancy blocks new execution authority, while terminal
  settlement remains available. Open-prefix byte exhaustion must refuse an
  advance before writing any partial receipt. This is a reviewed contract,
  not a production writer or bounded hot read.
- Checkpoint 74 derives a generation receipt's deterministic ID from its
  execution episode and destination generation, and reads expected retained
  receipt/accepted-transition pairs in pages of at most 16 and 8 MiB of
  source fields. A missing, changed, malformed or foreign-episode source
  blocks the page; older full-payload receipts remain observable. The page
  preflights row sizes before decoding and never changes SQL. It is only a
  source observation: it does not prove checkpoint ancestry, accepted
  transition semantics, chronology or absence of unclaimed rows. The future
  archive audit must add those checks and a durable cursor before it can gate
  execution authority.
- Checkpoint 75 reuses the accepted-transition verifier for each archived
  source pair. A page now checks canonical accepted contents, command and audit
  identity, valid claim times, contiguous generation fences and nondecreasing
  revision versions. Following pages require an explicitly supplied prior
  boundary; a skipped generation blocks. The predecessor is caller-held
  observation, so this still does not prove checkpoint ancestry, replay a
  retired prefix, find unclaimed rows or authorize execution. Historical
  snapshot decoding also remains outside the bounded hot-read target.
- Checkpoint 76 adds a read-only sweep over the existing generation-receipt
  namespace index. It pages up to 16 `(scope,key)` rows and 8 MiB of source
  fields, with a fixed upper key for continuation and byte preflight before
  decoding. It rejects BLOB namespace/scope/key aliases, noncanonical scope,
  malformed payloads, changed derived receipt IDs and invalid claim times,
  then returns only rows for the requested execution episode. The sweep spans
  other repositories because the episode is inside the hashed-key payload;
  malformed foreign rows conservatively block it. Caller-supplied cursors and
  observed rows still have no authority. A durable audit must persist its own
  cursor and reconcile the sweep with every expected generation pair to find
  unclaimed or missing sources before admission can depend on it.
- Checkpoint 77 mints a typed, fixed-size cause proof with each newly accepted
  generation transition in the writer's existing SQL transaction. It binds
  the boundary, accepted namespace and key, accepted payload digest, and
  canonical audit digest. Request and continue causes must name the matching
  request and prior revision; release causes must be observed transitions.
  Archive and canonical readers verify the digest against the retained
  accepted audit. Earlier digest-only and full-payload receipt variants stay
  readable. A forged cause digest blocks both readers even when the accepted
  transition bytes are unchanged. The proof stores no preflight or accepted
  snapshot, so a large operator audit cannot enlarge this receipt. It is a
  prerequisite for bounded hot reads, not a source-audit or compaction gate.
- Checkpoint 78 compares each bounded sweep page with a supplied episode
  generation frontier. The pure classifier rejects same-episode receipts
  before the supplied origin generation or after the supplied current
  generation. The adapter rereads the namespace page in the current SQL
  transaction and returns a typed blocker for an extra valid receipt; the
  expected-generation archive pages separately find missing receipts. A
  real two-advance history passes with its full frontier and blocks with a
  frontier ending at the first advance. The frontier and page cursor remain
  caller-supplied observations. Checkpoint ancestry and a durable audit-owned
  cursor must bind them before execution admission can depend on this check.
- Checkpoint 79 persists an expected-generation archive scan in `level_state`
  under the workflow episode. Each write transaction checks at most 16
  retained generation source pairs, folds their identities and digests, and
  saves a small predecessor cursor or an exact discrepancy. A completed pass
  restarts at the origin when due or after the frontier advances. An incomplete
  pass finishes its captured frontier when new generations arrive, preserving
  progress. Every call rechecks the bounded origin source row, including calls
  before the next due time; deletion or mutation persists a blocker. The scan
  reruns expected sources on its next pass; checkpoint 80 adds the missing
  comparison with the prior completed pass. This is an offline observational
  cursor only: it
  does not sweep unclaimed receipt rows, verify checkpoint ancestry or retired
  rounds, prove an atomic lifetime snapshot across pages, or gate admission.
- Checkpoint 80 joins a durable, key-ordered namespace census to that cursor.
  It stores the fixed upper key and one bounded page position per transaction,
  checks every retained generation receipt's source identity, and compares a
  count and order-independent receipt-set digest with the expected generation
  pass. Missing tail rows, changed receipt metadata, malformed or unclaimed
  rows and a retreating indexed frontier persist typed discrepancies. New
  generations during either phase leave the captured pass intact; the next
  pass covers them. The completed pass retains a generation-ordered digest as
  a baseline. Rechecking the prior frontier detects a well-formed receipt or
  accepted-transition mutation after its first checked page, even when the
  current frontier has grown. The due deadline remains overdue until the
  entire census catches up. The cursor remains an offline observation: it
  does not prove checkpoint ancestry, retired rounds, an atomic lifetime
  snapshot across transactions or admission authority.
- Checkpoint 81 adds a typed, fixed-size checkpoint receipt model binding the
  origin, previous and new frontiers, ordered retired round and generation
  source digests, and final source fingerprints. The pure builder checks the
  prefix plan and exact source order. The canonical loader now checks the
  receipt's scoped, canonical, digest-derived identity and one predecessor
  link against the retained origin; a forged origin, missing predecessor,
  changed frontier, alias or oversized row blocks. Because this still does
  not verify the immediate retained source anchors or the archived audit
  gate, every noninitial checkpoint deliberately refuses a canonical load.
  The old lifetime round count is replaced with an indexed high-water read;
  current suffix rows remain checked in sequence within their limit. The
  production writer, source anchor verifier and audit gate remain required
  before compaction can be used.
- Checkpoint 82 point-checks the final retired generation source pair named by
  a typed checkpoint receipt. It derives the generation receipt key from the
  execution episode, bounds both retained rows before reading, rejects TEXT
  and BLOB identity aliases, compares every stored field digest, verifies the
  boundary cursor and accepted-row payload binding, and parses both claim
  times. The check does not decode the accepted historical snapshot; full
  transition semantics and older source membership remain for the paged audit.
  Canonical loads invoke this check before their standing refusal of all
  compacted components. A real accepted-generation fixture covers that call
  path and detects changed source metadata; direct tests cover foreign
  episodes, missing and aliased rows, malformed claim time and pair byte cap.
  The remaining round anchor and audit gate still prevent compaction use.
- Checkpoint 83 adds a bounded indexed point read for the final retired round
  row. It refuses a missing or BLOB-aliased round ID, checks every field's
  SQLite type and byte limit before materializing the row, validates the
  canonical plan and exact episode/sequence identity, and derives the same
  row digest as the archived round inventory. Canonical loads compare that
  digest to the checkpoint's final round fingerprint. Tests cover a changed
  planning row, missing and aliased IDs, oversized plan and foreign episode.
  The receipt-set digest for that round is still unverified, and the canonical
  loader still refuses every compacted component until it and the paged audit
  gate are installed.
- Checkpoint 84 reconstructs the final retired round's exact bounded receipt
  membership from its canonical plan and finalized closure. It binds preflight,
  stages, submission registrations and indexes, applicable planning seal or
  abandonment, outcome, deferred preparation and one productive settlement.
  It rejects extra stage/index keys, fixed-key contradictions, TEXT/BLOB
  aliases, oversized fields and a receipt set above 32 MiB before hashing
  ordered `(namespace, key, all-field digest)` members. Canonical loads compare
  this digest to the checkpoint's final round fingerprint, then retain the
  global refusal because the writer and paged audit gate are pending. Tests
  cover source mutation and removal, aliases, extra and oversized keys,
  productive settlement cardinality and abandonment. The point reader binds
  source fields; semantic verification of every member remains a writer and
  archived-audit obligation.

Independent Sol review of checkpoint 84 identified an unbounded prefixed-key
fetch; the length-first revision and a large-key regression passed re-review
with no remaining medium or high finding. Final `make test` passed: 3,280
domain, 3,250 adapter and 2,876 app tests, plus hooks, governor, gate parity,
four import contracts and both Pyrefly checks. Ten strict xfails remained
expected. Log: `/tmp/phase4-checkpoint-round-receipts-gate.log` (ephemeral).

- Checkpoint 85 adds an indexed, transaction-local reader for an expected
  finalized round sequence and a two-round archive page over a contiguous
  range. It preflights the round ID and outcome before decoding, verifies the
  outcome's episode/sequence identity, and obtains the same bounded row and
  receipt-set fingerprint used by the hot checkpoint anchor. A missing,
  malformed or aliased sequence blocks the page. The lookup includes canonical
  BLOB aliases of the repository, target number, epoch, episode and sequence;
  SQL query-plan inspection confirms the full episode index is used. Tests
  cover a gap, open or oversized outcome, foreign binding, changed outcome,
  BLOB row/episode identity and duplicate BLOB sequence/epoch rows. This is a
  source observation, not a complete census: noncanonical numeric BLOB
  spellings and unrelated unclaimed rows still require the archive sweep.

Independent Sol review of checkpoint 85 reproduced a duplicate BLOB sequence
and epoch gap; the alias-inclusive indexed query and regression tests passed
re-review with no remaining medium or high finding. Final `make test` passed:
3,280 domain, 3,263 adapter and 2,876 app tests, plus hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected. Log: `/tmp/phase4-checkpoint-round-archive-page-gate.log`
(ephemeral).

- Checkpoint 86 adds a read-only transaction-local checkpoint manifest
  observation. It starts from the canonical active execution, replays a
  finalized prefix, reads expected retired round and generation sources in
  order, compares their full outcomes and boundaries with the plan, and
  constructs the typed source-digest receipt. A retained-field byte ceiling
  rejects oversized proposals; it is an accounting ceiling, not a bound on
  the existing canonical loader's reads or peak memory. Missing, aliased and
  changed round receipt sources block. This proposal grants no compaction
  authority: unclaimed archived rows, retired receipt payload semantics and
  the durable audit gate remain unproved. The canonical loader still refuses
  every compacted component.

Independent Sol review found no replay-order error and identified the
accounting limit and missing archive census as explicit constraints. The
reader documents them; malformed-source and byte-limit regressions pass.
Full-gate results are recorded in the progress board after acceptance.

- Checkpoint 87 adds schema v26's repository index and a read-only,
  transaction-local round archive sweep page. Each page fixes an upper rowid
  and reads at most 16 repository rows and 16 MiB of retained fields through
  that index. It validates every selected row's canonical plan, planning,
  closure and derived outcome before selecting the requested execution
  episode. Thus a SQL row rekeyed to another episode while its payload still
  claims this one blocks instead of disappearing. A BLOB repository alias,
  noncanonical numeric BLOB, malformed row or oversized source blocks.
  Negative rowids and genuine other-episode rows remain visible in sweep
  progress. A v25-to-v26 migration fixture retains existing rounds. This
  page is an observation only: no caller yet completes a pass, compares its
  matching set with expected sources, or grants checkpoint authority.

Independent Sol review found and verified the fix for a rekeyed-row omission
and a foreign-episode finalized-payload gap. The full-gate result is recorded
in the progress board after acceptance.
The sweep metric is rows and latency per page as repository round history
grows; its guardrail is refusal on rekeyed or malformed rows. The indexed
query plan is checked now, and a history-size latency comparison belongs in
the complete audit benchmark.

- Checkpoint 88 adds a durable, observational round archive census. One
  transaction reads one bounded repository sweep page and persists its fixed
  rowid frontier, cursor, expected sequence count and source membership in
  `level_state`. An empty final page detects a deleted tail. Completed passes
  retain a prior-prefix membership digest, so a scheduled pass or frontier
  growth detects a changed earlier source. Origin source drift, malformed
  rows and missing rows persist a blocked state. A Sol adversarial review
  found no correctness defect within this observational scope. The census
  does not match rows to checkpoint ancestry, verify retired receipt payloads
  or grant compaction and admission authority. Full-gate results are recorded
  in the progress board after acceptance.

- Checkpoint 89 adds a read-only, indexed receipt sweep page for all twelve
  namespaces in the round receipt-set definition. A fixed `(namespace,
  scope, key)` upper cursor bounds a pass; each page preflights at most 16
  retained rows and 16 MiB of source fields, then records source digests.
  Pages include foreign repositories so malformed and aliased source
  identities cannot hide behind an episode filter. The page does not yet
  interpret receipt payloads, persist a complete census or join members to
  expected retired rounds. Sol review caught a query that rescanned earlier
  keys; per-namespace index seeks and a late-cursor work regression replaced
  it. Full-gate results are recorded in the progress board after acceptance.

- Checkpoint 90 exposes the finalized-round reader's exact bounded receipt
  members as sorted namespace/key/source-digest values, checked against its
  existing aggregate fingerprint. A real finalized-round fixture matches
  those members to the indexed raw sweep and detects changed retained source
  fields. Sol review found no correctness or authority regression. This is
  the expected-member side of a future join, not a complete receipt census or
  namespace-specific payload proof. Full-gate results are recorded in the
  progress board after acceptance.

- Checkpoint 91 decodes each indexed receipt page through the closed workflow
  codec, requires permanent aware claims and canonical payloads, and classifies
  all twelve namespaces into typed bound-round, named-round or command
  references. It checks local payload shape, derived stage and submission-index
  keys, source repository where the payload carries one, and facts-derived
  outcome and stale-productivity dispositions. Historical two-field stale
  receipts block because they lack the generation crossing witness. Malformed rows block with a
  source rowid; the page remains read-only. This is local attribution only:
  named productivity receipts, command markers, claim provenance,
  checkpoint ancestry and the full namespace census still need source-backed
  joins. Sol review found stale-outcome, missing-witness and embedded-repository
  gaps; all have focused regressions. Full-gate results are recorded in the progress
  board after acceptance.

- Checkpoint 92 reuses the same classifier for one finalized round's exact
  bounded receipt members. It first reads the canonical round and its member
  digests, re-reads each member under the same SQL transaction, checks its
  digest and payload, and requires every embedded round reference to name the
  requested round. A real copied round and a generation-witnessed stale round
  pass; malformed and historical unwitnessed payloads block. This point read
  is observational. It does not detect unclaimed namespace rows, prove external
  command or claim ancestry, or authorize a compacted load. Full-gate results
  are recorded in the progress board after acceptance.

- Checkpoint 93 joins one classified namespace source to the exact finalized
  round member that claims its namespace, key and full source digest. Bound
  references use their embedded round, named receipts resolve through a
  bounded indexed round-plan lookup, and recorded-review command markers use
  their permanent submission registration. Missing or BLOB-aliased round rows,
  missing or changed registrations, changed source fields, and unclaimed
  members block. A real copied round's full retained set and a named ceiling
  receipt pass. This remains an observational one-source join: an open round
  has no finalized member set, and a complete namespace pass, checkpoint
  ancestry, command effects and admission authority remain pending. Full-gate
  results are recorded in the progress board after acceptance.

- Checkpoint 94 (`b55803e`) retains a distinct typed pending owner for a
  receipt in an unfinished round. It rechecks the exact indexed round row and
  swept receipt digest, requires the receipt to be planned by that open round,
  and does not grant finalized membership. A contradictory non-complete seal
  is blocked. Sol review found that seal edge; its regression and the full
  gate passed: 3,291 domain, 3,332 adapter and 2,876 app tests, four import
  contracts and Pyrefly.
- Checkpoint 95 (`abd4995`) lets the indexed round and receipt sweeps take a
  caller-selected one-to-sixteen source limit. A durable audit can now use one
  source per transaction while retaining the same fixed upper cursor,
  lookahead, malformed-source checks and byte limits. Sol review found no
  issue. The full gate passed: 3,291 domain, 3,334 adapter and 2,876 app
  tests, four import contracts and Pyrefly. Neither commit changes live
  authority, activates a family or permits a compacted load.
- Checkpoint 96 adds a persisted, observational receipt census for the
  contiguous finalized prefix of one execution episode. It advances one
  expected round or one indexed namespace source per transaction. The
  expected pass checks each finalized round's exact retained member payloads;
  the global namespace pass resolves every source to a finalized owner or a
  typed pending open owner, then compares the finalized member count and
  fingerprint. Later passes recheck the prior prefix. Missing, changed,
  aliased and orphaned sources block; a round that closes during the sweep is
  deferred to the next pass. The source-read ceiling is a conservative
  roughly 157 MiB derivation from nested reader limits, not a runtime byte
  meter. This census has no checkpoint ancestry or admission authority.
  Focused tests and independent Sol review found no remaining medium or high
  issue after malformed-frontier persistence and cumulative-bound fixes. The
  full gate passed: 3,296 domain, 3,347 adapter and 2,876 app tests, four
  import contracts and both Pyrefly checks.
- Checkpoint 97 makes that census's ordered round-source digest use the same
  seed and per-round encoding as the existing checkpoint receipt builder.
  Each expected round first verifies the canonical exact member set's own
  receipt-set SHA-256. A real finalized round's audit digest now equals a
  transaction-local initial checkpoint proposal's round-stream digest. The
  audit cursor moved to a versioned `-v2` namespace so existing observational
  rows remain intact while the new format starts a fresh pass; no old hash is
  silently resumed. Independent Sol review verified this after finding the
  initial cursor incompatibility. A later checkpoint still needs a segmented
  source pass and ancestry proof; this comparison alone grants no authority.
  The full gate passed: 3,296 domain, 3,348 adapter and 2,876 app tests,
  four import contracts and both Pyrefly checks.
- Checkpoint 98 adds a real-source contract regression for the existing
  generation archive scan: its completed ordered stream digest equals the
  same initial checkpoint proposal's generation-stream digest. Both stream
  encodings now have direct checkpoint comparisons. This is test evidence,
  not a persisted checkpoint-chain join or authority to compact. The full
  gate passed: 3,296 domain, 3,349 adapter and 2,876 app tests, four import
  contracts and both Pyrefly checks.
- Checkpoint 99 adds a bounded, read-only checkpoint predecessor page. From
  the current component's checkpoint tip it follows one to sixteen typed
  receipt links toward the exact origin checkpoint, checking each retained
  receipt's canonical identity, previous frontier, claim order and six-field
  source digest. A changed claim spelling changes the source digest even if
  its parsed instant is equal. Missing or aliased predecessors block. The
  caller-held page cursor is not proof of the path already traversed; a
  durable audit must own and revalidate it before any admission decision.
  Sol review found no remaining medium or high issue. Compacted loads still
  refuse and no writer uses these pages. The full gate passed: 3,296 domain,
  3,357 adapter and 2,876 app tests, four import contracts and both Pyrefly
  checks.
- Checkpoint 100 persists an observational checkpoint archive cursor by
  execution episode. One transaction checks a predecessor receipt or one
  expected retired round or accepted generation source, matching the ordered
  stream digests in each receipt and following the chain to the exact origin.
  A completed pass stores a chain digest for its next due pass; adding a new
  tip preserves the old chain as a tail baseline. Missing or changed sources
  persist a blocker. This is not an admission gate: it does not replay retired
  outcomes against checkpoint state or census unclaimed archive rows and
  receipt references. Canonical compacted loads still refuse. Independent Sol
  review found no cursor regression and confirmed those two remaining
  authority gaps. The full repository gate passed: 3,296 domain, 3,364
  adapter and 2,876 app tests, four import contracts and both Pyrefly checks.
- Checkpoint 101 attempted sequential state replay for round-only checkpoint
  segments. A forged checkpoint with matching source digests reproduced the
  missing state check, and a versioned cursor resolved a persisted-schema
  incompatibility found by Sol review. The full gate passed: 3,296 domain,
  3,366 adapter and 2,876 app tests, four import contracts and Pyrefly. This
  checkpoint was subsequently withdrawn: the replay rule was not valid for
  every compactable prefix.
- Checkpoint 102 withdraws that replay rule. A valid partial compaction can
  retire an earlier finalized round that a later generation boundary leaves
  unapplied. Applying every round falsely blocks that checkpoint. Skipping
  replay based on the live generation also misses previously applied rounds;
  Sol review found that the audit result would depend on scan timing. The
  checkpoint receipt does not establish whether the later boundary existed
  when the checkpoint was written. The restored observational scan still
  checks retired source streams and ancestry, but makes no checkpoint-state
  proof. Full boundary applicability and checkpoint timing must be bound in
  source evidence before replay or admission can use this result. Sol
  independently verified the false-block and timing issue are absent after
  withdrawal. The full gate passed: 3,296 domain, 3,365 adapter and 2,876 app
  tests, four import contracts and both Pyrefly checks.
- Checkpoint 103 adds a typed, transaction-local applicability proposal for
  the checkpoint writer. The compaction plan selects either all tail outcomes
  after its last retired boundary or the first remaining accepted generation
  boundary's applied-sequence set. The manifest reads that exact following
  boundary pair under its SQL snapshot, records its source fingerprints and
  applied-sequence digest, and charges the pair to its source-byte ceiling.
  A real late-settlement partial compaction exercises the selected path;
  missing or malformed retained sources block, and an equivalent claim-time
  spelling change changes the captured fingerprint. The proposal is neither
  a durable sidecar nor proof of checkpoint state. The writer must mint it
  atomically with a checkpoint, and the paged audit must re-read and replay it
  before an admission gate can use it. Independent Sol review found no
  remaining material issue. The full gate passed: 3,297 domain, 3,368
  adapter and 2,876 app tests, four import contracts and both Pyrefly checks.
- Checkpoint 104 adds a pure replay check for a complete archived checkpoint
  segment. It requires every retired outcome and boundary in order, verifies
  the recorded tail selection against the first following boundary source,
  then compares the projected checkpoint state. It accepts a following
  boundary created after the captured revision and rejects changed source
  fingerprints, applied sequences, missing outcomes and forged checkpoint
  state. The function relies on the caller to establish exact source
  membership and boundary absence; the current archive scan does not yet
  collect those facts for state replay. Independent Sol review found no
  concrete issue. The full gate passed: 3,297 domain, 3,368 adapter and
  2,876 app tests, four import contracts and both Pyrefly checks.
- Checkpoint 105 defines a compact application receipt keyed by the checkpoint
  receipt identity, carrying captured revision, tail start and typed choice.
  Its bounded SQL reader pairs the permanent sidecar with the exact canonical
  checkpoint receipt, checks equal claim instants, and fingerprints all six
  retained source fields. Real selected and all-tail manifests round-trip;
  absent, aliased, malformed, oversized, reusable, foreign and mismatched
  claims block. This does not prove atomic minting or archive membership. No
  production writer or archived audit uses the record yet. Sol review found
  that a copied checkpoint and sidecar pair could be accepted under a foreign
  repository scope; the embedded repository check and real copied-pair
  regression close that gap. The full gate passed: 3,297 domain, 3,380
  adapter and 2,876 app tests, four import contracts and both Pyrefly checks.
- Checkpoint 106 makes the durable observational checkpoint scan require and
  recheck each exact application source while following retired round and
  generation streams. The six-field application digest enters the chain
  baseline, so later source changes block the next pass. A versioned cursor
  restarts valid prior scans at the origin and carries prior blocked or
  malformed cursors into a persisted blocker. A prior completed chain digest
  is not comparable with the new encoding, so continuity across the upgrade
  is not yet certified. The selected following
  boundary and checkpoint state still need archived replay; this remains
  outside admission authority. Sol review found two migration gaps; both
  are fixed with regressions. The full gate passed: 3,297 domain, 3,389
  adapter and 2,876 app tests, four import contracts and both Pyrefly checks.
- Checkpoint 107 makes each active checkpoint scan step read the exact first
  following accepted generation from retained SQL when one exists. The pure
  application rule compares its receipt and accepted-source fingerprints,
  applied sequences and capture revision with the recorded tail choice. A
  selected choice without that source blocks; an all-tail choice blocks if a
  following boundary had already been captured. A real partial-compaction
  fixture passes; absent and changed following sources persist blockers. This
  checks applicability only: retired outcome state replay, complete source
  membership and admission authority remain open. Independent Sol review
  found no medium or high issue in this slice. The full gate passed: 3,297
  domain, 3,391 adapter and 2,876 app tests, four import contracts and both
  Pyrefly checks. Log: `/tmp/checkpoint-following-source-gate.log` (ephemeral).
- Checkpoint 108 adds an immutable one-source-at-a-time checkpoint state
  reducer and a transaction-local SQL observer for one compacted segment. It
  reads the retained first following source, retired generation pairs and
  classified round source sets, checks both ordered stream digests, and
  compares the projected checkpoint state. Real selected-tail, round-only and
  mixed round/generation fixtures pass; malformed source and source-byte
  ceiling cases block. The reducer's intermediate states stay private and
  in-memory; SQLite's round-id primary key supplies uniqueness for actual
  archive rows. Sol reviews found no remaining medium or high issue within
  this observational scope. This is not a durable paged audit, true cumulative
  SQL read bound, source census, chain proof or admission gate. The full gate
  passed: 3,302 domain, 3,395 adapter and 2,876 app tests, four import
  contracts and both Pyrefly checks. Log:
  `/tmp/checkpoint-state-replay-final-gate.log` (ephemeral).
- Checkpoint 109 moves the replay into the durable checkpoint archive scan.
  The `-v3` cursor stores its generation, round and tail frontier, advances by
  one new retired source per transaction, and checks the projected checkpoint state
  before completing each chain segment. It rechecks the checkpoint receipt,
  application sidecar and first following generation on resume. A pending
  boundary stores only its source fingerprint and is re-read at each step;
  cursor storage excess persists a fixed-size blocker anchor in initial,
  resumed and discrepancy paths. Valid `-v2`
  cursors restart at the origin; blocked and malformed prior cursors remain
  blockers. The harness-owned cursor's accumulated state is trusted between
  transactions, so future admission authority must settle that trust boundary.
  This remains observational: a complete retired member payload and
  unclaimed source census, atomic writer, cumulative hot-read bound and
  admission gate are still required. Independent Sol review found no
  remaining confirmed medium or high issue. The full gate passed: 3,302
  domain, 3,402 adapter and 2,876 app tests, four import contracts and both
  Pyrefly checks. Log: `/tmp/checkpoint-paged-replay-final-gate.log`
  (ephemeral).
- Checkpoint 110 joins the four completed archive observations for one current
  execution. A read-only adapter checks the checkpoint tip and immediate
  anchor, exact origin source, round-row and generation frontiers, receipt
  prefix covering the compacted checkpoint, and all pass deadlines. The
  round-row and generation audit bootstraps now use the bounded indexed
  execution reader because the canonical loader intentionally refuses
  compacted authority before the gate exists. Real round-only, retired
  generation and selected-tail fixtures pass. The join remains observational:
  independently timed passes are not one atomic snapshot, and the active
  suffix, atomic writer and admission gate remain open. Independent Sol review
  found no concrete medium or high issue. The full gate passed: 3,302 domain,
  3,410 adapter and 2,876 app tests, four import contracts and both Pyrefly
  checks. Log: `/tmp/archive-evidence-full-gate.log` (ephemeral).

The next seam is durable checkpoint authority. The writer must mint the
compact application receipt from its transaction-local proposal with the
checkpoint. It must verify the full retired stream using the identical round
receipt-set definition, bind the ordered source digests, and publish the
aggregate and checkpoint atomically.
The paged audit now carries state replay across bounded transactions, and a
read-only join aligns its completed result with the separate source censuses.
The writer must still revalidate its transaction-local sources, publish the
checkpoint atomically, and supply the admission gate. Keep the canonical
compaction refusal until these checks are complete.

With checkpoints 88–99, the bounded round and receipt cursors can reconcile
the finalized prefix against the complete retained receipt namespace. Valid
open-round receipts remain pending. Next, bind the retired member stream to
checkpoint ancestry. The census does not prove missing open receipts,
external command, effect, claim or accepted-transition evidence, or the
checkpoint lineage. Before a writer can use the checkpoint-86
proposal, establish a true cumulative read bound around canonical load and
each source reader; prove generation-only and later-checkpoint paths; verify
every retired receipt payload; and publish the checkpoint and initialized
audit gate atomically.

The intended read metric is bytes and latency per canonical load as archived
generations grow; the guardrail is refusal on changed immediate sources. The
next measurement is the planned >4,096-boundary stress and latency comparison
once the round anchor, audit gate and writer are available.

Independent Sol review found no medium or high issue after the canonical-load
integration fixture was added. Full `make test` passed: 3,280 domain, 3,237
adapter and 2,876 app tests, plus hooks, governor, gate parity, four import
contracts and both Pyrefly checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-generation-checkpoint-anchor-gate.log` (ephemeral). All four
watched services were inactive; no live authority was activated.

Independent Sol review of checkpoint 81 caught the first reader accepting
fabricated anchor digests and retaining lifetime prefix scans. The revised
reader refuses all compacted components and uses the indexed high-water; its
second review found no remaining medium or high issue in this limited slice.
Focused domain and persistence tests pass. Final `make test` passed: 3,280
domain, 3,231 adapter and 2,876 app tests, plus hooks, governor, gate parity,
four import contracts and both Pyrefly checks. Ten strict xfails remained
expected. The log is `/tmp/phase4-checkpoint-receipt-final-gate.log`
(ephemeral). All four watched services were inactive; no live service or
workflow family was activated.

Independent Sol review of checkpoint 80 found no remaining concrete issue
in the reviewed slice. A separate self-review found and fixed the missing
cross-pass digest comparison, with regressions for source mutation after a
sweep page and after generation growth. The terminal empty-page and frontier
retreat cases also have behavioral regressions. Focused tests and the full
`make test` gate passed: 3,279 domain, 3,220 adapter and 2,876 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-durable-generation-sweep-gate.log` (ephemeral). No live service
or workflow family was activated. Next: bind checkpoint ancestry and retired
round source proof before using the audit for admission or compaction.

Independent Sol review of checkpoint 79 found two issues: a resumed cursor
did not recheck the origin row, and a growing generation frontier restarted
an incomplete pass. Both were fixed with behavioral regressions; the reviewer
found no further medium or high severity issue in this slice. Focused tests
and the full `make test` gate passed: 3,278 domain, 3,212 adapter and 2,876
app tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-durable-generation-archive-scan-gate.log` (ephemeral). No live
service or workflow family was activated. Next: join a durable namespace
sweep and checkpoint ancestry to the audit cursor before using its result for
admission or compaction.

Independent Sol review of checkpoint 78 found no medium or high severity
issue. A lower-bound case was added to the pure test after review. Focused
tests and the full `make test` gate passed: 3,277 domain, 3,204 adapter and
2,876 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-generation-frontier-reconciliation-gate.log` (ephemeral). No
live service or workflow family was activated.

Independent Sol review of checkpoint 77 found no medium or high severity
issue in the fixed-size proof, writer or readers. Focused tests and the full
`make test` gate passed: 3,276 domain, 3,203 adapter and 2,876 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-small-transition-proof-gate.log` (ephemeral). No live service
or workflow family was activated. The intended efficiency metric is bytes
decoded per canonical hot read; the guardrail is exact source authenticity.
The next measurement is the compaction contract's 10/1,000/5,000-transition
benchmark after the audited bounded reader is installed.

Independent Sol review of checkpoint 76 found no concrete medium or high
issue. The final focused sweep and archive tests passed (12 cases), including
aliases, oversized payloads and an insert detected on the next pass. The full
`make test` gate passed: 3,275 domain, 3,201 adapter and 2,876 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-generation-receipt-sweep-gate.log` (ephemeral). No live service
or workflow family was activated.

Independent Sol review of checkpoint 75 found no concrete medium or high
issue in this observational slice. Prefix comparison cannot be required
between accepted snapshots because a valid compaction may retire earlier
boundaries; the checkpoint audit must establish that ancestry. The full
`make test` gate passed: 3,275 domain, 3,191 adapter and 2,876 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-generation-archive-semantics-gate.log` (ephemeral). No live
service or workflow family was activated.

Independent Sol review of checkpoint 74 caught the original post-decode byte
cap and missing malformed/foreign edge coverage. Size preflight and regressions
now cover both; its final pass found no remaining medium or high issue in this
observational slice. The full `make test` gate passed: 3,275 domain, 3,191
adapter and 2,876 app tests, plus hooks, governor, gate parity, four import
contracts and both Pyrefly checks. Ten strict xfails remained expected. The
log is `/tmp/phase4-generation-archive-page-gate.log` (ephemeral). No live
service or workflow family was activated.

Independent Sol review of design checkpoint 73 identified an unsafe initial
rollout order, an omitted open-prefix codec refusal and an incomplete authority
gate. The corrected contract requires the reader and audit before compaction,
specifies bounded page membership checks and source identity derivation, and
blocks generation advance, renewal and compaction when audit is overdue or
discrepant. The reviewer's final pass found no remaining medium or high design
issue. The full `make test` gate passed: 3,274 domain, 3,189 adapter and 2,876
app tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-compaction-manifest-design-gate.log` (ephemeral).

Independent Sol review of checkpoint 72 identified the remaining hot-read
cost: each active generation boundary still reloads the full historical
accepted snapshot, which embeds earlier boundaries. The reviewer classified
this as an outstanding compaction-contract gap rather than a new regression.
The new receipt is source-bound, but the production compacted checkpoint,
small hot-read verification path and paged archived audit must land together
before claiming bounded reads. Focused regressions cover digest mismatch,
semantic mutation even with a matching digest, and legacy receipt reads.
The full `make test` gate passed: 3,274 domain, 3,189 adapter and 2,876 app
tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-generation-digest-gate.log` (ephemeral). No live service or
workflow family was activated.

Independent Sol review found and verified fixes for two gaps in checkpoint 71:
the same signed key may legitimately exist in fix and review roots, and
caller-supplied labels cannot establish historical root ownership. Focused
regressions cover both, duplicated sessions, explicit extra roots, and a
substituted deployment root. The full `make test` gate passed: 3,273 domain,
3,187 adapter and 2,876 app tests, plus hooks, governor, gate parity, four
import contracts and both Pyrefly checks. Ten strict xfails remained expected.
The log is `/tmp/phase4-root-scope-gate.log` (ephemeral). No live service or
workflow family was activated.

Independent Sol review of checkpoint 70 found no concrete issue. Focused
adapter and app tests cover a current-owner run, a retained predecessor, an
opaque abandonment and an attempted substitute attribution. The first full
gate failed after the host filesystem filled (`Errno 28` during pytest cache
write); it was not a code assertion failure. After free space returned, the
unchanged implementation passed the full rerun: 3,270 domain, 3,173 adapter
and 2,872 app tests, hooks, governor, gate parity, four import contracts and
both Pyrefly checks; 10 strict xfails remained expected. The passing log is
`/tmp/phase4-sql-death-join-rerun.log` (ephemeral).

Independent Sol review of checkpoint 69 found that an initial version counted
predecessor-owner abandoned runs in the current owner's live count. The final
variant separates those runs and retains them as explicit historical evidence;
the reviewer found no further concrete issue. Tests cover current and prior
owners, both signed keys, closed PRs, shared and relinked issues, unknown and
foreign dispatch, overflow, forged complete values and source-order stability.
Full `make test` passed: 3,270 domain, 3,171 adapter and 2,872 app tests,
hooks, governor, gate parity, four import contracts and both Pyrefly checks;
10 strict xfails remained expected. The gate log is
`/tmp/phase4-death-attribution-gate.log` (ephemeral).

Independent Sol review of checkpoint 68 found the new app module missing from
the architecture manifest; the contract entry and focused regression were
added. The first full gate then exposed a boot test using the shared owner
helper with a duck-typed configuration. The helper now preserves that boot
contract, while the import wrapper requires a validated `HarnessConfig`.
The final review found no remaining concrete issue. Tests cover a real prior
run, omitted predecessor, repository alias, invalid configuration and existing
repoint adoption/startup behavior. Full `make test` passed: 3,258 domain,
3,171 adapter and 2,872 app tests, hooks, governor, gate parity, four import
contracts and both Pyrefly checks; 10 strict xfails remained expected. The
gate log is `/tmp/phase4-configured-import-owner-gate.log` (ephemeral).

Independent Sol review of checkpoint 67 found scope lifetime, store identity,
owner-input and proof-construction gaps. The final implementation binds the
exact held source object to an active context scope, checks the SQL connection
against the locked store, compares the declared owner inputs, and mints the
revalidated result only after the complete SQL reads agree. The final review
found no remaining medium or high issue in this observational composition.
Focused regressions cover expired/reopened scopes, substituted evidence,
direct construction, another database, owner mismatches and changed source
sets. Full `make test` passed: 3,258 domain, 3,171 adapter and 2,866 app tests,
hooks, governor, gate parity, four import contracts and both Pyrefly checks;
10 strict xfails remained expected. The gate log is
`/tmp/phase4-sql-import-sources-gate.log` (ephemeral).

Independent Sol review of checkpoint 66 found no concrete defect. Focused
tests cover attribution, changed source refusal, all three lock lifetimes and
read-only SQL scope. Full `make test` passed: 3,258 domain, 3,159 adapter and
2,866 app tests, hooks, governor, gate parity, four import contracts and both
Pyrefly checks; 10 strict xfails remained expected. The gate log is
`/tmp/phase4-import-source-scope-gate.log` (ephemeral).

Independent Sol review of checkpoint 65 found missing activation digest and
time bindings, a two-clock freshness race, the activation/read crossing, and
the corresponding hot-read fence. Each was fixed with a behavioral regression.
Canonical readback inside the write transaction also rolls back a preexisting
activation alias. The final review found no remaining concrete issue. Full
`make test` passed: 3,258 domain, 3,154 adapter and 2,866 app tests, hooks,
governor, gate parity, four import contracts and both Pyrefly checks; 10
strict xfails remained expected. The gate log is
`/tmp/phase4-initialized-origin-gate.log` (ephemeral).

Independent Sol review of checkpoint 64 found the stale-handle case in both
multi review and sessionless re-review, plus an initial evidence-ID binding
gap. The implementation was narrowed to an explicit unproven source, and the
second pass found no remaining material issue. The full gate passed: 3,257
domain, 3,134 adapter, 2,866 app, hooks, governor, gate parity, four import
contracts and both Pyrefly checks; 10 strict xfails remained expected. The
gate log is `/tmp/phase4-session-source-gate.log` (ephemeral).

The next refused-ceiling probe confirmed a separate prior-state gap. The
writer durably closes a productive round when a fresh ceiling decision is
refused, but the decision receipt embeds its own prior workflow snapshot.
Replaying policy against that embedded snapshot alone cannot prove it was the
actual durable prior state. An independent Sol review rejected a proposed
classification on this basis; the change was withdrawn. A regression now
asserts that this successful command stays unresolved and prevents complete
ordered coverage. A future proof must join the prior snapshot to durable
workflow lineage, including an activation origin or accepted predecessor
transition, before admitting the refused productive round.
The guard regression and full gate passed: 3,257 domain, 3,135 adapter and
2,866 app tests, hooks, governor, gate parity, four import contracts and both
Pyrefly checks; 10 strict xfails remained expected. The gate log is
`/tmp/phase4-refused-ceiling-guard-gate.log` (ephemeral).

Independent Sol review of checkpoint 63 found a mixed-repository provenance
gap: the listing used the requested repository while PR and issue reads used
the gateway's configured repository. The reader now checks equality before
reading, and the proof digest includes the canonical repository. The second
review pass found no remaining material issue. The full gate log is
`/tmp/phase4-counter-mapping-final-gate.log` (ephemeral).
The gate passed: 3,250 domain, 3,132 adapter and 2,866 app tests, plus
hooks, governor, gate parity, four import contracts and both Pyrefly checks.
Ten strict xfails remained expected. The architecture manifest now names both
new modules; its contract was the only failure in the first gate attempt and
passed after that documentation correction.

Independent Sol review of checkpoint 62 found that the first implementation
enforced row and byte bounds only after accumulating all configured roots.
The scanner now shares one directory-entry and retained-byte budget across
roots and stops on the first blocker. Multi-root overflow regressions and a
second Sol pass found no remaining material issue. The full gate log is
`/tmp/phase4-complete-root-files-gate.log` (ephemeral).
The gate passed: 3,236 domain, 3,120 adapter and 2,866 app tests, plus
hooks, governor, gate parity, four import contracts and both Pyrefly checks.
Ten strict xfails remained expected.

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

1. Call checkpoint 71's configuration-bound source verifier from the
   production import writer inside its acceptance transaction under activation
   barriers, retaining the held source context through commit. Daemon boot's
   `adopt_legacy_owner`
   rewrites some prior run owners, so neither the current owner nor configured
   owners alone establish complete historical run evidence. Caller-held
   complete values are observational until this SQL recheck succeeds.
2. Prove the prior workflow state for refused ceiling decisions from durable
   lineage, then classify those decisions and other unresolved historical
   commands, then replay every eligible older-generation outcome against a
   source-backed execution origin/checkpoint to establish resets. Verify
   delivered escalated holds or their current observed state, and resolve or
   block historical unregistered and old stale submissions. A completed
   agent run is not itself a productive review; unknown death or reset
   attribution blocks import with reviewable evidence.
3. Use the barrier-held file/linkage/counter and SQL source scope in the
   production import writer, preserving the scope through commit.
   The pure mapper retains unassigned counters in the selected review root and
   refuses a positive issue key shared by open PRs. Checkpoint 71 retains
   other-role and historical files in the full root audit; active evidence in
   a foreign root needs producer provenance before import. It retains every
   owner-lineage death in the barrier-held result but has not established
   historical resets or predecessor adoption. The typed session/round/counter parser keeps malformed values and
   mode markers explicit; a syntactically valid session candidate is now an
   explicit unproven source blocker. Even a current reviewed baseline cannot
   certify it because newer legacy reviews could retain an older handle.
4. Build the production import writer from revalidated legacy sources and
   activation decisions, then mint renewed origin variants,
   checkpoint receipts and the archived-source audit required by the
   compaction contract. The new imported-origin hot read is a prerequisite;
   it does not certify synthetic or unverified source digests. The 4,096-round
   and generation-boundary suffix limits remain activation gates until that
   work passes. The initialized-origin path applies only to a PR first
   observed after execution activation.
5. Wire active round identity through production dispatch, child reservation
   and verdict registration; complete composed fault/recovery tests, offline
   shadow, rehearsal and activation. Phase-5 live windows require explicit
   user authorization.

Use Sol or Luna for subagents. Run full `make test` before each coherent
commit. Keep the six-phase progress board updated at accepted checkpoints;
the user explicitly requested it on 2026-09-28.
