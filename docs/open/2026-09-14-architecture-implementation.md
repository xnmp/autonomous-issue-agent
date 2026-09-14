# Architecture implementation phases

Authority: the revised six-stage plan in
[the archived review](2026-09-14-architecture-review.md), copied from
`/tmp/architecture-review-2026-09-14/report.md`, incorporating
[the critique](2026-09-14-architecture-overhaul-critique.md).
The review baseline was `d26e935`; implementation starts from `4c365a8`
and preserves the existing working tree. The user requested one Astra agent
with a goal per phase. The parent goal covers complete implementation and
honest verification; phase agents are started as dependencies become ready.

## Protected window

The Tauri fix/review/steward services and dashboard are stopped at the user's
request. Do not restart deployments, publish target changes, or mutate live
workflow authority during implementation. Inventory and preserve live data;
exercise migration and rollback against isolated copies first. Actual live
soak evidence requires explicit release of this pause and is not replaced by
simulations. Existing spend caps and the trusted-inbound posture remain intact.

Read-only machine-wide inventory on 2026-09-14 found no reserved, starting or
running runs and no leases across any owner. The four installed target services
were stopped and disabled; retained command/state data was not cleared. This
inventory is not permission to activate the new schema or restart deployment.

## Phases and acceptance

| Phase / Astra owner | Dependencies | Work and acceptance |
| --- | --- | --- |
| 1 — `phase_1_correctness` | None | Reproduce all four findings on current code; unify action-specific eligibility over terminal state and explicit holds; distinguish missing/corrupt/unreadable control records without starving healthy targets; revalidate completion and destructive effects against fresh facts. Valid self-authored updates must still complete. Establish the supported profile and incident/intervention baseline with the parent. |
| 2 — `phase_2_composition` | Starts alongside 1; acceptance uses its fixes | Extend real daemon + durable executor + SQLite composition with a persistent GitHub double, restart and bounded fault injection. Cover remote success before receipt, duplicate delivery, partial relabel, stale completion and concurrent re-arm; prove held-state preservation, stable attempt identity, accounting conservation and visible uncertainty. |
| 3 — `phase_3_gates` | Design may proceed alongside 1–2; implementation requires both accepted | Superseding ADR first where authority moves. Implement one complete generation-fenced gate/re-arm flow, accept it, then the second. Include operator command, continuation changes, dashboard projection, and aged-uncertain-command gates. Completion requires verified remote outcomes. Delete replaced choreography. |
| 4 — `phase_4_continuations` | 3 accepted | Migrate remaining eligibility-bearing counters, consumed replies and continuation episodes one family at a time. Shadow comparison, scoped epochs, old-writer fencing and rollback reconciliation precede cutover. Retire replaced Store suffixes and temporary flags; never operate two authorities. |
| 5 — `phase_5_measurement` | 4 accepted | Implement incident/intervention records and comparable-cohort readouts, replay recorded interventions without an operator, and prepare/run authorized unattended windows. Include parked/abandoned work and operator diagnosis. Select new automation only from measured evidence; no automatic commitment to a diagnostic leg or general policy platform. Live windows remain pending while the user's deployment pause is in force. |
| 6 — `phase_6_policy` | Findings/measurements from preceding phases | Consolidate demonstrated shell/Python gate-policy duplication behind one source while retaining parity and evidence requirements. Preserve current capability validation; do not refactor runtime registries merely because they are parallel. Address independent revision-bound test execution as a distinct verification contract, without treating model review as execution evidence. |

Each phase requires behavioral tests, independent adversarial review, a green
full `make test` before any commit, and explicit deletion/remaining-risk
evidence. The parent integrates and commits coherent accepted changes only.
No phase may claim a live experiment or migration was performed when it was
only exercised in a controlled double.

## Shared conventions brief (included in every phase assignment)

