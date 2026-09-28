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

The checkpoint-32 full `make test` gate passed: 3,176 domain, 2,848 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-review-death-classifier-gate.log` (ephemeral). Independent Sol
review found and verified fixes for three provenance defects; its final pass
found no remaining medium or high issue in this pure classifier scope.

These are offline source readers and a classifier. None creates an execution
episode or changes production review eligibility. The current review runtime
still reads legacy session/round files and counters. No execution-family
activation, live migration, target mutation, service restart, paid run or push
occurred.

## Next required boundary

1. Inventory all authorized review owners, including configured predecessor
   deployments. Daemon boot calls `adopt_legacy_owner`; the offline importer
   must inspect the authorized lineage without rewriting it. Complete every
   paged run scan under one SQL snapshot, and revalidate under activation
   barriers before commit.
2. Inventory bound `SUBMIT_REVIEW` commands, their effect receipts and
   submission indices, plus verdict hold provenance. Reconstruct productive
   reset boundaries from confirmed source receipts; a completed agent run is
   not itself a productive review. Unknown death or reset attribution blocks
   import with reviewable evidence.
3. Join positive issue-keyed file counters to PRs with fresh complete
   linked-issue observations. Shared or ambiguous issue counters cannot be
   copied to multiple PRs. Parse runtime-tagged session and round values,
   preserving real session provenance or blocking an unproved handle.
4. Mint activation/renewal origin receipts, checkpoint receipts, bounded hot
   reads and the archived-source audit required by the compaction contract.
   The 4,096-round and generation-boundary suffix limits remain activation
   gates until that work passes.
5. Wire active round identity through production dispatch, child reservation
   and verdict registration; complete composed fault/recovery tests, offline
   shadow, rehearsal and activation. Phase-5 live windows require explicit
   user authorization.

Use Sol or Luna for subagents. Run full `make test` before each coherent
commit. Leave the unrelated untracked
`docs/open/2026-09-28-implementation-progress-board.html` untouched unless
the user asks to incorporate it.
