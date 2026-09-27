# Phase 4 — review execution provenance

Date: 2026-09-27. Read-only implementation analysis supporting the
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[outer-round/finalization design](2026-09-27-phase-4-review-execution-design.md#outer-round-outcome-contract).
The parent owns the planned-stage, finalization, claim-occurrence and receipt
contracts. Settle those contracts before freezing schema v25. No live migration
or deployment is authorized by this document.

## Admission is not launch

`SQLiteControlStore._reserve` creates the run in `reserved` state, its captured
workflow binding, budget reservation and `reservation_recovery` record with
`policy='spawn_pending'`. These establish admitted work. They do not establish
that a provider process started or returned a reusable session.

`supervised_exec._register` is the existing guarded launch boundary:

1. Verify the launching parent's PID/start-ticks identity.
2. Register the prepared supervisor token and child process identity.
3. Call `SQLiteControlStore.register_process_lease` with the reserved run,
   owner, PID, process group and start ticks.
4. Recheck the parent and registered launch token before provider execution.

`register_process_lease` atomically records the lease, changes the run to
`running`, initializes `runs.started_at`, and changes reservation recovery to
`running`. This is the appropriate hook for an immutable SQL launch-registration
receipt naming the exact run/admission, planned child, outer round, dual binding,
owner and process identity.

Crossing this boundary does not prove that provider billing occurred. Provider
exec/fork failure or cancellation after registration remains possible. Such a
run requires its verified infrastructure/cancellation outcome; it cannot be
declared never launched merely because no tokens were billed.

There is currently no immutable SQL launch-receipt identifier to reference.
Leases are deleted during normal completion, settlement and recovery.
`runs.started_at` survives registration, but `start_run` can also initialize it
before execution, and the separate `register_lease` helper has different
timestamp behavior. Do not mint `LaunchedReviewChild` from an arbitrary
timestamp, a terminal state, or an invented `launch:<run>` receipt string.
Persist and validate the receipt at the actual registration transaction.

## Settlement and returned evidence

`AccountingRuntime._settle_result` constructs a receipt ID of the form
`runtime:session-or-no-session:run`. This is a harness identity, not independent
proof of provider launch. `SQLiteControlStore._settle` atomically records the
charge, settled reservation and matching run receipt. These establish money
settlement; session and productive-review authority additionally require their
typed source evidence and bound submission receipt.

An existing edge needs an explicit finalization decision: `CodexRunner.run`
returns `AgentResult(exit_reason=RUNTIME_UNAVAILABLE)` when preflight fails
before any process starts. Accounting can normally settle that returned result
as zero `SYNTHETIC` cost, leaving a completed/settled run with a receipt ID but
no launch. It is neither a launched failure nor the current never-launched
release variant. Choose a typed no-launch settlement outcome or normalize a
provably unlaunched return into the release path before settlement. Do not
rewrite an already settled reservation as released or infer launch from its
receipt ID.

For review children, use the existing accounting-return transaction seam to
persist returned evidence with money. The resolve path already implements this
in `SQLiteWorkflowControl.settle_return` using `_settle` and return capture in
one transaction. Review needs its corresponding typed capture, preserving
launch, refusal and session-source distinctions through multi-review synthesis.

## Death, release and cancellation

`reclaim_dead_leases` snapshots the full lease identity, probes process
liveness outside SQL, revalidates the snapshot in the write transaction, deletes
the lease, marks the run `abandoned/dead_lease`, and retains its active budget
with recovery policy `reconcile_only`. This is a verified child-death hook at
which immutable evidence should be retained before deleting the lease.

A later normal, non-`RECONCILED` settlement can correct `dead_lease` to
`completed/settled`. Finalization must handle that race before charging a sealed
outer outcome. Several abandoned children still produce at most one failure
for their outer round, and active accounting obligations must be reconciled.

The existing `proven_unlaunched_run` predicate requires all of:

- `runs.state='cancelled'`, `outcome='unlaunched_released'`;
- a released budget reservation and recovery policy `spawn_pending`;
- no provider receipt, charge or lease.

`release_unlaunched` checks reserved/starting state, `spawn_pending` and absence
of a lease before using the release transaction to mint that outcome. Reuse
this proof for a never-launched reservation release. Generic released state or
an absent lease does not provide equivalent evidence.

`run_cancellation_intents` retains owner/run/reason evidence for hard shutdown;
`has_cancellation_intent` can read it after lease deletion. Operator/shutdown
cancellation does not consume an ordinary failure strike. Actual spend still
settles, and active unsettled reservations continue to block recovery
completion. `record_reservation_retained` can mark a run completed with
`spend_unreconciled` and delete its lease, reinforcing that terminal state alone
is insufficient.

## Parent claim occurrence and abandonment

`dispatch_claims` stores repository, role, target, leg, owner ID, parent PID,
parent start ticks, paths and claim timestamp. `dispatch_claim_runs` links its
admitted children. `claim_dispatch` probes parent identity outside SQL and
revalidates observations before reclaiming; a live parent or a bound
reserved/starting/running child protects the claim.

Existing limitations matter to outer-round recovery:

- `release_dispatch_claim` deletes the claim, cascading deletion of child
  links. Claim reclamation also deletes it. Neither retains an immutable
  release/abandonment receipt.
- `_dispatch_claim_observations` omits the claim timestamp and leg. Its
  snapshot is not a complete immutable occurrence identity.
- `active_dispatch_claims` is an explicitly fail-soft advisory reader that
  skips malformed rows. It cannot authorize abandonment.
- The same daemon/owner can release and reacquire the same target. Owner and
  target alone cannot identify the original outer invocation.

At preflight, capture the exact claim occurrence, including PID/start ticks
and claim timestamp, or introduce a durable occurrence ID. Each stage/child
admission must match that occurrence. Persist an immutable release/reclaim
receipt at the existing claim-removal hooks, tied to its outer round, before
the row disappears.

For an unsealed round, snapshot authoritative parent/child/lease facts, probe
the captured process identity outside SQL, then revalidate and record the
abandonment decision atomically. An absent claim, terminal children, owner
retirement, or an empty planned stage alone does not prove the parent stopped.
An explicit normal release also needs durable meaning when the daemon remains
alive. The resulting evidence supports the existing reconciler; it introduces
no new scheduler or queue.
