# Phase 3 re-arm preparation — checkpoint handover

## Production integration checkpoint (2026-09-23)

The current increment builds on `2ff5151`. Production daemon recovery and
first-paid-entry composition are implemented; operator HTTP/CLI transport,
dashboard projection, explicit activation/import and the second gate family
remain outstanding. Phase 3 is not accepted. The six-phase goal remains active;
phases 4–6 retain their dependencies. Services and live migrations remain paused.

- `app/workflows/resolve_rearm.py` composes Request/Continue/Cancel with the
  existing fix-owned workflow adapter and durable projection command. It never
  drains remote writes under its exclusive deployment barriers. Replays retain
  canonical operator subject and exact command identity without another fetch,
  allowance grant or reset.
- Recovery uses existing re-arm intent plus dedupe observation history, with
  bounded batches, backoff and aged observation holds. Healthy peers continue
  after corrupt per-target records. Healthy Git verification removes only the
  observation hold belonging to the original handoff, including after Continue.
  Fresh facts supersede obsolete decisions before workspace mutation; completion
  requires actual persisted command receipts and exact verified workspace proof.
- `app/resolve_control.py` supplies shared daemon/operator composition and a
  fail-closed operator factory. It requires existing current authority, the
  canonical configured deployment and fix owner; it does not initialize,
  migrate, adopt or fall back to legacy writes. There is no operator transport
  caller yet. Snapshot host disagreement is refused because the existing GitHub
  runner inherits process host.
- The fix role's existing recovery callback now reconciles both paid returns
  and pending re-arms, including startup and effect replay without world
  construction or paid dispatch.
- `WorkspaceAdmission` fences actual Git verification and first reservation
  together: deployment barriers, then the Worktrees mutation lock, then short
  SQLite admission. All locks release before provider execution. Direct public
  reservation cannot create the first completed-handoff admission without this
  proof. Task construction uses the exact read-only managed path and never
  recreates a missing prepared checkout through `ensure`.
- The immutable paid admission records `workspace_handoff_id`. One indexed
  current-episode admission, including ROUTE or an unlaunched reservation,
  consumes first-entry verification. Later legitimate agent edits are not
  compared with the pristine base or reset again. This proves admission, not
  attribution of every later dirty file. Existing continuation/recovery rules
  govern those files. No new readiness flag, counter, queue or schema is added.
- Codex hook preparation writes outside the checkout in the state directory;
  earlier assumptions that it dirtied the prepared tree were incorrect. The
  actual mutating pre-admission hazard was `ensure` recreating a missing tree.

Independent review corrections include preserving normal/shared fail-fast
admission outside the one-shot handoff boundary; deferring a failed read only
against the exact acquired revision; superseding requests when external holds
change the required remote projection; rejecting contradictory uncertainty;
retaining observation holds until healthy verification; and making generic
terminal transitions retain every successful required-command receipt, not
just the receipts named by their caller. The optimistic entry lookup uses a
deferred read transaction rather than taking another SQLite writer lock.

The metric is fewer mechanical re-arm interventions and duplicate paid retries.
Guardrails are unchanged allowance/spend conservation, preservation of displaced
work and independent holds, exact remote receipts, and no additional exclusive
locking on ordinary admissions. Composed fault replay is the current evidence;
live intervention/cost measurement remains phase 5 work under the user's pause.

Focused domain, adapter, real-Git and actual-executor tests pass. The executor
suite includes a spawned process exiting after remote success before receipt,
then restart reconciliation with exactly one remote relabel. Independent review
accepted the bounded checkpoint after the corrections above (111 focused
domain/adapter and 194 focused app checks, Pyrefly zero errors). The first full
attempt found architecture manifest/no-op logging contract violations; the
second collected a new test before its correction; the third passed 1,624 domain
and 1,719 adapter cases but found nine role-composition cases rejected by an
unnecessary concrete worktree-adapter type check. Removing that nominal check
preserves the existing injected adapter contract without weakening Git proof;
all 124 role tests now pass. The next full run passed 1,624 domain, 1,721 adapter
and 2,482 app cases, then import-linter caught a daemon-to-optional-web dependency
through the CLI naming helper. That unchanged pure helper now lives in
`domain/deployment.py`, shared by CLI and operator composition. All four import
contracts pass. Final frozen `make test` passed at
`/tmp/phase3-rearm-production-full-verified.log`: 1,624 domain, 1,721 adapter and
2,482 app tests, hooks 123, parity 13, configuration checks, all four import
contracts and both Pyrefly checks (zero errors). Independent review also accepted
the dependency correction after verifying unchanged CLI normalization/re-export
and 34 operator-composition cases. Services were rechecked inactive/dead,
disabled, MainPID=0. The checkpoint subject is
`feat: reconcile resolve rearms and verify first paid workspace entry`.
This accepts only the bounded production integration, not phase 3 overall.

