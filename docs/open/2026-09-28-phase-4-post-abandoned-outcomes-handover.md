# Phase 4 handover after abandoned-round outcomes

Date: 2026-09-28 Australia/Sydney. Branch `dev`. The six-phase goal remains
active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending. Read
`CLAUDE.md`, `AGENTS.md`, the [progress record](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md), the
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
the [persistence map](2026-09-27-phase-4-persistence-map.md), and the
[previous handover](2026-09-27-phase-4-post-facts-handover.md).

## Current checkpoint

The new transaction-local source reader lives in
`adapters/review_round_sources.py`. `SQLiteControlStore` exposes its existing
child evidence reconstruction as a static transaction-local method so both the
review execution writer and canonical loader use the same verifier.

`finalize_abandoned_round` commits any source-backed, terminal nonproductive
abandoned outcome and the v25 execution projection in one transaction. It
retains facts and outcome in the round row and writes an exact immutable
`review-round-outcome` receipt. Canonical reload rereads parent, child and
submission sources for finalized abandoned suffix rounds. An unchanged
pending command stays open, and exact replay is inert. The review daemon's
bounded recovery sweep now finalizes both newly fenced and previously fenced
abandoned rounds. Durable wait observations track a bounded count per exact
facts plus run/command progress fingerprint; a live leased child does not
advance the count. Blocked reasons are journalled once per distinct reason
up to eight.

Independent Sol review found the missing composed finalization call and a
narrow terminal classifier. Both were corrected. Its follow-up found that
pending command progress was absent from the wait fingerprint and alternating
blocked reasons could re-emit events. Those were corrected and focused tests
cover the cases. Final `make test` passed: 3,121 domain, 2,788 adapter and
2,864 app tests; hooks, governor, parity, four import contracts and both
Pyrefly checks. Log: `/tmp/phase4-abandoned-outcomes-final-full.log`
(ephemeral). No live activation, target mutation, service restart, paid
harness run, migration or push occurred.

## Next implementation boundaries

The subsequent offline transport-return checkpoint classifies a settled
`TRANSPORT_DEATH` envelope as non-striking infrastructure evidence. A
deterministic transport strike still needs its own positive harness proof;
SDK exceptions and Codex's multiple supervised attempts remain separate
composition work. The transport checkpoint passed the full gate: 3,122
domain, 2,788 adapter and 2,864 app tests, plus hooks, governor, parity,
architecture and Pyrefly. Independent Sol review found no blocker.

The following offline Codex checkpoint limits attached review children to
one supervised process for both stream drops and 429 returns. Focused and
composed fake-CLI tests show one launch receipt and durable raw return; a
child with no trusted usage retains its reservation until reconciliation.
Unattached Codex legs keep their retry budgets. Independent Sol review found
no blocker. Full `make test` passed: 3,122 domain, 2,790 adapter and 2,866
app tests, plus hooks, governor, parity, four import contracts and both
Pyrefly checks. Log: `/tmp/phase4-codex-child-retry-full.log` (ephemeral).

The next offline checkpoint adds a completed-planning nonproductive writer
and extends the bounded composed round sweep to sealed completed rounds.
Canonical suffix reload now verifies their source receipts. Full `make test`
passed: 3,122 domain, 2,795 adapter and 2,866 app tests, plus hooks,
governor, parity, architecture and Pyrefly. Log:
`/tmp/phase4-completed-rounds-full.log` (ephemeral). Independent Sol review
found and verified fixes for the missing composed caller and synthetic
fixture incompatibility. The subsequent post-seal abandonment checkpoint
adds terminal non-admission proof for children whose sealed parent departs;
independent Sol review found no introduced blocker, while confirmed productive
submissions still need joint settlement. Full `make test` passed: 3,123
domain, 2,797 adapter and 2,866 app tests, plus hooks, governor, parity,
architecture and Pyrefly. Log: `/tmp/phase4-post-seal-abandonment-full.log`
(ephemeral). Restore composed ordered/compacted suffix coverage alongside
the productive and checkpoint writers.

An attempted source-backed checkpoint writer was reverted after independent
Sol review. Validating only its newest batch missed old source corruption;
validating every batch on each canonical read caused lifetime-sized hot reads
and quadratic repeated finalization (the 65-round focused case took about
43 seconds including setup). The chain also lacked an activation/import anchor
for its sequence-zero state, and its recovery scan revisited historical PRs.
The next design must state which immutable checkpoint becomes authority, how
its initial state is source-backed, how archived sources are audited, and how
ordinary reads and recovery remain bounded. The implementation remains at
checkpoint 24.

Checkpoint 25 adds the offline `RUNTIME` preparation path. A verified Codex
preflight stop now finalizes either sealed completed or departed planning with
no paid strike, while the round outcome, exact preparation receipt, execution
projection and visible hold commit together. The existing hold reconciler
retries the local preflight on a bounded schedule. A successful probe releases
the hold; stale probes and stale changed-head reads cannot clear newer failures.
Production composition supplies an explicit readiness reader for both review
runtimes. Independent Sol review found and verified fixes for a missing reader
and both concurrency release paths. Focused tests cover nine behaviors. Full
`make test` passed: 3,123 domain, 2,806 adapter and 2,866 app tests, plus
hooks, governor, parity, architecture and both Pyrefly checks (ephemeral log
`/tmp/phase4-runtime-preparation-full.log`). No live activation,
target mutation, paid run, migration or push occurred.

1. Add source-specific bounded reread paths for `REVISION_DIFF` and
   `CONTAINMENT` preparation failures when their adapter sources are minted.
   The runtime preparation path already uses the rejection-family hold
   machinery; unsupported kinds remain refused.
2. Settle confirmed paid/copied submissions jointly with the existing
   `record_review` rejection baseline or `open_ceiling` transaction before
   their later marker. Capture its canonical round outcome and execution reset
   there. Keep a pending or uncertain remote submission open.
3. Design source-backed checkpoint compaction and benchmark canonical reads
   with a large finalized suffix. The current abandoned writer deliberately
   keeps outcomes in the suffix so every read verifies source receipts; the
   4,096-round suffix limit is still an activation gate.
4. Wire real round identity through review dispatch, child reservations and
   verdict registration. Resolve SDK exception transport death classification.
   Complete offline migration, shadow,
   rehearsal and activation, then phases 5–6. Live phase-5 windows require
   explicit user authorization.

Standing restrictions from the
[original handover](2026-09-27-review-slice-handover.md) apply. Use Sol or
Luna for subagents. Run the full `make test` gate before every coherent commit.
