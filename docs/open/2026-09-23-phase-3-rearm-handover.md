# Phase 3 re-arm preparation — checkpoint handover

Goal remains **active and incomplete**: complete the remaining phases of the
[six-phase plan](2026-09-14-architecture-implementation.md). Phases 1–2 are
accepted; phase 3 is current. No phase-4 implementation or live activation is
authorized by this checkpoint. Base is `4685ef0` on `dev`; that commit accepted
the bounded episode-index increment below. The preceding `73c63bb` accepted
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

## Agreed next re-arm handoff design

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
