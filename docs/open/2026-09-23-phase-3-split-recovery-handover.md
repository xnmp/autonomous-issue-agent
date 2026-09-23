# Phase 3 split recovery — verified checkpoint handover

The subsequent [re-arm preparation checkpoint](2026-09-23-phase-3-rearm-handover.md)
closes the historical adjudication-query debt recorded below and holds the current
next-step inventory. This file retains the split checkpoint's evidence.

The full six-phase goal remains **active and incomplete**. The overall plan is
[architecture implementation phases](2026-09-14-architecture-implementation.md).
Phases 1–2 are accepted; phase 3 is still the current phase. Do not start phase 4
or declare phase 3 accepted from this checkpoint alone.

Base checkpoint: `99bc61d` on `dev`, ordinary RESOLVE/RESUME atomic returned-result
capture and recovery. This checkpoint adds the split changes described below;
its commit subject is `feat: recover split resolve stages with isolated contract resets`.
Full verification and independent review are complete for this bounded increment.
Live services, target
writes, real migrations and paid harness runs remain paused/unperformed.

## Current implementation

- `domain/resolve_split.py` supplies bounded immutable context, exact reply and
  routing selection, contract baseline provenance, split successors and reset
  intent/acknowledgement. Cursor/source means the latest paid predecessor;
  immutable receipts retain the original dispute and intervening attempts.
- AUTHOR, IMPLEMENT and ADJUDICATE now capture their stage inputs and normalized
  output in the existing accounting settlement transaction. Original task input
  fingerprints remain independent of delivered scratch/repository context.
- New split SQL admission requires the typed stage input, exact cursor/reset
  acknowledgement, runtime/model/effort and immutable predecessor. Existing
  dedupe rows record consumed phases and a target-scoped latest work admission;
  ordinary RESOLVE/RESUME cannot bypass an active split episode.
- `workflow-work-latest` indexes every non-ROUTE work admission. The same loader
  validates the immutable run/admission evidence for admission and reset. No
  additional queue or independent retry counter was introduced.
- The existing deployment barrier is shared/nonblocking during bound admission
  and exclusive during contract reset. Git runs outside SQL transactions. An
  abandoned reset intent does not permanently block a changed-input AUTHOR;
  once another stage is admitted, the old reset fails its latest-source fence.
- Worktree capture pins exact clean contract commits. Reset preserves committed,
  dirty, untracked and ignored displaced work under an intent-specific recovery ref,
  verifies the managed branch/path, restores the exact baseline and checks the
  postcondition, including ignored-file absence. Replaying an acknowledged reset
  skips Git. The unchecked legacy `reset()` API was deleted. AUTHOR capture still
  permits ignored caches; reset cannot exempt arbitrary ignored directories,
  because they may contain implementation content.
- Review found replacement AUTHOR could observe prior implementation. The new
  `ReauthorResetIntent` is distinct from an adjudication petition, binds the exact
  replacement input fingerprint, and uses the same archive/authority/acknowledgement
  path. A pure policy derives safe baselines from authenticated finalized returns,
  even when changed issue facts cleared the successor. Failed replacement writing
  cannot promote its captured HEAD. `ReauthorInput` carries the exact reset proof;
  admission refuses a plain replacement AUTHOR or a stale/different-input reset.
  A shared bounded one-hop predecessor resolver handles proven never-launched
  stages without another queue, pointer, or historical scan.
- Successful UPHELD requires the unchanged baseline; REVISED requires an actual
  changed captured revision. Missing capture evidence holds the return. Recovery
  reads an existing run pin only and verifies the current clean checkout; it
  cannot manufacture a historical AUTHOR/revised baseline from a later HEAD.
- A normal returned INFRA IMPLEMENT advances its split predecessor while
  preserving partial implementation work. INFRA ADJUDICATE advances a dispute
  predecessor so retry requires a new reset/archive. Neither changes the quality
  failure count or session. A separate split admission watermark preserves
  ordering, including terminal/cap-clearing tombstones.