Next: expose typed operator Request/Continue/Cancel and held-state reads through
the authenticated local dashboard boundary; retain stable IDs/revision CAS and
canonical server-minted actor. Refuse legacy unblock for an activated resolve
family. Exercise explicit offline import/rollback against isolated copies,
including legacy unbound run refusal/draining/mapping, before any authorized
activation. Complete the resolve slice before starting review rejection.

The sections below retain the preceding accepted checkpoint history.

Goal remains **active and incomplete**: complete the remaining phases of the
[six-phase plan](2026-09-14-architecture-implementation.md). Phases 1–2 are
accepted; phase 3 is current. No phase-4 implementation or live activation is
authorized by this checkpoint. The durable handoff increment builds on `8552c2a`
on `dev`. Earlier `4685ef0` accepted the bounded episode-index increment below;
`73c63bb` accepted
[split recovery](2026-09-23-phase-3-split-recovery-handover.md), not the complete
resolve or phase-3 vertical slice.

## Accepted episode-index increment (`4685ef0`)

The split reviewer identified a repository-wide historical adjudication-binding
scan. It grew with all retained runs and decoded unrelated corrupt payloads on
admission, reset and finalization. This verified checkpoint removes that
scan without adding a second counter or admissions table.
Its commit subject is `fix: bound adjudication allowance reads by episode`.

- Schema v20 adds a stable `runs.workflow_episode_key` and a partial index for
  counted adjudications. The key is a canonical JSON tuple of repository, typed
  target, family, epoch and episode; generation is deliberately excluded.
- The existing reservation transaction writes binding and key together. SQL
  guards reject missing projections and changing an established key. Idempotent
  reservation replay checks the projection too.
- The query returns at most `MAX_SPLIT_TRAIL + 1` rows and verifies each selected
  run against its immutable admission and reservation. Saturation above every
  supported ceiling is sufficient; the result is not an exact all-history total.
- `unlaunched_released` is a terminal evidence classification, not an independent
  allowance flag. Only reservation release mints it, inside the same transaction,
  from an active reservation, reserved/starting run, spawn-pending policy and no
  lease, charge or provider receipt. The proof is checked before deleting leases.
  General, ambiguous, crashed and accounting-retained releases remain counted.
- The public completion API cannot mint that classification; both lease paths
  reject terminal runs; settlement rejects released reservations. Existing
  semantic integrity checks reject contradictory proof records. Strict reset
  replay also rechecks proof. The bounded hot path does not continuously audit
  every excluded historical record after arbitrary raw SQL tampering.
- V20 backfill uses the tagged codec and validates run/admission/reservation
  provenance, never current workflow state. Its stable transform specification
  participates in the migration digest. It runs in the existing migration
  transaction with bounded-memory iteration. Malformed bound history rolls back
  schema and row changes; it cannot silently gain a fresh allowance.
- Only isolated test databases were migrated. No live database, service, target
  repository or remote issue was changed.

The metric is bounded allowance-query work as retained history grows. Allowance
conservation and strict unknown-execution treatment are the guardrails. An
execution-step regression covers 4,000 historical and exempt runs; independent
release-order tests cover both winners of the release/lease race.

Verification: the 14 index/migration cases and 8 release-proof cases pass.
The first full run exposed legacy fixtures omitting the new projection
and collected a provisional corruption test before its authority-boundary
correction; those are fixed. Current full run:
`/tmp/phase3-episode-index-full-second.log` passed all suites. Independent review
accepted the increment; its nonblocking wire-stability suggestion was adopted
before the first v20 commit, using the existing `issue`/`pr` scope discriminator.
The intermediate rerun collected a new wire test before its invalid PR-resolve
fixture was corrected; the focused corrected suite is green. Exact frozen-tree
full verification `/tmp/phase3-episode-index-full-verified.log` passed: 1,461 domain,
1,615 adapter and 2,421 application tests, plus hooks, parity, configuration,
all import contracts and both Pyrefly checks. Independent review accepted the
final stable-wire correction as well. This closes the indexed-allowance debt,
not phase 3 overall.

## Current workspace-preparation increment

This verified checkpoint implements the typed workspace obligation and local Git
preparation boundary, not production operator re-arm or its durable
acknowledgement authority. There are no production callers yet. Keep this
distinction when continuing: a valid handoff value is not permission to reset.

- `domain/resolve_workspace.py` holds the observed upstream base, explicit
  prior-admission/absence variants, exact handoff, preparation operation and
  acknowledgement. No successful AUTHOR baseline is fabricated.
- The handoff identity includes the original grant, source/destination
  authority and episode, prior admission/version, issue branch and exact base.
  Observation time is not replay identity. A later execution generation in the
  same destination episode retains the original handoff, pin and archive.
