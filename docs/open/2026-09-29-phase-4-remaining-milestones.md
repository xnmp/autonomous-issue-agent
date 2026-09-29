# Phase 4 — remaining acceptance milestones

Baseline: `59214b8` (2026-09-29, Australia/Sydney). Phases 1–3 are accepted;
phase 4 is active. This is a completion tracker for the remaining phase-4 work,
not a replacement for the [review execution design](2026-09-27-phase-4-review-execution-design.md),
[continuation inventory](2026-09-27-phase-4-continuation-inventory.md), or
[six-phase acceptance plan](2026-09-14-architecture-implementation.md).

**Status: 0 of 7 remaining milestones accepted.** Substantial review runtime,
historical-source, and archive-audit groundwork is already accepted. The count
below deliberately measures remaining outcomes, not commits, tests, lines of
code, elapsed time, or percentage of effort. Update a milestone only when its
acceptance evidence exists; record new findings as blockers under that
milestone rather than adding another progress unit.

| Milestone | Status | Observable acceptance result |
| --- | --- | --- |
| **1. Close review import evidence** | Doing | A complete, bounded and freshness-checked source set yields either one deterministic import decision or a specific blocker for every review PR. Link registrations to commands and rounds; resolve or explicitly block historical submissions, refused-ceiling prior state, ordered resets, death attribution, issue counters, sessions, and delivered/current verdict holds. Demonstrate a valid multi-round import fixture and malformed, ambiguous, missing, and changed-source refusals. |
| **2. Commit review authority atomically** | To do | An isolated SQLite transaction imports the verified decision and writes the execution component, origin, checkpoint and source receipts together. Exact retry is inert; failure rolls back all authority changes. Compacted hot reads stay bounded and reject corrupt or stale evidence. The durable archive audit and admission gate block incomplete, changed, or overdue sources. |
| **3. Complete the review production slice offline** | To do | Scheduler, child reservation, submission settlement, recovery, operator and dashboard all use the same execution authority. Selected legacy runtime readers/writers are removed or fenced. Composed fault tests, shadow comparison, quiescence check, offline activation rehearsal, and rollback reconciliation pass without two live authorities. No live deployment is implied. |
| **4. Migrate consumed replies and fix repair** | To do | Consumed human replies and fix repair session, model, branch, and resume state have typed PR/issue-scoped authority and durable receipts. Replayed or reordered replies cannot repeat or erase a continuation. Shadow results match supported legacy cases; ambiguous legacy data blocks import. Retire replaced runtime file paths. |
| **5. Migrate remaining fix recovery** | To do | CI, review, and rebase allowances; infrastructure deaths; machine/budget holds; self-clear state; and PR remediation markers use one durable authority. Recovery, duplicate delivery, restart, operator action, and rollback tests preserve unrelated holds and accounting. Shadow and offline cutover evidence cover every selected legacy reader and writer. |
| **6. Migrate steward continuation** | To do | Triage/groom baselines and stalls use scoped durable episodes and ordered receipts. Restart and duplicate-result tests show the same eligibility and continuation decisions as the shadow; ambiguous history blocks import. Replaced steward runtime paths are fenced or removed. |
| **7. Accept phase 4 as one system** | To do | Cross-family daemon/effect/accounting tests pass, selected legacy Store suffixes and temporary flags are retired, shadow differences are resolved or documented blockers, rollback retains SQL authority, independent adversarial review finds no blocking issue, and full `make test` is green. Record exactly which offline activation/rehearsal ran and which live actions remain paused. |

Milestone 1 evidence in progress: checkpoint 131 links one bounded
registration to its recorded command, effect, preflight, round, and planned
stage sources in a transaction. The global command census still lacks an
expected registration membership proof, so this does not accept the milestone.

## Progress rule

For each milestone, record the accepting commit, behavior-level test or
rehearsal result, full `make test` result, independent review result, removed
legacy paths, and any remaining risk. A passing unit test or another source
reader is an increment within a milestone, not milestone completion. If a
blocker makes a result impossible, show it explicitly; do not convert unknown
historical state to zero or silently narrow the source set.

Update the phase-4 milestone count when a gate is accepted. Keep the existing
six-phase board at **3/6** until milestone 7 is accepted. Live activation and
deployment remain subject to the existing pause and separate authorization;
offline tests or a controlled double must never be reported as live cutover.
