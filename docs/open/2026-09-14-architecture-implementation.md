# Architecture implementation phases

**Resumed by the user on 2026-09-25** ("resume implementing the plan and don't
stop until it's complete"). The [paused handover](2026-09-25-architecture-resume-handover.md)
is historical input. The operator UI checkpoint below is accepted, as is the
offline activation/import checkpoint that follows it. The **resolve slice is
accepted** at `6905184` (absence-authorized retry `bba05c8`; composed fault-matrix
acceptance through the production fix-daemon composition `6905184`). The
**review-rejection slice and phase 3 are accepted** at `a7214bf`; see the
[verification record](2026-09-27-review-slice-verification.md). Current work is
phase 4 under the [review execution design](2026-09-27-phase-4-review-execution-design.md).
The current offline source checkpoints inventory durable generation receipts,
match complete accepted transition chains to typed stale-settlement witnesses,
and classify every potentially successful review submission against complete
settlement and owner sources. A fresh PR first observed after execution
activation now gets a source-checked initialized execution origin in the same
transaction as its workflow; imported and renewed episodes still need their
production writers and the complete archived audit. An offline durable cursor
now scans expected generation sources and the retained receipt namespace in
bounded pages, comparing the set and a prior pass digest. Checkpoint ancestry
and retired round sources are not yet joined to it. A typed checkpoint receipt
and bounded predecessor-chain pages now exist. Canonical reads point-check
the final retired generation source pair and the final retired round's row and
bounded receipt set, but refuse all compacted components until the checkpoint
writer and audit gate are installed.
A bounded transaction-local archive page now observes expected finalized round
sequences and their row/receipt fingerprints for future writer and audit joins;
the separate durable repository sweep accounts for unclaimed rows
observationally.
A read-only transaction-local manifest observer now joins expected retired
round and generation sources to a canonical prefix replay and proposes a
typed checkpoint receipt. Retired round and generation pages now apply its
remaining byte budget before payload reads; the writer still needs a bound
around the canonical load and origin, an unclaimed-row
and receipt census, payload audit and atomic audit gate.
The manifest now classifies each retired round's local receipt payloads after
its source fingerprint, charging repeat reads before moving to the next round.
Command/effect and accepted-transition lineage remain separate proof duties.
Schema v26 now supplies an indexed, read-only repository round sweep page
that checks row and finalized-payload identities before selecting an episode.
The page accepts a caller-selected byte ceiling before payload fetch and
charges foreign episode rows; a complete source census still needs a
resumable freshness witness.
The retained receipt namespace sweep has the same caller-selected byte
ceiling and reports the bytes charged through raw and classified pages.
Schema v27 maintains repository round-row and global retained-receipt mutation
revisions. The durable round-row census now captures its revision across pages,
and the read-only archive join rejects a stale completed round pass. The
receipt census still needs both revisions; this does not authorize compaction.
A transaction-local cursor now persists one bounded sweep page at a time,
checks that the episode has exactly its allocated sequence count, and compares
the prior pass's source membership when the frontier grows or a scheduled
rescan begins. It records a missing, changed or malformed source as a blocked
audit state. A separate durable observational pass now compares a fixed
finalized-round prefix and its exact classified receipt members with every
retained receipt namespace source, leaving valid open-round receipts pending.
The ordered round and generation stream digests each match the initial
checkpoint builder in real-source tests. Bounded checkpoint-chain pages check
typed predecessor links, claim order and retained-row digests. Caller-held
cursors now feed a durable observational scan that checks one predecessor or
retired source per transaction, matches each checkpoint segment's ordered
stream digests, and compares the prior completed chain after tip growth.
A proposed sequential state replay was withdrawn after a valid partial
compaction showed that a later boundary can leave earlier finalized rounds
unapplied. The scan currently verifies source streams, not checkpoint state;
the transaction-local checkpoint manifest now proposes a typed tail
application choice and fingerprints the exact first remaining accepted
boundary when one selected the outcomes. A pure replay check verifies a
complete caller-supplied segment against that choice and projected checkpoint
state. It still needs an atomic writer, durable audit replay, unclaimed row and
receipt census, cumulative hot-read bounds and the admission gate.
A compact application receipt format and bounded source reader now pair a
retained tail choice with its exact checkpoint receipt, pending the writer.
The versioned observational archive scan binds that recorded choice and its
full source digest through each checkpoint segment. It also re-reads the exact
first following accepted generation and checks that source against the recorded
tail choice on every active step. A one-source-at-a-time pure reducer and a
transaction-local SQL observer now replay one archived checkpoint segment and
compare its projected state with its receipt. The versioned archive cursor now
carries that replay across transactions, interleaving accepted generation
boundaries with their retired round outcomes and comparing the final projected
state. Complete source membership, the atomic writer and the admission gate
remain open.
A read-only archive observation now aligns completed checkpoint, round-row,
receipt-member and generation census cursors with one current execution,
including the origin source, checkpoint tip, frontiers and due times. The
round-row and generation audits can bootstrap from the bounded indexed
execution after compaction, while the canonical loader still refuses
compacted authority. On SQLite 3.43+, the canonical active-round suffix now
checks each indexed row's metadata byte length against a cumulative budget
before fetching its payload; older SQLite keeps the weaker prior read path.
The archive observation does not establish a single atomic
snapshot across the completed passes or authorize admission.
A barrier-held source scope now joins
configured-root files and complete open-PR counter linkage before a future
import transaction. See the
[phase-4 source handover](2026-09-28-phase-4-execution-source-handover.md).
The SQL inventory composition now rechecks owner runs, submission settlement
and generation receipts against that barrier-held scope in one control-store
transaction. Its owner inputs remain caller supplied; the production writer
must use the new app composition that derives them from the same repoint
configuration as daemon boot before import authority.
Pure whole-set death attribution now separates current-owner abandoned runs
from predecessor evidence and assigns current counter files to open PR keys.
Unknown or cross-PR deaths block. Historical reset order and predecessor
adoption remain unproved, so this does not yet authorize an inherited debit.
The barrier-held SQL source verifier now carries that exact attribution after
revalidating its owner runs and counter mapping in the acceptance transaction.
The file scope now attributes counters only from the physical review state
directory, while retaining other configured and declared historical roots in
the complete audit. Current fix and steward directories are exempted by
deployment configuration; an unrelated root with active PR evidence blocks
until its producing role is proved. Duplicate PR session sources block direct
planning. These remain offline source checks, not import authority.
Live operation, family activation on
real deployment data and paid harness runs remain separately withheld.

Offline activation checkpoint (2026-09-25): `issue-agent resolve-activation
{inventory,shadow,rehearse,activate}` (see HARNESS.md). Pure planning and shadow
comparison through the real ceiling policy; strict legacy parsing where corrupt,
unmapped, unreadable or cross-root duplicate records block rather than default;
quiescence over canonicalised runs/leases/claims/commands/reservations plus
process and fail-closed heartbeat checks for every same-repository deployment;
a whole-plan confirmation digest; one transaction under the three deployment
barriers writes epoch, imported episodes, ceiling gates, decisions, schema-v22
activation evidence and the journal event. Rehearsal operates only on an
isolated copy (destination may not overlap any original). Legacy resolve Store
accessors and the unblock's resolve reset are deleted; inactive scopes return a
typed `WorkflowMigrationRequired`. Two independent adversarial review rounds;
all substantiated findings fixed with regression tests. `make test`: 1,672
domain, 1,781 adapter, 2,598 app, hooks, contracts and both Pyrefly passes.
No real deployment was activated.

Operator UI checkpoint (2026-09-25): the issue page owns one keyed resolve
controller; the board's "Review holds" opens it rather than mutating from a row.
Commands are prepared from the fresh available status, rechecked against its
binding at confirmation, saved in tab session storage before POST, and retried
only byte-identically after an ambiguous reply; definite outcomes clear the
saved record. The legacy unblock is offered only for a witnessed inactive family
on a held issue. Shared polling now bounds each attempt (30 s) instead of letting a
hung request silently stop a poll. Evidence: 380 Vitest + 3 build tests, 15
Playwright scenarios against the real FastAPI app and production bundle (lost
reply/reload/exact replay, cancel/continue, history lookup, legacy fence,
keyboard flow, 320–1440 px layout), two independent adversarial reviews with
all substantiated findings fixed and regression-tested. Python `make test` was
green on the unchanged backend (1,624 domain / 1,753 adapter / 2,577 app).

Overall status: **1–3 accepted; 4 in progress; 5–6 dependency-gated.** The
[phase table](#phases-and-acceptance) below covers all six phases. Phase 3 ended
with explicit offline review activation, legacy deletion and complete linked
issue hold enforcement. Full `make test`: 2,869 domain, 2,401 adapter and 2,861
app tests; all remaining checks and both Pyrefly passes green. Dashboard:
479 Vitest, 3 build and 21 Playwright tests. Independent adversarial review
found no remaining blockers. This does not authorize a live restart.

The checkpoint narratives below preserve the implementation history; their
then-outstanding phase-3 work is superseded by this acceptance record.

The operator checkpoint passed independent adversarial review and the final
frozen full suite: 1,624 domain, 1,753 adapter, 2,577 application tests, all
remaining checks and both Pyrefly passes. Dashboard contracts/typecheck and
247 Vitest + 3 build tests passed. Verification:
`/tmp/phase3-operator-full-verified.log` and
`/tmp/phase3-operator-dashboard-verified.log`. All four services remain disabled
and inactive. Next is visible operator UX, then explicit offline activation/import
and complete resolve-slice acceptance; phase 4 has not started.

Operator UI collaboration brief (after `5e2d004`): extend the existing dashboard
visual system, not its API authority. Parent owns generated-type aliases, the
single HTTP client boundary, command lifecycle/state and persistent retry identity,
polling/race semantics, and board/detail integration. Astra owns agreed presentational
controls and behavioral UI tests after those contracts are fixed. Keep current
and historical requests distinct; accepted is not completed. A lost response retains
the exact command for retry, never a renewed ID or edited payload. Unknown/absent/
unavailable state never falls back to legacy unblock; verified inactive authority
is the only legacy entry. Reuse existing tokens, controls and polling primitives;
no new framework, dependency or state/queue authority. Independent functional and
visual review, isolated real-browser outcomes and full `make test` precede commit.
No live service, target mutation, paid harness run or activation is authorized.

**Phase 3 resumed on 2026-09-23 at the user's request.** See the
[current re-arm preparation handover](2026-09-23-phase-3-rearm-handover.md),
[split-recovery checkpoint](2026-09-23-phase-3-split-recovery-handover.md),
[ordinary-return checkpoint](2026-09-23-phase-3-paid-return-handover.md) and the
[earlier integration inventory](2026-09-14-phase-3-integration-handover.md).
The ordinary-return checkpoint is committed as `99bc61d`. The documented crash boundary
now has atomic paid-return receipts and composed restart coverage. The bounded
increment passed full-suite verification and independent review; phase 3 overall
remains incomplete.
The subsequent split-recovery checkpoint `73c63bb` also passed the final full suite and
independent review, including replacement-author isolation. Its handover above
records the remaining re-arm/activation work. The bounded-adjudication-query
gap was subsequently closed by `4685ef0`; workspace preparation at `8552c2a`
preceded the current durable-handoff authority checkpoint. It binds re-arm policy,
persisted preparation acknowledgement, quiescence, paid admission and projection
commands to that exact handoff. Independent review and full `make test` passed:
1,592 domain, 1,695 adapter and 2,421 app tests, plus all remaining checks.
Log: `/tmp/phase3-durable-handoff-full-second.log`. The next production increment
implements daemon recovery, shared operator composition and first-entry Git
verification. Independent review and final full `make test` passed: 1,624 domain,
1,721 adapter and 2,482 app tests, hooks/parity, configuration, import contracts
and both Pyrefly checks. Log: `/tmp/phase3-rearm-production-full-verified.log`.
The following operator API increment supplies transport and authoritative
projection; visible SPA controls and explicit activation/import remain outstanding.
This is not full vertical-slice acceptance or live activation.
Implementation and its phase agent are authorized to resume;
live operation and live migrations remain paused. Phase 3 must be accepted before
phase 4 starts. The six-phase goal remains incomplete.

Current collaboration brief: domain policies remain pure; existing SQLite run,
admission, gate, receipt and lifecycle-command authority is reused. Adapters own
I/O, app owns composition; no second queue or execution engine. The parent owns
core values, ports, codec/schema and shared signatures. Phase agents implement
assigned policy/adapter/app slices only after those seams are agreed. Retain
generation fencing, actual settlement provenance, atomic outbox insertion and
bounded reconciliation. Use isolated databases, `uv`, Pyrefly and `apply_patch`;
no live services, target writes, paid harness runs or commits by agents. Full
`make test` and fresh independent adversarial review precede every parent commit.

Production re-arm integration brief (after `2ff5151`): the parent owns shared
values/ports/codec, transition transaction seams and role/API composition. Astra
owns the focused application re-arm service and its composed tests after the
ports are agreed; the adapter agent owns bounded pending-intent observation,
request replay and atomic receipt-based reconciliation. Scheduling metadata is
attached to existing re-arm intent via existing dedupe receipts, never a second
work queue. Fixed deployment barriers surround observation/preparation but no
remote write or nested SQL transaction. Reuse the current fix owner, worktree
adapter and effect executor; inactive authority refuses instead of importing or
falling back. Independent review and full `make test` precede commit. Use isolated
repositories/databases, `uv`, Pyrefly and `apply_patch`; no agent commits or live
services, target changes, paid runs or live migrations.

Operator-boundary integration brief (after `82fe5c7`): the parent owns wire
contracts, workflow identity/port changes, coherent SQLite projection reads,
server-minted local operator identity, and legacy-authority fencing. The Astra
phase agent owns assigned endpoint/application tests after those contracts land;
the adapter agent owns agreed authority-fencing tests. HTTP requests carry full
expected binding plus revision, stable command identities and typed guidance,
never actor/owner/effect/workspace evidence. Verify binding inside the existing
acceptance barrier. Replays report the original intent separately from current
held state. Status reads do not initialize, migrate, reconcile or drain effects.
Reuse the existing loopback Host/Origin/CSRF boundary; its trust domain is the
local operator account, not arbitrary browser-supplied identity. Preserve
distinct inactive/absent/unavailable results and never fall back from an active
family to legacy file/label choreography. No new queue or mutation authority.
Use isolated SQLite/Git fixtures, `uv`, Pyrefly and `apply_patch`, no agent
commits or live operations. Full tests and fresh adversarial review precede
parent integration. Visual UI work remains a subsequent explicit slice.

Workspace-handoff collaboration brief (current increment): exact base and
handoff values live in `domain/resolve_workspace.py`; the parent owns these
values and codec/shared seams. The worktree agent owns strict upstream pinning
and preparation using the existing preserving reset, plus real-Git tests.
Astra owns behavioral domain/codec tests and checks the re-arm integration
contract before its policy changes. No fabricated AUTHOR baseline, mutable
episode reset counter, new queue, or local-base fallback is permitted. A handoff
is an obligation, not execution authority: callers must hold the existing
exclusive deployment barrier and establish quiescence before Git mutation.
Use `uv`, Pyrefly, `apply_patch`, isolated repos/databases, and no agent commits
or live operations. Independent review and full `make test` precede integration.

Re-arm integration collaboration brief: the parent owns workspace/workflow
values, codec, ports, admission/command guards and application composition.
Astra owns the pure request/continue/stop/complete policy and domain tests;
the adapter agent owns agreed SQLite acceptance, quiescence and acknowledgement
methods and tests. A Request-only acceptance wrapper joins fresh remote facts
to observed workspace input. Every intent retains the original handoff;
continuation cannot renew allowance or replace that handoff. Existing dedupe
receipts and deployment barriers own preparation acknowledgement—no new queue,
schema authority or mutable readiness flag. Production effects and paid admission
must require the persisted acknowledgement, not a constructed value. Domain is
pure, adapters perform I/O, app composes; use `uv`, Pyrefly and `apply_patch`,
isolated tests, no agent commits or live operations. Parent integrates only after
independent adversarial review and full `make test`.

Split-recovery collaboration brief (current increment): new bounded cursor,
reply/selection/trail and exact contract/reset values live in
`domain/resolve_split.py`, reused by the existing workflow episode and receipt.
The parent owns these values, ports, codec, runtime-plan/accounting and admission
seams. Astra owns agreed pure completion policy and workflow persistence; the
scoped worktree agent owns verified local revision capture/reset and its tests.
No agent may invent a second phase queue or reset counter. Run/admission rows
and existing dedupe/evidence records own consumption and reset acknowledgement.
Use typed immutable values, short SQL transactions, deployment effect barriers
around Git I/O, and current role composition. Preserve displaced local work
under exact harness-owned recovery refs before a verified reset. Every agent
uses isolated tests, `uv`, Pyrefly and `apply_patch`; no live/target operations.

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
- At the review baseline, #198 triage lifecycle/withdrawal work was uncommitted.
  Independent review required observed issue-author identity before treating
  a comment as author withdrawal. Its then-current full-suite attempt failed
  `test_shutdown_deadline_forces_process_teardown_and_exits_transient`.
  These are historical inputs, not the current working-tree or test status;
  the accepted phase-1/2 integration at `1703195` supersedes that checkpoint.
- The pre-existing `docs/README.md` and critique work was preserved in the
  phase-1/2 integration.
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

The integration later committed at `99bc61d` replaced selected resolve scheduling and
workflow file reads/writes with the episode authority, carries decision bindings
through claims, and checks AUTHOR applicability against a fingerprint of the
authoritative task inputs. Scratch handover and repository-guidance enrichment
are separate runtime context; they must not rewrite or duplicate those inputs.
Application fixtures now exercise real accounting and SQLite authority. Fresh
review found and prompted fixes for discovery-failure isolation, runtime-name
normalization, the resolve-to-PR session handoff, and missing remote ceiling
projection. This integration reached the verified ordinary-return checkpoint
below; complete phase-3 acceptance still requires the remaining vertical slices.

The ordinary RESOLVE/RESUME crash boundary now commits settlement, an immutable
returned-outcome receipt and its observation gate in one existing SQLite
transaction. Restart reconciliation observes fresh issue/PR facts, classifies
once and queues guarded compound follow-up commands atomically. The session is
retained in the receipt until classification, not provisionally published as a
successful continuation. A real child-process exit immediately after settlement
tests that recovery does not pay for another leg. Unavailable receipt records
are isolated per run and use a bounded, restart-safe audit schedule; healthy
targets continue. Receipt-capacity refusal is infrastructure unavailability,
not budget exhaustion. This is not yet full resolve-slice acceptance.

### Phase 3 checkpoint history (complete)

1. **Checkpoint verified:** scheduler, continuation and ordinary paid-return
   integration; held/stale command projection debt uses shared typed retirement
   and read-only reconciliation. Independent review has no blocking findings;
   full `make test` passed with 1,406 domain, 1,521 adapter and 2,408 app tests,
   plus other targets. Log: `/tmp/phase3-return-full-accepted.log`. Committed as
   `99bc61d`.
2. **Checkpoint verified:** AUTHOR/IMPLEMENT/ADJUDICATE settlement and restart
   recovery, durable dispute context, preserving reset authority and replacement-
   author isolation bound to exact new task inputs. Real-Git application coverage,
   final independent review and the full suite passed. Final log:
   `/tmp/phase3-split-final-verified.log`. This accepts the bounded recovery increment,
   not phase 3 overall.
3. **Complete:** re-arm/continuation/cancellation, exclusive barriers, daemon
   recovery, exact-bound operator commands, explicit offline activation/import
   and absence-authorized retry controls.
4. **Complete:** visible dashboard controls, aged-command operator outcomes,
   migration/rehearsal, rollback constraints, old-writer fencing and selected
   legacy deletion. Inactive review authority now reports migration-required.
5. **Resolve slice accepted** at `6905184` with independent review and full
   composed failure coverage. **Review-rejection slice accepted** at `a7214bf`
   after increments 1–8, independent adversarial review and the final full
   suite ([verification](2026-09-27-review-slice-verification.md)).
6. **Phase 3 accepted. Phase 4 started** with the reviewed continuation design
   and inventory. Live activation and soak remain separately blocked by the
   deployment pause, not simulated as completed.

The indexed-allowance checkpoint replaces the repository-wide historical scan
with a bounded per-episode admission query, preserving strict never-launched
exclusions without another counter. Independent review and full `make test`
passed; final log: `/tmp/phase3-episode-index-full-verified.log`. Schema v20 was
exercised only on isolated databases. The current handover records the next
workspace-handoff/re-arm increment; no live family activation has occurred.

Receipt recovery trusts atomically written terminal dispositions and schedule
indexes. It isolates corrupt selected unfinished payloads, not arbitrary at-rest
database tampering. Direct return lookup/capture/finalization still validate
terminal dispositions; a general historical audit/quarantine path is not part of
this increment. Adjacent file-backed infra/block telemetry remains outside the
atomic return classification and is not claimed exactly-once.

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