- The closed workflow codec registers these variants without permissive
  missing-field defaults. Shared repository and Git-object validation replace
  ad-hoc validation objects. Publication and base observation share one remote
  identity policy in `domain/git_remote.py`.
- Worktree preparation reuses the existing preserving reset and its recovery
  archive; it does not add a second reset journal or pending-work queue.
  Configured upstream observation and exact-pin verification remain distinct
  from later checkout mutation. No stale local base may substitute for a
  successful upstream fetch.
- Review identified additional required boundaries: credential-bearing
  fetch URLs must be refused before process invocation, and missing admitted
  predecessor work must not be replaced with a new empty branch. Fresh branch
  creation is reserved for explicit verified admission absence. The checkout
  must be the exact canonical `issue-N` child, not merely a descendant or a
  symlink alias of another managed checkout. Explicit external worktree roots
  and `WORKTREE_ROOT=.` remain supported; the target itself cannot be either
  the managed root or the repository root.

The metric is avoiding paid work against a contaminated or incorrectly selected
new-episode checkout. Preservation of all displaced work and one allowance per
grant remain guardrails. Full `make test` passed against the final frozen code:
1,568 domain, 1,658 adapter and 2,421 application tests, plus hooks, parity,
configuration, all import contracts and both Pyrefly checks. Independent adversarial review has
accepted the final correction; its 150 focused workspace/domain/codec/remote
checks pass. The 19 real-Git workspace cases include preservation, pin movement,
invalid/unavailable upstream, missing/corrupt pins, credential refusal, exact
child containment and restart after pin/reset interruption. Existing worktree
coverage also passes (101 combined cases). Final full log:
`/tmp/phase3-workspace-preparation-full-final.log`.
No database schema, live authority, target repository or service was changed.
The checkpoint commit subject is
`feat: prepare resolve episode workspaces from pinned upstream`.

## Durable handoff authority integration

The current increment connects preparation evidence to workflow policy and SQLite
authority. It does not yet add production operator or daemon callers.

- New request acceptance carries both fresh remote facts and a typed workspace
  observation. Pending, cancelled and superseded intents retain the original
  handoff; completed intent requires its exact preparation acknowledgement.
  Continue preserves the grant, base and archive instead of selecting new ones.
- The bound workflow adapter acquires existing fix/review/steward barriers in
  order and checks current authority and target quiescence. Active runs, process
  leases, unsettled reservations, captured unfinished returns and unresolved
  publication commands prevent preparation. Git work occurs outside SQL.
- Preparation acknowledgement uses the existing dedupe receipt table, keyed by
  exact handoff identity. A caller-constructed proof cannot by itself complete
  the workflow or make its remote projection runnable. Reads validate the
  persisted acknowledgement, and paid admission requires a completed re-arm.
- The latest admitted-work pointer remains intact. Bridging a previous episode
  requires a completed handoff naming that exact admission/version. Missing
  pointer plus retained work is corruption, not a clean workspace.
- Schema v21 adds indexes over the existing immutable episode projection and
  unresolved publication command run IDs for target-wide quiescence/absence
  checks. It adds no authority rows or second queue. The allowance query explicitly
  selects its existing partial index: a regression showed SQLite otherwise
  chose the new full index and scanned excluded historical adjudications.

Parent owns types, ports, codec, schema and central admission/effect guards;
Astra owns pure transition behavior; the adapter agent owns SQLite authority
and its tests. Independent adversarial review accepted the increment after two
corrections: reject future predecessor admission versions before workspace I/O,
and force target-first indexed joins for both captured returns and unresolved
publications. A CAST matching the TEXT run ID is necessary for SQLite to probe
the publication expression index instead of scanning it. Behavioral execution-
step tests cover 4,000 unrelated return records, 4,000 unrelated active commands
and 4,000 terminal commands; scope and v21 rollback tests also pass.

The first full run passed 1,592 domain tests but exposed eight return-command
fixture failures: they claimed no predecessor despite actual paid admissions.
Those fixtures now observe the real predecessor, and the healthy-command test
acknowledges preparation before expecting it to run. Its 13 focused cases pass.
The second full run, `/tmp/phase3-durable-handoff-full-second.log`, passed:
1,592 domain, 1,695 adapter and 2,421 application tests, plus hooks, parity,
configuration, all import contracts and both Pyrefly checks. The independent review ran 267 focused tests;
additional paid-bridge tests cover old ordinary/legacy AUTHOR work, new ordinary
and AUTHOR entry, missing/corrupt acknowledgement, crash rollback and cancel/
continue without another allowance. No live database has been migrated.
The checkpoint commit subject is `feat: fence resolve rearm with durable workspace handoffs`.
This accepts the bounded authority increment, not the complete resolve slice or
phase 3. Services were rechecked inactive/dead, disabled and MainPID=0.

