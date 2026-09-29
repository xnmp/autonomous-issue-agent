# Phase 4 — execution origin and bounded compaction contract

Date: 2026-09-28 Australia/Sydney. Contract for offline implementation;
the pure prefix planner, checkpoint cursor, imported-origin read prerequisite,
fresh first-observation origin, digest-bound generation receipts and typed
checkpoint source manifest are implemented. The canonical reader verifies
checkpoint row identity, one predecessor, the final retired generation source
pair, and the final retired round row and receipt set, but refuses every
compacted component until complete archive membership and the audit gate
can be verified.
The transaction-local manifest observer now reconstructs expected retired
round and generation streams and a candidate receipt. Its retained-field byte
limit is an accounting ceiling after source reads, not a canonical-load or
peak-memory bound. It neither surveys unclaimed archive rows nor certifies
every retired receipt payload, so it cannot authorize a checkpoint write.
Production import and renewal writers, the checkpoint writer, bounded
compacted hot reads and the complete audit remain pending. A durable offline
cursor now scans expected generation sources and the retained receipt
namespace in bounded pages, comparing the set and the prior pass digest. It
has no admission or compaction authority. Execution authority is not
activated. Read with the
[execution design](2026-09-27-phase-4-review-execution-design.md),
[generation-boundary finding](2026-09-28-phase-4-generation-boundary.md), and
[phase 4 progress](2026-09-27-phase-4-progress.md).

## Reason for a separate checkpoint

Canonical reads currently verify every uncompacted round and every generation
boundary. Each accepted transition snapshot also embeds all earlier retained
boundaries. Repeated rearm or ceiling release therefore makes stored history
and reload work quadratic, and the 4,096-boundary limit eventually refuses
recovery even with zero rounds. The 4,096-round suffix limit likewise refuses
admission after a long gap. The abandoned compaction prototype verified only
its newest proof, lost an import origin, or made hot reads traverse lifetime
history; see the [withdrawal record](2026-09-27-phase-4-progress.md).

