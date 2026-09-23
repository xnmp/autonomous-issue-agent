# Phase 3 paid-return integration — 2026-09-23

Historical checkpoint: this increment is now committed as `99bc61d`.
The goal is active; the [split-recovery handover](2026-09-23-phase-3-split-recovery-handover.md)
supersedes the working-tree/goal status below. Live operation remains paused.

## Status and scope

The user explicitly resumed implementation after the earlier diagnostic
checkpoint. The [six-phase plan](2026-09-14-architecture-implementation.md)
is the overall plan. Phases 1–2 are accepted; phase 3 is still in progress;
phases 4–6 have not started. This document supersedes the stop instruction in
[the earlier checkpoint](2026-09-23-architecture-checkpoint-handover.md), not
the protected live-deployment pause.

Read `CLAUDE.md` and [proposed ADR 0015](../adr/0015-generation-fenced-gate-rearm.md)
before continuing. Branch `dev`, implementation base `ee7de45`. Existing
scheduler/continuation changes and this increment remain uncommitted; this
checkpoint passed full verification and independent review. Preserve the entire
working tree. Do not start the harness,
activate live workflows, migrate the live database, publish target changes or
start paid target runs. A fresh read-only service check confirmed the fix,
review, steward and dashboard units are all inactive/dead, disabled, MainPID=0.
This increment did not change service state.

The goal tool still reports the previous paused objective. The user's resume
authorizes work, but the available goal tools cannot change paused to active.
Do not mark the six-phase objective complete or replace it to conceal this.

## Implemented increment

- Ordinary `RESOLVE` and no-PR `RESUME` carry validated task inputs into
  `AccountingRuntime`. Settlement, immutable returned outcome and the exact-run
  observation gate commit in one existing SQLite transaction. A process exit
  immediately after settlement is covered, not merely an application exception.
- `domain/resolve_return.py` holds bounded evidence variants;
  `workflow_policy.decide_return` classifies fresh issue/linkage observations.
  `SQLiteWorkflowControl.finalize_return` atomically commits the single strike,
  continuation update, gate removal, typed final disposition and command outbox.
  Admission/settlement provenance is verified; stale outcomes are retired.
- Compound guarded issue relabels include comments and multiple label additions
  in one command. Partial writes are revalidated against captured facts;
  telemetry includes every effect while old empty payload identities remain
  unchanged. PR projections retain independent head guards.
- Startup/wake recovery and ordinary executor replay invoke bounded return
  reconciliation before paid work. Fresh failed observations preserve the raw
  result/session and defer read-only recovery instead of buying another leg.
- Closed/no-PR local cleanup is idempotently replayed under effect authority,
  requires an exact acknowledgement, and clears the SQL continuation.
- Receipt inputs are bounded before admission; provider prose is normalized
  before settlement. Receipt-authority refusal uses infrastructure unavailability,
  not a false budget-exhaustion classification.
- Corrupt selected unfinished receipts produce typed unavailable diagnostics,
  retain their existing gate, and rotate a durable one-hour audit cursor so a
  bounded batch cannot starve healthy peers. Invalid index keys get diagnostic
  digests. SQLite/systemic failures still propagate. The immutable payload is
  never overwritten by error handling.
- Command draining separates visible inventory from runnable work. Same-generation
  held projections defer quietly; superseded projections retain audited retirement
  evidence. Uncertain outcomes remain read-only and retain gates/history.
  Re-arm and returned-result retirement share the same validation primitive.

No schema migration, second queue, provisional successful continuation or
duplicate quality charge was introduced. `control_store._settle` is the
transaction-local form of existing settlement, reused by normal and atomic-return
paths.

## Verified checkpoint

The first full `make test` stopped at a missing architecture-manifest entry for
the new domain module; that entry is fixed. The subsequent full application
tier found five fixture failures: repeated/new fake gateways lacked issue
revisions. Production correctly rejected unversioned observations; fixture
corrections preserve the production contract.

Focused receipt/isolation, composed process-exit/restart, compound effect and
workflow-policy suites pass. Both Pyrefly passes and `git diff --check` passed.
Independent review drove atomic settlement/capture, bounded malformed output,
terminal session clearing, exact strike accounting, command provenance,
quiet held selection, audited supersession, complete action telemetry, and
per-record recovery isolation. Final independent review reports no remaining
blocking findings for the ordinary RESOLVE/RESUME increment, not phase 3.
The fresh full `make test` after the final SQLite-error propagation correction
passed: 1,406 domain, 1,521 adapter and 2,408 application tests, plus hooks,
configuration, parity, import boundaries and both Pyrefly passes. The command
exited 0 and ended with `all suites passed`. This accepts the bounded ordinary
return increment, not the remaining phase-3 vertical-slice work. No commit,
live activation or operational restart was performed.

Useful logs:

- `/tmp/phase3-return-full-accepted.log`: final complete green run.
- `/tmp/phase3-return-full-final.log`: earlier complete green run before the last
  storage-error correction; superseded by the accepted log.
- `/tmp/phase3-return-full-test.log`: initial manifest failure, not green.
- `/tmp/phase3-return-app-wide.log`: initial five fixture failures, not green.
- `/tmp/phase3-return-isolation3.log`: workflow adapter/domain recovery checks.
- `/tmp/phase3-return-isolation-composed.log`: composed restart and domain checks.
- `/tmp/phase3-return-types-latest.log`: both type-check passes.

## Next work and remaining limits

The current increment's review and full-suite checkpoint are complete.
The phase plan lists the remaining phase-3 checkpoints in order:
split AUTHOR/IMPLEMENT/ADJUDICATE crash recovery; operator import/activation,
re-arm/cancel/explicit retry; authoritative dashboard and aged uncertainty;
rollback/old-writer fencing/deletion; resolve acceptance; then the review-rejection
vertical slice. Do not start phase 4 or call phase 3 complete yet.

Ordinary receipts deliberately reject split stages: adjudication recovery needs
verdict/rationale plus objection/reply/trail/uphold context. The existing split
flow still uses its prior result-recording path. Adjacent infra/block-state and
journal updates are outside atomic return classification and are not claimed
exactly-once. Recovery trusts atomically written terminal dispositions and SQL
scheduling indexes; it is not a general audit/quarantine facility for arbitrary
at-rest database tampering. Strict direct capture/lookup/finalize diagnostics
still validate completed dispositions.

Parent owns core values/ports/codec/schema and shared seams. The resumed Astra
phase agent owns agreed policy/persistence slices; scoped 5.6 agents handled
compound projection/fixtures and independent review. Give subsequent agents the
conventions brief in the plan, coordinate shared files, and use isolated DBs,
`uv`, Pyrefly and `apply_patch`.

Preserved target decisions: #696 waits for interactive Mac access; #523 gets
branch-status badges; #514 Gravatar is explicit opt-in; #722 is pasting an image
from another app. #723 attachment preparation remains a recorded gap. Exceptional
Astra/Fable implementer routing is already committed in `eaa1cc5`.
