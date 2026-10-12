# ADR 0016 — Phase 4 current-state authority

Status: Accepted implementation, 2026-10-03. Selected runtime authority and
replacement removals passed A2 whole-system review, copied rehearsal, dashboard
outcome checks and the final full gate. Phases 5 and 6 remain pending.

Governs: src/issue_agent/domain/review_execution.py src/issue_agent/adapters/workflow_rows.py src/issue_agent/adapters/review_execution_control.py src/issue_agent/adapters/review_workflow_control.py src/issue_agent/adapters/control_store.py

## Problem

Phase 4 has made ordinary review admission depend on proving the lifetime
history of its own state. Checkpoint ancestry, independent namespace censuses,
source mutation revisions, expiring archive certificates and maintenance are
now prerequisites of the counters and sessions they were introduced to support.
Global source changes invalidate paged scans. The real-history pilot exposed
repeated lifetime replay; a further change-capture and certification subsystem
was proposed to repair it. None of this completes another continuation family.

ADRs 0002, 0003 and 0015 already trust the harness-owned SQLite transaction for
money, workflow changes and receipts. Extend that boundary consistently instead
of implementing a second historical proof system around review continuity.

## Decision and trust boundary

The committed current workflow state is operational authority. Its transition,
exact immutable result/idempotency receipt and outbox event commit together in
the existing control-store transaction. Remote effects retain their existing
intent, readback and receipt protocol. No network or process work runs in SQL
transactions. A receipt or state write failure rolls back the whole mutation,
including when an enclosing caller catches the exception.

Validate untrusted inputs, exact current identities, typed state, revision,
owner, epoch/episode, required live receipt references and actual accounting
and effect evidence. Missing, unreadable or inconsistent active state blocks
new admission. No absent field or malformed legacy value means zero allowance.
Exact retries resolve their existing receipt before new eligibility checks;
same identity with different input conflicts. Settlement and recovery remain
available while new admission is held.

The boundary trusts harness-owned SQLite writers and the existing database
integrity/ownership controls. It does **not** promise that every well-formed
out-of-band edit to retired history is rediscovered periodically before the
next paid operation. Retired history cannot grant admission or change current
state. This explicitly replaces the extra forensic-detection contract in the
September 28 compaction design; it is not an assertion of equivalent detection.
Historical forensic tooling is not a phase-4 prerequisite. Preserve retained
evidence and the existing integrity checks; do not delete paid-run history.

This decision does not strengthen child credential isolation or change the
trusted-inbound operating posture. An actor able to rewrite the authoritative
database remains outside this boundary, as with the existing money authority.

## Review state and ordering

Reuse the existing execution binding, policy, outcome classifier and state
fields: failure allowance, pin deferrals, runtime-tagged session, displayed
round and their ordered provenance. Do not change strike/reset policy.

The active component contains current typed state, a monotonic allocated round
sequence, a generation-local fold baseline and its cutoff. Keep only the
bounded unresolved suffix in the existing round table: it includes finalized
results after an earlier open gap. Recompute that bounded suffix in allocation
order when a result arrives, using the existing pure outcome rules. Advance
the contiguous finalized baseline in the same transaction. Ordinary reads use
the committed current state and bounded active references; they do not re-run
the historical fold. An exact old result retry uses its indexed retained
receipt, not a history scan.

The existing 4,096 suffix ceiling and source-byte ceilings remain limits on
**unresolved work**, not lifetime rounds. Capacity refusal allocates no partial
round or money. Recovery of the obstructing round remains possible. Completed
history stays indexed and retained; it is not embedded in the active component.

A generation advance freezes the then-current projection, carries only the
authorized state, clears the session according to existing policy and starts
a new generation-local baseline at the allocated cutoff. Persist one immutable
transition receipt with before/after identities and the bounded transition
facts. Do not embed prior transition snapshots or an ever-growing boundaries
tuple. The cutoff does not claim that old open rounds are terminal. Old rounds
remain recoverable through their existing claims/runs/rounds; later results
settle money and retain evidence without changing the new generation.

Execution-ceiling renewal writes its new episode/origin, authorized reset,
intent and required projection command atomically. It completes only after
the existing effect/readback protocol proves the projection. Rejection
recovery cannot renew execution allowance; execution recovery cannot renew
rejection allowance or erase another hold. No ancestry replay is needed to
read the newly committed current episode.

Renewing an execution episode requires complete old-episode quiescence,
including settled money, classified returns and resolved submission effects.
Within a continuing episode, stale productive settlement keeps a harness-minted
immutable witness of its exact original round/outcome/command receipt and the
current binding that makes it inapplicable. The receipt replaces traversal of
every intervening generation; test recovery after several advances explicitly.

## Import and compatibility

> Superseded in part (slice 5a, 2026-10-13): offline family activation and legacy
> import were deleted; family state is initialized only by a target's live first
> observation.

Use stopped-writer barriers, complete configured roots, bounded parsing,
current complete remote observations and one final source recheck. These are
one-time import duties, not periodic admission audits.

Each selected target has an explicit decision: verified import/initialization,
or a durable blocked migration with source identity and reason. Attributable
ambiguous history blocks that target; unknown cross-target ownership or an
incomplete repository census blocks activation of the affected scope. Terminal
retained records are classified explicitly rather than omitted by an open-only
listing. A blocked target cannot be rediscovered as new and initialized empty.
Repository activation must support a mixture of eligible and blocked targets.

