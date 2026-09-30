# Phase 4 — focused milestone 2 continuation

Updated 2026-09-30, 14:30 AEST. Goal remains all seven phase-4 milestones,
then the six-phase architecture plan. Phase 4 is 0/7 and phases 1–3 are 3/6.
Read CLAUDE.md and the [fixed milestone-2 execution checklist](2026-09-29-phase-4-remaining-milestones.md).

## Priority correction

Operator/retained-plan transport is parked. Finish milestone-2 compaction,
source convergence and actual long-history acceptance; do not mistake another
foundation checkpoint for a milestone. Subtask evidence stays visible in the
checklist without changing the seven agreed acceptance units.

## Main checkout and gate

Checkpoint 138 contains the foundation, collector, fixture isolation and
documentation. Exec session 9799 completed `make test` successfully; log:
`/tmp/phase4-foundation-collector-tiered-gate.log`. Scratch pointer:
`/tmp/phase4-foundation-collector-tiered-gate-scratch-path`.

123 hooks, 3,358 domain, 3,799 adapter and 2,924 app tests passed (10,081 Python
tests), along with parity, four import contracts and both Pyrefly checks.
The earlier exact foundation tree passed 10,063 Python tests; that does not
stand in for this final combined gate. The failed collector gate exposed an
upward app import in adapter tests; collector tests are now in tests/app and
both layout checks pass.

## Verified first-checkpoint bootstrap (isolated, not integrated)

Snapshot `/tmp/phase4-m2-bootstrap-3umflxi_` contains:

- Root-owned `domain/review_execution_archive_bootstrap.py` with typed pending,
  blocker and existing completed observational evidence result.
- Sol-owned `adapters/review_execution_archive_bootstrap.py` and its adapter
  tests: one page per family/call, global identities before dependent passes,
  cumulative read budget, caller transaction and savepoint cleanup.
- Actual copied productive and abandoned sources reach the existing checkpoint
  writer without precompleted test audits. Restart resumes; changed/expired
  sources and small budgets refuse completion without authority loss.
- Luna reproduced a backwards-time source failure committing earlier cursor
  progress; fixed cleanup covers every thrown exception. Recognized inner
  source failures return Blocked, unexpected failures rethrow after rollback.
- 17 bootstrap tests, 2 layout checks and Pyrefly pass. Luna independently ran
  all 17 tests and cleared the repaired result.

Maintenance in this snapshot: Root defined immutable maintenance result
variants and the public `SQLiteReviewExecutionControl.maintain_archive` wrapper.
Sol owns only new compaction-maintenance adapter/tests. It must choose a real
contiguous finalized prefix, preserve open gaps, bootstrap/recertify through
bounded calls, publish only with complete current evidence, and return the new
revision before a new proposal is derived. No silent operator rebasing. Root
owns all shared seams and integration. Maintenance, bootstrap and meter pass
64 focused tests. Luna found missing production caller enforcement; root must
wire maintenance before proposal derivation without rebasing stale proposals.
These files are outside the completed main gate.

## Actual-history profiling

Sol `/root/empty_import_replay` owns only temporary script/log/database artifacts.
Script `/tmp/test_phase4_m2_scale_pilot.py` uses configured activation, real
abandoned production rounds and the atomic checkpoint writer; zero charge mode
is stated, not paid-run proof.

Cadence 128: 128 rounds passed in 66.71s; 256 passed with two checkpoints in
138.35s. Second audit required 1,024 receipt pages and 131 checkpoint pages,
confirming growth remains. At 256 the hot read is 56 rows, 43,036 returned bytes,
11,978 VM steps and about 8.2ms; indexed claim 44 rows, 11,813 bytes and 5,655
VM steps. Cadence 256 took 242.17s for 256 rounds. The 1,000 attempt reached a
verified checkpoint at 512 rounds in 295.94s; the latest audit took 16.57s and
read 2,048 receipt pages and 393 checkpoint pages. It was stopped after projecting
beyond its 600s cap. 1,000 and >5,000 histories are not yet passed.
No source edits are authorized
for this agent. Profile conclusions must identify actual remaining contracts.

## Parked operator work

`/tmp/phase4-source-collector-kiltu4wd` retains the next original-plan receipt /
digest-only retry draft and 30 focused tests. Two earlier review findings
(absent authority with active workflow projection; caller-caught partial import)
were fixed there. Retained timestamp provenance remains a review question.
Do not integrate this work before the milestone-2 blockers.

## Operational constraints

All seven installed services remain paused; no live activation/paid target run.
The user approved the guarded remote repair. One atomic exact-lease push restored
remote dev to 0bf3ba8bd92ede680e3b4cdf1e04324fb5e6ee39 and deleted fix/7-token;
read-only verification confirmed both. No accepted local work was pushed.
No-push remains in force for subsequent work. See the
[fixture incident](2026-09-30-hook-fixture-incident.md).
Ambiguous historical PRs stay blocked; no additional chronology source exists.
Use Sol/Luna agents only, uv for Python and Pyrefly. Before any main integration,
check the completed foundation gate; before any commit, full make test must be green.