Activation/import must refuse, drain or map legacy runs without a bound workflow
projection before enabling this family; the current index cannot prove those
runs quiescent. This remains an explicit activation prerequisite, not an implicit
claim that existing live history is ready for import.

## Prior production integration brief (now implemented above)

Add a small `app/workflows/resolve_rearm.py` service, recovered by the existing
fix workflow callback already bound to both `Executor.replay_pending` and
`EffectRecovery`. Use bounded due selection and persisted observation backoff;
do not add another queue. Required remaining shared ports are canonical accepted
request lookup, pending intent inventory, deferred observation recording,
atomic observation/receipt-based completion or supersession, and first-workspace
entry lookup. Extend the existing worktree port with observe/pin, prepare and
verify operations.

Acceptance must keep its exclusive barriers across fresh observation, upstream
pinning, proposal and commit, then release them before remote execution. The
daemon prepares or verifies the workspace and acknowledges it before the existing
command drain runs the fix-owned projection. A later reconciliation joins its
durable receipt with fresh facts to complete the intent. Dashboard transport
must supply authenticated actor, stable request ID and expected revision, never
new command owners or file resets for this migrated family.

Before first paid entry, verify the actual checkout against its persisted
acknowledgement atomically with reservation. Hook preparation is external to the
checkout. The current production increment above implements this seam, including
router entry and reservation replay, without a separate readiness marker.

## Agreed re-arm handoff design

The Astra phase owner inspected existing policy, persistence, dashboard and
worktree behavior. Current request/continue/cancel/completion primitives still
lack production callers, epochs have test-only writers, and dashboard unblock
still performs legacy cross-role choreography. Complete the resolve slice before
starting review rejection.

A genuinely new allowance starts at an exact harness-observed, pinned configured
upstream base for both prior ordinary and split work. Displaced issue work is
archived using the existing preserving reset mechanism, not silently deleted.
Do not fabricate `ContractBaseline`: no successful AUTHOR exists in the new
episode. Do not use current HEAD or a fallback local base as upstream proof.

Shared values now implemented by the parent; workflow integration remains:

- `ObservedResolveBase`: repository, configured upstream ref, exact commit and
  observation time.
- `EpisodeWorkspaceHandoff`: granting request, source and destination binding,
  verified prior admission or proven absence, managed branch and pinned base;
  deterministic handoff, pin and archive identities.
- `PrepareResolveWorkspace`: an operation in the existing pending re-arm.
- `EpisodeWorkspacePrepared`: exact handoff, current execution binding, preserving
  reset result and observation time.

The handoff remains bound to the original granting request and episode. A
continued re-arm may advance generation, but cannot reset the workspace again
after acknowledgement, replace the base, or grant another allowance. Keep the
old latest-work pointer. Cross-episode admission may pass it only through an
acknowledged handoff naming that exact predecessor; after new admission normal
latest-work fencing resumes.

Acceptance validates fresh issue/no-PR facts and quiescence under existing
exclusive deployment barriers, captures the upstream pin without changing the
checkout, and atomically records grant, preparation obligation and projection.
The existing daemon reconciler performs Git outside SQL, records its exact
acknowledgement, then releases exclusive barriers before remote effects. Keep
paid work held until both workspace and remote projection are verified. A
terminal run alone is insufficient quiescence: reject live processes, unsettled
reservations, unfinalized returns and unresolved publication effects.

Required tests: prior ordinary/split work preserved, missing/moved/unreadable
upstream, wrong checkout, corrupt predecessor, all archive/reset/ack/remote crash
boundaries, concurrent admission/re-arm, cancellation and continuation without a
second grant/reset, upstream movement after acceptance, stale results/effects,
and unrelated holds. Then wire operator acceptance, bounded daemon recovery,
isolated activation/import, shared held-state projection and one-shot authorized
absence retry. Delete only the selected family's replaced live choreography.

## Working conventions and ownership

Parent owns core values, ports, schema/codec, shared signatures and integration.
`phase_3_gates` (Astra) owns agreed pure policy and workflow persistence slices;
it added the workspace domain/codec behavior tests for this increment.
`compound_projection` owns workspace adapter and real-Git test implementation.
`review_return_recovery` independently reviews this increment read-only.
Use the existing authority, receipts, outbox, barriers and daemon—not another
queue, retry counter or orchestration engine. Domain stays pure, adapters own
I/O, app composes. Use `uv`, Pyrefly and `apply_patch`; full `make test` and fresh
independent review precede commits. No live operations or agent commits.

Services were reverified inactive/dead and disabled with MainPID=0 during this
checkpoint and have not been started. Preserve target decisions:
#696 blocked pending interactive Mac; #523 branch badges; #514 explicit opt-in
Gravatar; #722 image pasted from another app. Exceptional Astra/Fable routing
was already committed. No live qualification or soak has been performed.
