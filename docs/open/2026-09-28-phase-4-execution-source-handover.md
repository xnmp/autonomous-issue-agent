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

These are offline source readers and a classifier. None creates an execution
episode or changes production review eligibility. The current review runtime
still reads legacy session/round files and counters. No execution-family
activation, live migration, target mutation, service restart, paid run or push
occurred.

## Next required boundary

1. Revalidate the bounded owner closure and run inventory under activation
   barriers before any import commit. Daemon boot's `adopt_legacy_owner`
   rewrites some prior run owners, so neither the current owner nor configured
   owners alone establish complete historical run evidence. Caller-held
   complete values are observational until this SQL recheck succeeds.
2. Join bounded outcome receipts to the complete round inventory, then use
   verified command-to-round links with plain recorded baselines to prove
   confirmed productive outcomes and ordered resets. Verify escalated verdict
   hold provenance and classify unrecorded or stale successful commands
   separately. A completed
   agent run is not itself a productive review; unknown death or reset
   attribution blocks import with reviewable evidence.
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
