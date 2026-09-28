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

The checkpoint-35 full `make test` gate passed: 3,191 domain, 2,868 adapter
and 2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten strict xfails remained expected. The log is
`/tmp/phase4-owner-lineage-gate.log` (ephemeral). Independent Sol review found
and verified fixes for the implicit legacy owner and BLOB successor omissions;
its second pass found no remaining medium or high issue.

These are offline source readers and a classifier. None creates an execution
episode or changes production review eligibility. The current review runtime
still reads legacy session/round files and counters. No execution-family
activation, live migration, target mutation, service restart, paid run or push
occurred.

## Next required boundary

1. Complete every paged run scan for the inventoried owner closure under the
   same SQL snapshot, then revalidate the closure and runs under activation
   barriers before commit. Daemon boot's `adopt_legacy_owner` rewrites some
   prior run owners, so neither the current owner nor configured owners alone
   establish complete historical run evidence.
2. Join the inventoried `SUBMIT_REVIEW` commands, effects and registration/index
   rows to bounded round plans; inspect verdict hold provenance. Reconstruct productive
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
commit. Keep the six-phase progress board updated at accepted checkpoints;
the user explicitly requested it on 2026-09-28.
