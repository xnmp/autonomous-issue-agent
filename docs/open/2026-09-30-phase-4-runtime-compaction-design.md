# Phase 4 — runtime compaction continuation

Date: 2026-09-30 Australia/Sydney. Implementation notes after the offline
checkpoint publication foundation. This is proposed work, not milestone
acceptance. Phase 4 remains **0/7** and the six-phase plan remains **3/6**.

## Implemented foundation

The transaction-local checkpoint writer verifies the current complete source
passes, indexed identities, round claim lifecycle, accounting, command/effect
and accepted transition lineage before publishing checkpoint/application
receipts, the component and its archive certificate together. An internal
savepoint makes failure atomic even when the caller catches the exception
inside its transaction. Exact publication retry is inert; a later audit
detects mutable source discrepancies without erasing the original result.

Canonical compacted reads verify a bounded suffix and immediate source anchors.
New preflight, reservation, launch and generation advances check the archive
admission deadline. Settlement, retained-spend capture and recovery remain
available when admission is held. A paged offline runner recertifies only after
all source families complete; an incomplete page does not renew the deadline.
Identity pages require independently recorded cursor provenance and a fresh
projection cohort after repair. Source/projection mutation witnesses cover
replacement writes and fail closed on missing or overflowing revisions.

The SQL meter bounds cumulative returned source rows and bytes before payload
reads, with restored SQLite row/value limits and nested budgets. This does not
bound all SQLite input scans, temporary work, CPU or peak Python memory. Query
plans and per-page bounds remain part of the contract. The strict path requires
SQLite 3.43 or newer.

## Measured retained identity growth

The composed fixture starts from configured source-revalidated activation,
finalizes a copied production-control round and publishes a real checkpoint.
It then grows unrelated retained identity sources with 1 KiB payloads and
recertifies before measuring the canonical hot read and indexed claim join.

| Retained unrelated identities | Hot rows | Hot bytes | Hot VM steps | Hot wall / CPU seconds | Claim rows | Claim bytes | Claim VM steps | Claim wall / CPU seconds |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 68 | 40,460 | 14,214 | 0.00812 / 0.00792 | 44 | 11,364 | 5,669 | 0.00417 / 0.00405 |
| 1,000 | 68 | 40,460 | 14,214 | 0.00869 / 0.00845 | 44 | 11,404 | 5,669 | 0.00428 / 0.00413 |
| 5,000 | 68 | 40,460 | 14,214 | 0.00974 / 0.00948 | 44 | 11,420 | 5,669 | 0.00579 / 0.00568 |

These are returned SQL source bytes and SQLite progress-handler VM steps;
wall/CPU times are observations, not timing assertions. This fixture proves
bounded selected lookups under unrelated identity growth. It is **not** the
required 5,000-round and generation-history acceptance profile. The metric is
ordinary read work; allowance, session provenance and accounting must remain
unchanged. Cost per merged PR has not been measured for this change.

## Next runtime boundary

1. Add bounded archive maintenance outside ordinary paid admission in the
   review loop, including unchanged-world and held-work paths. Current periodic
   recovery runs every 30 ticks, which is too sparse for long paged audits.
   Preserve recovery/settlement ordering and journal deferrals with a ceiling.
2. Use a durable keyset cursor over active review PRs and a bounded page burst.
   Bootstrap identity, round, receipt, generation, lifecycle and initial
   checkpoint-absence passes before the first checkpoint. The current compacted
   admission runner requires an existing tip and cannot do that bootstrap.
3. Publish maintenance in a separate transaction before deriving a new
   preflight or generation proposal. Compaction advances the workflow revision;
   inserting it after deriving `WorkflowChanged` would invalidate the proposal.
   Operator requests retain their captured revision and conflict rather than
   silently rebasing. A daemon may reload and rederive its own proposal.
4. Select a finalized prefix below both the 4,096 suffix ceiling and the SQL
   byte ceiling. Large plans can reach the payload ceiling first. Do not retire
   an open gap or accept partial source evidence after a budget refusal.