- Read `CLAUDE.md`, `AGENTS.md`, relevant ADRs and the revised report first.
- Pure policies and typed observations/targets/gates belong in `domain/`;
  I/O and persistence belong in `adapters/`; `app/` composes them.
- Preserve SQLite admission/accounting, durable effects/outbox, receipts,
  revision fences and existing process supervision. JSONL is telemetry only.
- Use existing bounded async dispatch and durable effect patterns. Keep SQL
  transactions short and outside remote calls; never invent a second queue,
  workflow engine, resume manager, or independently authoritative state machine.
- The parent owns shared seam decisions and integration: proposed core types,
  action/port changes, schema epochs and cross-cutting signatures must be sent
  to the parent before implementation. Phase 2 owns test-only composition;
  it must not independently extend production seams.
- Shared checkout: preserve existing edits and coordinate overlapping files.
  Use `apply_patch`; Python through `uv`; type-check with Pyrefly. No commits
  by phase agents without parent approval and full-suite evidence.
- If a worktree is used, verify `git merge-base HEAD dev` against the actual
  current `dev` tip before starting, and recheck before integration. A stale
  base is not an acceptable shortcut.
- Establish the assigned phase goal using the goal tool without replacing an
  unrelated active goal or inventing a token budget. Report tool limitations
  honestly. Mark complete only after the phase's acceptance work is done.
- Do not restart paused services or operate the target repository. No live
  paid agents, remote writes, or real-state migrations as a test shortcut.

## Preserved pre-overhaul work

- Committed: `eaa1cc5` exceptional Astra/Fable implementer routing and launcher
  status propagation; `d26e935` shared branch identity and decision ownership;
  `4c365a8` shared PR repair context and revision-bound CI logs.
- Uncommitted #198 triage lifecycle/withdrawal change remains in the checkout.
  Independent review requires adding observed issue-author identity before
  treating a comment as author withdrawal. Its last full-suite attempt failed
  `test_shutdown_deadline_forces_process_teardown_and_exits_transient`; do not
  label the tree fully verified or discard the failure without investigation.
- Existing `docs/README.md` and the critique document belong to concurrent
  review work and must be preserved.
- Tauri #696 remains blocked pending interactive Mac access; #523 badges and
  #514 explicit-opt-in Gravatar were authorized. User clarified #722 means
  pasting an image from another app; this answer has not been posted remotely.
- #723 screenshot was fetched locally at `/tmp/tauri-723-attachment.png` and
  reads: `Open URL error / Unknown URL type, cannot open: trash:///`.
  The triage browser had returned a cache miss. A potential shared attachment
  preparation gap is recorded, not an instruction to widen child networking.

## Status

Phases 1 and 2 are accepted together after independent adversarial review and a
green full `make test`: 1,115 domain, 1,201 adapter and 2,361 application tests,
plus hooks, parity, configuration, import boundaries and both Pyrefly passes.
The durable-composition suite includes 18 controlled fault/interleaving cases.
Final test log: `/tmp/architecture-phases1-2-full-final.log`.
Phase 3's Astra design assignment precedes its implementation; independent
design findings must be resolved before its shared seams are integrated.
Phases 4–6 remain dependency-gated, not silently omitted.

Phase 3 foundation checkpoint: typed identity, validated transition proposals,
bounded encoding, additive test-only v19 schema, captured admission/command/
publication bindings and the first transactional resolve adapter passed the full
`make test` and independent adversarial re-review. The review fixes cover request
replay, receipt provenance/history, retired-command recurrence and cross-target
publication authority. Full log: `/tmp/phase3-foundation-full.log`; the re-review
also ran 302 focused tests and an additional adopted-receipt rollback diagnostic.
This accepts the foundations only, not the complete resolve slice. The superseding
[ADR 0015](../adr/0015-generation-fenced-gate-rearm.md) remains proposed.
Daemon/operator/dashboard wiring, explicit absence-authorized retry, activation
import and full composed crash acceptance still remain. Focused tests passing
do not establish phase acceptance or authorize a live migration/restart.