The blocked disposition is a canonical typed component under the active
repository epoch, not only a row in an import report. Discovery, loading,
dispatch, operator status and dashboard must all consume it. New-target
initialization first proves there is no retained prior target decision.

The old scalar files did not record historical reset chronology. Such history
stays blocked, as the user directed. Remove the requirement to demonstrate a
successful first historical multi-round import from those impossible sources.
Keep real multi-round **post-activation** continuation/restart tests and label
them accurately. No automatic fresh grant, dropped debt, inferred zero or
operator-reset feature is introduced by this simplification.

Schema changes are additive and versioned; never edit an applied migration or
its digest. Existing schema-32 files must open or refuse with a specific,
non-destructive migration result. Existing phase-4 components are converted
only by a bounded, stopped, verified conversion, otherwise retained and held.
The fact that live activation has not been reported is not proof that a state
file is empty. Old binaries/writers remain fenced. Rollback keeps SQL authority
and never restores file counters over newer results or charges.

## Implemented continuation boundary

Issue recovery, PR repair and separate TRIAGE/GROOM episodes use existing target
aggregates, paid runs/reservations and durable commands. Reply consumption,
allowances, infrastructure classification, source-bearing machine/budget holds,
shared self-clear count and free remediation obligations have typed current
identity and exact result/effect receipts. Source refusal remains specific to
the affected action; healthy peers and unrelated cleanup continue. No generic
workflow engine, queue, history monitor or second counter was added.

PR result recovery selects the union of current issue pending admissions,
current PR admission watermarks and active monetary obligations. Independent
spend reconciliation can settle money before outcome classification, so money
alone is insufficient. A public review generation release and newer REBASE may
supersede PR continuity while an exact earlier CI classification remains owed
by the issue component. Already-settled, fully superseded reply output needs no
new historical completion authority. Selected malformed output remains held;
malformed settled history cannot suppress healthy work. Recovery continues past
refused current candidates and completes at most 32 successful outputs per pass.

Schema39 adds only three fixed partial indexes on existing current workflow,
continuation-admission and reservation tables. Copied 10/1,000/5,000-history
query-plan measurements hold zero-selected work at 20/47/14 SQLite VM steps;
without these indexes the same queries grow with retained history. Exact raw
result reads use the selected run/receipt identity. This does not claim all
writes are globally constant-time: the previously disclosed retirement-header
scan remains distinct from ordinary current reads.

CI history, predicted paths, telemetry stall signatures and Jira mirrors remain
advisory. The obsolete automerge hint reader/callback is removed; existing
exact-head durable merge command/readback receipts provide idempotency, including
lost acknowledgements and restart. Jira's existing raw infrastructure counter
and abandoned-run eligibility gate are the sole approved runtime file-counter
exception; SQL TRIAGE/GROOM are decoupled without dual writes or a third lane.

The copied rollback rehearsal explicitly proves the archived F14 binary refuses
schema39. Only on the stopped test copy, it validates the exact migration39
digest and three index definitions, removes those indexes and that matching
schema metadata, then opens schema38 with the real archived F14 reader. Later
money, holds, replies and every authoritative table row survive unchanged.
Forward migration restores schema39 indexes without data loss. F14 does not
directly open39; no live rollback or generic downgrade API is provided.

## Delete after replacement is accepted

- Online archive certificate/deadline checks in canonical load, preflight,
  paid reservation, launch and generation transition.
- Recurring round/receipt/generation/lifecycle namespace censuses, archive
  replay cursors, checkpoint ancestry/application proofs and global identity
  certification projections used only by that admission architecture.
- Their active trigger maintenance through a new migration, retaining old
  migration definitions/digests and financial/operational source rows.
- Unsupported legacy chronology reconstruction and multi-round import proof
  scaffolding; keep the bounded source classifier and blocked decisions.
- The unintegrated mutation stream, reverse-dependency index and prefix
  certification prototypes. Do not copy them from scratch into production.

Do not remove child reservation, launch/return provenance, round finalization,
actual submission settlement, durable commands, foreign-key/uniqueness checks,
operator fencing, SQL byte limits, runtime session rules or fixture isolation.
Tests asserting those behavioral contracts survive. Tests whose sole contract
is the superseded audit mechanism retire with that mechanism.

## Evidence and limits

Step 0.5 requires equivalence tests for ordered and out-of-order outcomes,
open-gap generation advance, stale result settlement, productive reset, pin
deferral, duplicate/conflicting delivery, rearm isolation, crash/rollback and
malformed active authority. Test 10/1,000/5,000 sequential rounds and more than
4,096 zero-round generation advances through real public control adapters.
Measure selected rows/bytes and query plans: hot work must depend on bounded
pending work, not retired history. Unrelated writes cannot require global
recertification. Wall times are reported, not used as machine-dependent gates.

Archived rows may grow linearly with retained work, like existing accounting
receipts. A new retention/garbage-collection service is out of scope. Revisit
only with measured database-size/backup pressure and a separately scoped issue.

Completion requires removal of the replaced production paths, independent
adversarial review and `make test`. A smaller implementation is expected; line
count alone is not acceptance. No deployment or live data migration is authorized.

## Basis

[SQLite's transactional guarantee](https://www.sqlite.org/transactional.html)
supports atomic state and receipt publication; its
[transaction rules](https://www.sqlite.org/lang_transaction.html) explain short
writer transactions and explicit rollback. Microsoft's
[event-sourcing guidance](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
warns that the pattern adds substantial trade-offs and is unnecessary for many
systems. The application of these principles here is this ADR's decision,
not a claim that those sources establish this repository's business invariants.