- Adjudications consume the existing episode allowance, including lost/unknown
  execution. Only a reservation durably proven never launched and released
  without any charge/receipt/lease is exempt; its exact retry can reuse the
  immutable reset acknowledgement without repeating Git.
  Exhaustion creates the durable contract escalation, not a budget label, and
  clears the actionable split successor. Blocked/requeued implementation retains
  context but yields to normal lifecycle scheduling rather than looping inline.
- Supported `MAX_CONTRACT_ROUNDS` is now 0–4. Provider prose is bounded by its
  encoded JSON size; AUTHOR preflight reserves retained-context capacity for
  permitted rounds independently of transient reset provenance. All stage input
  envelopes have a separate ceiling. Near-boundary ASCII/Unicode tests cover
  initial AUTHOR, replacement AUTHOR and IMPLEMENT with 0, 2 and 4 allowed rounds;
  a four-round, large-Unicode settlement test also passes.
- Old split admission records remain readable. Historical concurrent/reversed
  result fixtures now explicitly seed pre-cutover rows; they no longer require
  the new SQL writer to admit impossible concurrent split phases.

## Verification and outstanding acceptance

Before these changes, the three composed stage tests reproduced a charge with
no recoverable return after settlement. The composed suite now has 33 passing
cases, including actual `os._exit` after each split stage settlement, recreation
of the SQLite/app composition, correct successor and no paid recovery dispatch.
Latest composed log: `/tmp/phase3-split-composition2.log`.

Focused pure policy/SQL suites passed 344 cases; worktree and app subsets and
both source/test Pyrefly checks have passed during integration. Independent
adversarial re-review found no remaining P0/P1 blocker in this bounded checkpoint
after the ordering, retry, cross-stage, reset-locking, author-isolation and
bounded-context corrections. This is not complete phase-3 acceptance.

Full-suite attempts:

1. `/tmp/phase3-split-full-first.log` stopped at dependency resolution because the
   sandbox-only uv cache lacked packages and DNS was unavailable. This was not a
   test pass.
2. `/tmp/phase3-split-full-second.log`: domain passed; six old AUTHOR handover
   fixture cases failed strict admission. Those tests were migrated and now
   pass, preserving authoritative-versus-delivered-input assertions.
3. `/tmp/phase3-split-full-third.log` passed all suites, import contracts and both
   Pyrefly checks. Additional edits made during that run require a final full
   rerun before any commit.
4. `/tmp/phase3-split-full-final.log` also passed all suites. Replacement-author
   isolation changes followed, so this is not final acceptance of the current tree.
5. `/tmp/phase3-reauthor-full-first.log` passed all suites and both type checks.
   Review subsequently found the ordinary-to-split admission boundary needed an
   explicit refusal. The new guard and two focused real-runtime regressions pass;
   a fresh final full run is required before commit.
6. `/tmp/phase3-split-accepted-final.log` passed, but the retained-context versus
   reset-envelope capacity correction followed. The final current-tree run is
   `/tmp/phase3-split-final-verified.log`: all suites passed, including both
   Pyrefly checks and all import contracts. This is the final checkpoint evidence.

Do not commit until the final full `make test` and independent review both pass.
Use the normal existing uv cache with approved `make test` execution when the
sandbox-only cache lacks dependencies. Focused runs use
`UV_CACHE_DIR=/tmp/uv-cache uv run ...`.

Specific edges to finish/audit:

- Replacement-author isolation is implemented and independently reviewed.
  Focused runtime/domain tests cover exact input binding, typed reset purpose,
  paid-return recomposition, refusal without reset, and fresh reset after executed
  author failure. SQL tests cover changed-facts cleared cursors and proven
  never-launched attempts. Real-Git application regressions now pass for changed
  issue body and workflow guidance: the next author sees only the contract
  baseline, displaced committed/dirty/untracked/ignored work is recoverable, and
  crash/restart reuses the exact acknowledgement with one reset. A changed issue
  during reset prevents paid AUTHOR dispatch. The app/worktree subset passed 143
  tests; the policy/control subset passed 366; current source/test Pyrefly is green.
- Real-Git partial IMPLEMENT + infrastructure-failure restart regression passes:
  committed implementation, dirty tracked content and untracked work all survive;
  the next scheduler runs one IMPLEMENT without another AUTHOR.