5. Resolve audit convergence under concurrent writes before runtime activation.
   Broad source witnesses currently restart a census on every relevant mutation.
   With 5,000 retained sources and continuing writes, repeated single pages can
   starve. A larger burst or admission hold alone cannot guarantee convergence
   while unrelated settlement continues. Do not hold a long SQLite write
   transaction or suppress money recovery to make the audit finish.
6. Design bounded retention for identity page certifications and orphaned
   projection members. Current append-only page receipts are safe for the
   offline foundation but grow across indefinite periodic scans.

SQLite documents [snapshot isolation](https://www.sqlite.org/isolation.html)
and [trigger semantics](https://www.sqlite.org/lang_createtrigger.html), including
replacement and old/new-row constraints. PostgreSQL describes
[materialized projections](https://www.postgresql.org/docs/current/rules-materializedviews.html).
These guide investigation of a transactionally captured mutation stream and
paged projection maintenance; they do not establish this repository's source
membership or security proof. A replacement must preserve the old identity's
invalidation and the new identity's insertion even when implicit delete
triggers do not run.

## Mutation coverage design from independent Sol investigation

This is a proposed shared seam, not implemented authority:

- Keep global revisions as tamper witnesses. Add a transactionally captured,
  append-only mutation journal with contiguous event sequence, table/rowid,
  bounded old/new identity and join fields, their SQL types and byte lengths.
  An oversized identity becomes an explicit unclassifiable event; recording
  it must not abort settlement. Journal coverage must have no missing events.
- Preserve deleted or rewritten ownership: dedupe namespace/scope/key; round
  repository/target/epoch/episode/sequence; claim repository/role/target/
  occurrence/owner; run/round/episode and run-owned accounting keys; command,
  effect and workflow-evidence identities. Checking only the new row would
  lose the old archived member after a delete or reassignment.
- Mint a reverse source-membership projection from full verified round/claim
  joins, including run and command ownership and checkpoint segment. Classify
  both old and new aliases. Retired-source changes dirty the affected segment;
  verified unrelated and active suffix writes advance the watermark. Unknown
  ownership blocks admission while recovery remains available.
- Fix the audit frontier and each table's rowid upper bound. Retain page cursor
  and consumed mutation sequence. Reconcile bounded mutation pages instead of
  restarting on global changes, including late inserts below an already scanned
  rowid, deletion and rowid reuse. Final certification requires complete baseline,
  current journal watermark and no dirty retired source in one write transaction.
  All five audit families and the completed evidence join need this same seam;
  changing identity scan behavior alone would leave starvation intact.
- Use a stable identity projection epoch plus mutation watermark. Remove stale
  members in bounded pages and retain the current independent cursor receipt.
  Prune journal rows only after every dependent cursor has acknowledged them or
  their effect is preserved in a durable dirty marker. A paused episode must not
  silently lose coverage.
- SQLite's [conflict semantics](https://www.sqlite.org/lang_conflict.html) mean
  implicit replacement deletes are observed only with recursive delete triggers.
  Do not rely on a connection pragma. Collision guards on monitored unique keys
  can reject hidden replacements; nonconflicting inserts remain allowed. Verify
  existing contracts before adopting this change. Current production source
  writers do not use `OR REPLACE`; adversarial witness tests do.

A long-lived WAL snapshot alone cannot prove the final live source set. It
still needs mutation reconciliation. Finite bounded work cannot outpace an
unbounded rate of relevant changes; journal backlog and dirty-segment ceilings
must keep admission fail closed and report a specific blocker.

Independent Luna review identifies two prerequisites before adopting that seam:

1. Define baseline/delta ordering, rather than treating a fixed upper rowid and
   starting watermark as sufficient. Separate page transactions can observe a
   post-start row while its mutation is replayed again. Use per-row observed
   revision or idempotent current-state reconciliation with a proved watermark;
   late low-rowid insertions must also be included.
2. First measure whether precise source witnesses remove unnecessary restarts.
   The lifecycle membership reader scans submission commands/effects, but schema
   32's lifecycle witness also covers accounting, claims and all dedupe payloads.
   A dedicated witness for the exact scanned source, including old/new aliases,
   can preserve census progress during independent settlement. Identity members
   contain only three identity fields per table; payload-only changes do not
   inherently invalidate those fields. Any narrowed witness must match the
   actual read contract and preserve detection of alias insertion, deletion,
   rowid reuse and type changes. Full paid lineage still needs its own mutation
   coverage; simply deleting accounting tables from broad witnesses is unsafe.

The reviewer found no simpler complete substitute for membership ownership and
coverage, but these precise-witness changes may reduce the required delta scope.
Do not relax the completed evidence join until that proof exists.

### Candidate ordering protocol to investigate next

Identity projection is a current membership set, not an ordered outcome fold.
A narrowly scoped identity journal could record only structural identity/type/
rowid changes for its three tables. During baseline and catch-up, reconcile each
affected source rowid against its **current** bounded identity in the same short
transaction: upsert the current cohort member or remove it if absent. Updating
an already present cohort member must not increment membership counts again.
Applying an event for a row not yet scanned must be idempotent when the baseline
later reaches it. A late low-rowid insertion is visited through the journal;
delete/reinsert is reconciled in order. Final completion needs a current journal
watermark and independently recorded projection receipt, not only a cursor.
This is a candidate answer to the double-application race, still requiring tests
and adversarial review. It does not establish semantic archive coverage.

The subsequent Sol design and independent Luna review refine the prerequisites:

- The historical lineage read set includes dedupe, round, claim, run, lease,
  reservation, recovery, charge, limit, quota, cancellation, command, effect and
  workflow-evidence rows. Workflow/component rows carry current tip and chain
  authority. Canonical gates, rearms and activation records are rechecked in the
  current transaction; `retired_owners` belongs to historical import instead.
  The foundation now witnesses every lease write, with a paid-checkpoint
  regression that detects a new retained lease and requires fresh proof after
  removal. Disabling only those triggers makes the regression fail.
- A missing or overflowing journal head must leave a durable uncovered/poisoned
  condition without preventing ordinary settlement. Rejecting every source write
  to preserve journal atomicity would violate the money recovery contract.
  Design and test that degraded capture mode before changing admission.
- Precise identity witnesses still change for newly inserted dedupe identities;
  actual settlement can insert such receipts. A global command/effect witness
  also changes for another repository. Narrowing payload-only updates therefore
  cannot claim full admission convergence. Exercise actual production write sets.
- Use a general UPDATE trigger comparing old/new rowid and identity, including
  `_rowid_` and `oid` aliases. Capture payload-only writes too: unchanged routing
  identity does not mean unchanged run, effect or evidence semantics.
- Start a new explicitly versioned cursor/receipt contract for changed witness
  semantics. Do not reinterpret old revision tuples or their page receipts.
  Every foreign/active-suffix classification needs verified old/new ownership;
  missing ownership remains a blocker.

The detailed proposed domain/schema/API sketch is temporarily in
`/tmp/phase4-mutation-coverage-next.md`. It is design input, not implemented or
accepted incremental authority. Existing broad lineage checks remain in force.

For semantic archive proof, investigate fixing the immutable retired frontier
and marking any old/new mutation of that prefix dirty rather than incrementally
changing its outcome fold. Verified unrelated/active suffix mutations could
advance the coverage watermark without altering the prefix proof. This requires
complete reverse ownership and current bounded tail validation. Existing audits
also census active allocated sources and global orphan membership, so they cannot
be silently reinterpreted as prefix-only certificates; the domain evidence and
writer join must make that changed scope explicit. Unknown aliases/ownership
remain blockers. Repair requires a fresh complete proof against the committed
source digests.

## Real-history pilot

An independent Sol pilot starts from configured activation and uses public
preflight, claim release, abandoned-planning seal and round finalization. Ten
finalized rounds took 0.607 seconds; 100 took 37.399 seconds before checkpoint
auditing. This demonstrates the uncompacted replay cost and rules out a naive
5,000-round fixture. These are exploratory wall times, not an accepted profile.
A host-run 100 + 100 pilot passed in 91.02 seconds: first batch 40.988 seconds,
audits 1.757 seconds, publication 1.616 seconds, second batch 44.123 seconds.
Initial audits required identity 6, round 7, receipt 400, generation 1,
lifecycle 2 and absence 8 page calls. Compaction contains the suffix growth;
cumulative lifetime audit cost remains unmeasured. A 16–32-round cadence is the
next candidate. Two sandbox attempts encountered SQLite/quota errors and do not
count as functional failures or accepted timing. Actual long-history outcomes
remain to measure.

A cadence-16 diagnostic completed checkpoints at 16 / 64 / 128 / 256 real
rounds. Audit times were 0.335 / 2.447 / 5.488 / 11.247 seconds respectively;
receipt pages were 64 / 256 / 512 / 1,024. At 256, checkpoint replay needed
285 additional pages. Hot reads remained 53–56 rows, about 39–43 KiB and
11.5–12 thousand VM steps; indexed claim proof remained 44 rows, about
11.8 KiB and 5,655 VM steps. The diagnostic was stopped after the verified
256 checkpoint because the existing audit replays from origin after each
source drift, giving quadratic cumulative work. Neither 1,000 nor 5,000 rounds
passed. The namespace sweep's `limit=1` and per-source checkpoint replay are
concrete throughput costs. Bounded batching can reduce transaction overhead,
but requires mutation coverage to remove repeated lifetime replay safely.
The reusable draft and detailed measured output are temporarily in
`/tmp/test_review_execution_compaction_scale.py` and
`/tmp/phase4-compaction-scale-handover.md`; integrate only after the foundation
checkpoint and the production optimization, with fresh outcome assertions.

## Historical import evidence constraint

Independent Sol inspection confirms that `app/workflows/review.py` stores only
latest `pr-N.round` and `pr-N.session`; a later result without a new session can
leave an older handle. Productive progress clears the PR and legacy issue death
counter best-effort, losing reset order. Current remote reviews and labels do
not reconstruct harness acceptance or delivery chronology. Scalar-only source
history therefore remains blocked.

Configured import currently accepts only `EmptyReviewExecutionImportPlan`.
Existing ordered SQL replay requires an already activated execution origin and
checkpoint. A trusted prior SQL authority can support continuation or an
explicitly specified transfer, but a test that seeds a new origin into old
preactivation rows would invent the missing trust root. The postactivation
two-round rehearsal remains continuation evidence. A genuine first historical
multi-round import needs an independently trusted origin and ordered source
corpus that the old scalar producer never wrote. The user confirmed on
2026-09-30 that no additional history source exists and directed ambiguous
historical PRs to remain blocked. Other offline work continues. The required
valid first historical multi-round fixture remains unsupported by these sources;
do not relabel modern continuation as first historical import evidence.

## Required acceptance evidence

- Real 10 / 1,000 / 5,000 round and generation histories through configured
  activation and production control, including repeated checkpoints and
  zero-round generation advances.
- Measured returned SQL rows/bytes, VM work, tick wall/CPU, peak memory where
  relevant, audit completion/admission delay and storage retention.
- Audit progress during unrelated settlement, mutation between pages,
  replacement, rollback, cursor corruption, source aliases and deleted rows.
- Deadline expiry, discrepancy and repair; exact retry; paid settlement and
  recovery while held; restart and bootstrap; open-prefix and byte refusal
  without partial authority.
- Independent Sol/Luna adversarial review and full `make test` before commit.

Historical multi-round imports, terminal import sources, renewed origins,
production wiring and the other continuation families remain separate pending
requirements in the linked milestone and architecture plans. Services remain
paused; no live activation, target mutation, paid external run or push is
authorized by this offline design.