The following bounded increment adds first-observation initialization only under
an already activated epoch, with owner/freshness/duplicate checks and atomic
evidence recording. Verified command absence now has a pure reconciliation policy
that preserves the retry-authorization gate (or settles retired uncertainty),
retains expiry history and schedules the next read. Independent review accepted
the initializer and caught an immediately-due successful-read loop, now fixed
with a behavioral regression. These pieces still require daemon integration;
they are not a claim that expired-command retries or live activation are wired.
Full verification passed: 1,274 domain, 1,328 adapter and 2,361 application tests,
plus hooks, parity, import contracts and both Pyrefly passes. Final log:
`/tmp/phase3-discovery-reconciliation-full-final.log`. The earlier run's sole
failure was test-only union narrowing in Pyrefly; the complete rerun is green.

The next resolve-continuation increment adds episode-owned fresh/authored
checkpoints and runtime-tagged sessions, durable admission ordering in the existing
reservation transaction, and an adjudication allowance derived from admitted runs.
Independent reviews reproduced and corrected four boundary/interleaving gaps:
incomplete admission replay identity, global-only spend-scope confusion, a delayed
author checkpoint suppressed by a newer session, and progress claimed by a
never-launched reservation. The corrected policy orders author provenance and
session updates independently, and requires normal settled execution evidence for
progress even when no session handle was returned. Real SQLite tests cover all
24 delivery permutations of two authors, implementation and blocked resume.
Daemon/scheduler/operator integration and legacy-path deletion remain required;
these authority primitives alone do not accept the resolve vertical slice.
The corrected increment passed fresh adversarial re-review (327 focused tests,
including both original reproductions) and the full `make test`: 1,345 domain,
1,411 adapter and 2,361 application tests, hooks/parity/import contracts and both
Pyrefly passes. Final log: `/tmp/phase3-continuation-full-final.log`.

Phase 1 replaces contradictory raw trigger checks with shared action-specific
eligibility, replaces swallowed control corruption with per-target errors,
and replaces separate triage/groom relabel-then-close choreography with guarded
compound disposition. Phase 2 extends existing daemon/effect/accounting tests
with a persistent remote double and real process loss; it does not add another
production queue or authority. Reviews also covered the pre-existing #198
author-withdrawal change, now supplied with observed author identity.

The [pre-overhaul baseline](../reference/architecture-overhaul-baseline-2026-09-14.json)
records the supported Linux/Codex profile, paused exposure, separate escaped
incidents/audit catches, and missing measurement fields explicitly. The window
contains 69 settled paid legs costing $76.406584 (configured-rate-card basis)
and two observed merges, with ongoing/inherited work. Those are not 69 admitted
workflows and do not establish comparable cost per merge or unattended success.
Operator duration, complete intervention counts and first-pass approvals remain
unknown rather than zero. Phase 5 must replace these missing observations with
prospective records and comparable workflow cohorts.

### Phase 1 compatibility decisions

Legacy pending close commands lack the facts that authorized their disposition.
Read-only reconciliation may recognize a fully completed outcome, but incomplete
legacy intent must not acquire a fresh snapshot and then mutate the issue.
This deliberately retires automatic restoration of a missing explanation on an
already-closed legacy target; it is not a claim that the old repair still works.
Preserve the command and its uncertainty for a freshly authorized reconciliation
decision. Phase 3's aged-uncertainty gate and migration acceptance cover that
remaining recovery obligation. New guarded compound closes retain automatic
partial-effect recovery under their captured facts.

Resolve's stale-question check compares the supplied title/body/author inputs.
An undifferentiated comment change is not proof that those inputs changed or
that a comment was written by the child: operator and bot can share a login.
Triage/groom consume discussion and keep stricter discussion revalidation.
All paths still recheck fresh lifecycle holds; destructive completion does not
receive a blanket bot-author exemption. This contract does not prove that an
initial resolve consumed every comment-only requirement.