- A durably proven unlaunched cancelled reservation can retry exact inputs;
  SQL checks `spawn_pending`, released reservation, no receipt/charge/lease.
  The corresponding ADJUDICATE reset/cap path now exempts this exact proof from
  the round allowance and reuses only its matching immutable reset acknowledgement.
  Independent review reproduced the ceiling-one retry successfully. The full app
  restart regression now passes: only ADJ is retried, with the exact prior input
  and no second Git reset. Post-launch uncertainty cannot reuse old reset evidence.
- Adjudication allowance is still projected from admitted run rows by the shared
  helper. Independent review retains a P2 acceptance debt: it scans repository-wide
  historical bindings, so runtime grows with history and a malformed unrelated
  binding can block healthy episodes. Before full phase-3 acceptance, replace
  this with an indexed immutable per-episode query bounded by the ceiling plus
  one, retaining strict proven-never-launched exclusion. Latest-stage exclusion
  itself no longer scans all historical runs.
- Pin capture failure before a pin exists remains explicitly held; no arbitrary
  later checkout is promoted to historical evidence.
- A latest-work pointer from another episode currently fails closed in re-author
  preparation. The remaining re-arm/activation slice must deliberately reconcile
  cross-episode worktree provenance; no foreign baseline is silently inherited.
- Ordinary RESOLVE/RESUME cannot switch into split contract writing within the
  same episode: there is no verified pre-implementation contract baseline to
  isolate the author. Both app preparation and SQL admission fail closed. Mode
  transition needs deliberate episode/worktree reconciliation, not a fresh-context
  fiction or a reset to an arbitrary later HEAD.
- Review-rejection vertical slice, remaining continuation families, activation
  and rollback acceptance, measurement/live windows, and policy consolidation
  remain in the all-phase plan. This increment does not complete those items.

## Agent ownership

- `phase_3_gates` (Astra): pure policy and workflow-control/reset persistence,
  associated tests; currently finished/frozen awaiting findings.
- `compound_projection` (GPT-5.6): worktree capture/reset, app split integration,
  app fixtures and real-Git retry test.
- `review_return_recovery` (GPT-5.6): independent read-only adversarial review.
- Parent owns core values, ports, codec/schema, accounting/admission seams,
  composition tests and integration. No subagent may commit or operate targets.

Preserve target decisions: #696 blocked pending interactive Mac; #523 branch
badges; #514 Gravatar explicit opt-in; #722 means pasting an image from another
app. Astra/Fable exceptional implementer routing was already committed earlier.

## Next phase-3 increment

The Astra inventory recommends one exhausted resolve issue accepting a durable
re-arm, surviving restart, completing required remediation, and reporting verified
eligibility or the exact remaining hold. Existing pure request/continue/cancel/
complete primitives have no production callers; dashboard still uses legacy
cross-role reset choreography. Activation epochs currently have test-only writers.

Start with explicit cross-episode worktree handoff: a fresh episode must not
silently delete the latest-work pointer or inherit a previous implementation as
an author baseline. Preserve displaced work and bind the permitted starting tree
to the accepted re-arm request; retain a hold until acknowledgement. Continuation
of an already granted re-arm must retain its existing episode and allowance.

Then compose operator acceptance and bounded daemon reconciliation using the
existing fix owner, typed intent/outbox and short SQL transactions. Dashboard
lifetime must not own completion. Follow with isolated activation/import and
shared dashboard projection; implement the currently protocol-only one-shot
absence-authorized retry before deleting migrated resolve file choreography.
Keep unselected families and shared PR-repair consumers intact until migrated.

Required composed cases include duplicate/concurrent requests, crash after intent,
remote success before receipt, disabled executor, simultaneous holds, unavailable
linkage, cancellation/continuation without a second grant, stale paid returns,
cross-episode checkout recovery, absence-authorized retry/restart, and malformed
or changed import sources. No live activation is authorized by this inventory.

Read-only service verification during this increment confirmed fix, review,
steward and dashboard are inactive/dead, disabled, with MainPID=0. No service was
started or target repository modified.