The standard event-sourcing shape is a trusted starting snapshot plus replay
of the bounded suffix, while retaining the older event log for a separate
audit. [AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/event-sourcing-pattern.html)
and [Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
describe snapshots as a way to avoid replaying the entire event stream on
ordinary reads. SQLite's
[snapshot isolation](https://www.sqlite.org/isolation.html) lets the writer
capture a consistent source set and publish its proof in one transaction.
Those references guide the shape; the source and authority rules below are
specific to this repository.

## Authority records

1. **Origin.** Every new execution episode must write one immutable origin
   receipt in the same transaction as its active component. Activation/import
   binds it to the reviewed activation plan, authority epoch, imported counter
   and session sources, inherited debit/reset provenance, captured policy,
   initial state, rejection binding, and sequence zero. A renewed episode
   instead binds its origin to the execution-ceiling re-arm completion/authority
   receipt, its prior and new episode bindings, and exact inherited or reset
   state, with its new episode and sequence zero. A rejection ceiling release
   preserves the execution episode. A component without its exact origin
   source is corruption.
   A fixture may construct an active component only with equivalent test
   origin evidence.
2. **Checkpoint.** A compacted prefix records the largest contiguous finalized
   round sequence, the projected state at that point, captured episode policy,
   origin identity, previous checkpoint identity, and an exact digest of the
   newly retired round and boundary receipts. Its frontier is the pair
   `(through_sequence, last retired boundary receipt/revision)`: sequence alone
   cannot distinguish successive zero-round advances. Exact retry keys on the
   previous checkpoint and this frontier/source set. Its receipt is inserted
   with the aggregate update. The current component names one checkpoint
   receipt. The current model permits a sequence-zero checkpoint after one or
   more zero-round generation advances when it has a boundary cursor; a
   production writer must mint its receipt and source proof. A boundary alone
   never proves an older open round is finalized.
3. **Source log.** Round, payment, closure, preparation, submission, transition,
   and generation-boundary receipts remain immutable and addressable after
   compaction. A checkpoint retires them from the *hot replay*, not from the
   audit log. The acceptance proof contains identities and digests of the
   exact sources it retires; it must not accept a count or latest state alone.

## Commit and read rules

- Compact only a source-verified contiguous finalized prefix. Replay from the
  previous checkpoint with the exact boundary acceptance sets and round
  outcomes, then compare the resulting checkpoint and active projection before
  commit. A generation boundary may be retired only when its allocated
  `through_sequence` lies in the finalized prefix; multiple zero-round
  boundaries at the same sequence are retired in accepted transition order.
- Run compaction inside one SQLite write transaction, after reloading the
  canonical aggregate. The transaction publishes the new checkpoint receipt,
  removes retired boundaries from the active component, updates the component,
  and preserves the source rows. A failed receipt or aggregate write rolls all
  of them back. Exact retry returns the existing checkpoint without changing
  the chain.
- An ordinary canonical load verifies the exact origin/checkpoint identity,
  the latest checkpoint receipt and its immediate source anchor, then scans
  only the current bounded round and boundary suffix. Replace the current
  episode-lifetime `COUNT(*)` and compacted-prefix open-row queries with
  indexed high-water/edge and suffix checks. The trusted allocated high-water
  mark comes from the aggregate and checkpoint proof; a deleted archived
  middle row is an audit discrepancy. Neither the load nor the recovery tick
  scans every older checkpoint or historical round.
- A surviving boundary must have a small transition proof minted with the
  accepted transition: exact before/after revisions, mutation, request or
  release identity, boundary identity, and accepted-payload digest. A hot
  read checks that proof and bounded current source identities without
  deserializing its potentially large historical accepted snapshot. The
  archived audit checks the full accepted payload against the proof and its
  retained source receipt. Retention of earlier accepted snapshots cannot
  grow the active boundary tuple or make hot reads scale with their size.
- A separate source-audit operation walks compacted history from the origin
  through each checkpoint and its archived source rows. It records a durable
  audit cursor and discrepancy, is resumable in bounded pages, and blocks
  execution admission on any discrepancy. A completed pass wraps to the
  origin and begins another pass; an overdue pass blocks admission until the
  bounded audit catches up. The cursor and alert update transactionally with
  each checked page. A source changed after its page was checked must be found
  on the next pass. It is not silently presented as a proof that every
  archived source was checked by a hot read.
- Open gaps are recovery obligations. If an older round remains open, the
  bounded sweep must terminalize it from complete source evidence or refuse
  further admission at the explicit suffix limit; compaction cannot declare
  it complete. Recovery advances its cursor rather than rereading every
  historical PR on each tick.

## Checkpoint source manifest and cutover order

The checkpoint receipt must bind its origin, previous checkpoint receipt (or
the initial origin), previous and new `(sequence, boundary revision/receipt)`
frontiers, projected checkpoint, and two ordered source digests. One digest
folds each retired round's episode, sequence, round identity, exact indexed
row fields and verified receipt identities/digests. The other folds each
retired generation number, boundary receipt identity/digest and accepted
transition identity/digest. These are digests of the *source stream*, not of
the projected state alone. The writer derives both streams from the same
transaction-local canonical load and verifies their exact contiguous ranges;
the audit later reconstructs those ranges from the origin and checkpoint
chain. This keeps the receipt small even for thousands of retired sources
without trusting a count or an arbitrary caller-supplied source list.

The generation receipt identity is derived from repository, PR, execution
epoch/episode and destination generation. The audit enumerates every expected
generation from the previous to the new frontier and verifies its derived key
by bounded point lookup; a separate sweep pages by `(scope, key)` and decodes
every matching namespace row to detect unclaimed or unreadable receipts. Both
reject a missing, repeated or foreign boundary.

The checkpoint writer, typed checkpoint reader and durable paged audit gate
must be installed before any production compaction can commit. The writer
verifies the newly retired source stream and initializes its audit state in
the same transaction as the checkpoint and active-suffix update. The writer
must make the cumulative source budget an actual read bound and cover
generation-only and later-checkpoint proposals. Audit pages verify retained
membership and payloads, including full historical
accepted transitions, and persist a cursor, rolling digest, due state and any
discrepancy. The current loader checks typed checkpoint ancestry, the last
retired generation source pair, and the last retired round row and receipt
set, then refuses every compacted component because the checkpoint writer and
audit gate are absent. It cannot yet serve a
compacted writer. The completed hot reader checks the typed receipt,
immediate source anchor, bounded active suffix and audit gate without decoding
historical accepted snapshots or scanning the compacted prefix. Older
full-payload generation receipts require retirement into an audited checkpoint
or an explicit bounded refusal before a universal hot-read bound is claimed.

For the final retired round, the point reader derives membership from its
canonical finalized plan and closure: preflight and outcome, every planned
stage receipt, each planned submission's registration and round index,
completed planning seal and abandonment fence when applicable, deferred
preparation when applicable, and exactly one productivity settlement for a
productive outcome. It rejects contradictory fixed-key receipts, extra stage
or submission-index keys under the round's derived prefixes, and TEXT/BLOB
aliases. Each member contributes `(namespace, key, digest of all six stored
dedupe fields)` in sorted order to the receipt-set SHA-256. The read caps each
field at 2 MiB and the set at 32 MiB. This immediate anchor detects a changed
last-round source; the future writer must verify member payload semantics and
commit the same digest, while the paged audit must reconstruct all retired
round sets and unclaimed global receipts.

The retired-round archive reader can now resolve each expected sequence by
the episode index and return a two-round page of finalized outcomes and row
plus receipt-set fingerprints. It preflights row identity and outcome bytes,
rejects canonical TEXT/BLOB identity aliases, and fails at the first missing
or malformed sequence. This page is an observation for the writer and audit,
not a complete archive proof: the writer must join it to the canonical
projection and verify member payload semantics, and the audit must separately
census unclaimed rows, including noncanonical numeric BLOB encodings that an
exact indexed point lookup cannot enumerate.

Schema v26 adds a repository-key index for a separate round archive sweep.
The read-only page fixes an upper rowid and examines at most 16 repository
rows and 16 MiB of retained fields. Every selected row must have a canonical
SQL/plan identity, planning state and closure-derived outcome before the page
filters for the requested episode; a rekeyed row cannot silently disappear.
The page rejects BLOB repository aliases and malformed numeric BLOB fields.
A complete durable pass must reconcile its episode sources with the expected
checkpoint chain, repeat from the origin after completion, and record
discrepancies before it can gate admission.

The current read-only round audit stores a bounded page cursor and fixed
repository rowid edge in `level_state`. It compares the number of episode
rows with the allocated sequence frontier and XORs SHA-256 members over each
row's sequence, round identity and full source digest. A completed pass keeps
the previous frontier and membership as a baseline; the next pass compares
that prefix while also seeing newly allocated rows. Missing, changed and
malformed rows persist a blocked state. This census does not yet join expected
checkpoint entries or the round receipt namespace. It cannot authorize
compaction or admission.

A read-only checkpoint-chain page now walks one to sixteen canonical
checkpoint receipts backward from the current component's tip toward the
exact origin frontier. It rechecks the current tip, each predecessor link,
claim order and the digest of all six retained receipt fields. A caller-held
page cursor does not prove the earlier path; the durable audit must persist
and revalidate that path, then match each checkpoint's segmented retired
round and generation streams. This page does not authorize compaction,
admission or a compacted hot read.

A durable observational checkpoint archive scan now owns that predecessor
cursor in `level_state`. Each transaction rechecks the current segment
receipt and consumes one expected retired round or accepted generation source,
folding the same ordered digests as the checkpoint receipt builder. It compares
each segment frontier and digest, reaches the exact origin, and records a
completed chain digest for the next due pass. A new tip preserves the previous
chain as a tail baseline; changed or missing predecessors persist a blocker.
Completion is not an admission verdict: checkpoint state replay and a census
of unclaimed round rows and receipt references are still missing. A proposed
round-only replay was withdrawn because a later generation boundary can leave
an earlier finalized round unapplied. The checkpoint receipt does not record
whether that boundary existed when the checkpoint was written. Live generation
alone cannot determine which earlier outcomes were applied. State proof needs
source-backed boundary applicability and checkpoint timing before any audit
result can gate authority. The production writer and canonical compacted read
remain disabled until those checks, cumulative bounds and the atomic audit
gate exist.

The transaction-local manifest now proposes a typed tail application choice
from the compaction plan: all outcomes after the last retired boundary apply,
or the first remaining accepted generation boundary selects them. In the
second case it reads that exact retained boundary pair in the same SQL
transaction, binds its source fingerprints and the digest of its ordered
applied sequences, and charges its bytes to the manifest ceiling. This is a
writer input, not a persisted receipt or state proof. The writer must mint the
choice with its checkpoint atomically; the audit must re-read the named source
and replay outcomes before a compacted load can use it. The event stream
remains the source of truth for snapshots, consistent with the
[event-sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing).
The pure replay check now verifies a complete caller-supplied segment against
that choice and the checkpoint state. It enforces contiguous retired sources,
the supplied following boundary fingerprint and applied-sequence digest, and
the captured revision's ordering. The archived audit does not yet supply or
persist this complete state proof, so this check grants no admission authority.

A read-only receipt sweep page now covers the twelve namespaces used by the
round receipt-set definition, including command-keyed submission and recorded
review receipts. It scans all repositories in indexed namespace, scope and
key order, with a fixed upper key, at most 16 rows and 16 MiB of source fields
per page. Each row retains a full source digest. BLOB namespace aliases,
malformed identities and oversized sources block. The classified page now
requires canonical permanent payloads and identifies bound-round,
named-round and command references. This remains a bounded observation, not
a complete namespace census.

The finalized-round point reader also returns its exact bounded, sorted
`(namespace, key, source digest)` members. It checks that their aggregate
matches the existing receipt-set fingerprint; those member digests match the
indexed sweep's source digest for the same row. The member read still proves
only the local retained set. A second bounded point read classifies every
member payload, and a one-source join resolves named rounds or command
registrations before comparing that source to its exact finalized-round
member. Open rounds have no finalized member set. A persisted observational
pass now compares a fixed contiguous finalized prefix with the complete
retained receipt namespace, leaving valid open-round sources pending. Its
round-source digest matches the initial checkpoint receipt builder's stream
encoding, but later checkpoint segments, checkpoint ancestry and admission
authority remain unimplemented.

An audit discrepancy or overdue pass blocks new execution admission,
generation advance, episode renewal and further compaction. Terminal money
settlement and evidence-preserving recovery remain allowed so a hold cannot
strand an already launched operation. Archived tampering after a checked page
is found on the next pass; the due bound prevents indefinite unchecked use.

Pre-transition compaction must run before a new accepted workflow snapshot
would exceed the codec limit, including repeated zero-round advances. It must
also run before the round or boundary suffix reaches its 4,096-entry ceiling
when a verified contiguous prefix is available. If an open early round leaves
no compactable frontier and the next snapshot would exceed the byte limit,
the new advance refuses before writing an accepted transition and records a
reviewable bounded-capacity reason. A refused compaction leaves existing
authority and receipts unchanged; it cannot discard an open round or use the
new transition as evidence for an older source. The recovery sweep uses the
same writer and frontier rules.

## Acceptance evidence

- Imported and fresh origins replay from their exact activation sources;
  missing, changed, duplicated or foreign-episode origins fail closed.
- Full replay and replay from each compacted prefix produce the same failures,
  pin deferrals, reviewer session, displayed round and productive watermark,
  including repeated zero-round advances. In particular: leave round 1 open,
  finalize round 2 before rearm, finalize round 1 after rearm, then compact
  through 2 and compare full replay with the compacted projection.
- Tampering with a current checkpoint, immediate source, suffix round, or
  boundary proof fails a hot load. Tampering with an older archived source or
  its full accepted transition is found by the paged audit and blocks
  admission, including when it occurs after that page's first audit; the test
  must state that it is an eventual audit guarantee, not an immediate
  hot-read guarantee.
- More than 4,096 finalized rounds and more than 4,096 generation advances
  remain operable through compaction. A deliberately open prefix still hits a
  bounded refusal without losing charge or closure evidence. Exercise a long
  zero-round advance series behind one open early round until the encoded
  accepted snapshot approaches the codec byte ceiling; the next advance must
  refuse without a partial receipt or lost prior state.
- A benchmark compares identical bounded suffixes after 10, 1,000 and more
  than 5,000 lifetime transitions, including zero-round advances. It records
  rows visited, bytes decoded and latency/CPU, showing that hot load and
  recovery work depend on the active suffix rather than episode lifetime.
  Query count alone is insufficient because one row can hold a lifetime-sized
  accepted snapshot. Activation, migration, target mutation and paid runs
  remain separate explicit gates.
