# Architecture goal — comprehensive paused handover

> **Historical.** The user resumed implementation on 2026-09-25; the operator UI
> described below as uncommitted WIP was finished, reviewed and committed. See the
> [implementation plan](2026-09-14-architecture-implementation.md) for current status.

Date: 2026-09-25. Repository: `/home/chong/Repos/autonomous-issue-agent`.
Branch: `dev`. Last accepted commit: **`5e2d004`**, `feat: expose fenced resolve operator controls`.

## Start here: authority and stop state

The user's latest instruction is: **“stop and write a comprehensive handover
document for another agent to resume the goal.”** Work is stopped, not complete.
Do not interpret this document as permission to resume implementation or services.

The goal tool reports objective `continue until the rest of the phases are complete`,
status **paused**, no token budget. The live-agent inventory at handover lists only
the parent `/root`; no subagents are running. Names retained in environment metadata
are historical, not evidence that those agents are active. Do not blindly restart them.
No new tests, implementation, commit, services or agents were started for this handover.
Uncommitted work was preserved. Only handover/plan documentation was changed.

Read in this order when the user authorizes resumption:

1. `CLAUDE.md` (authoritative project instructions), `AGENTS.md`, and current user instructions.
2. This document and `git status --short`; preserve existing changes.
3. [Overall six-phase plan](2026-09-14-architecture-implementation.md).
4. [Accepted operator API checkpoint](2026-09-23-phase-3-operator-handover.md).
5. [Re-arm production checkpoint](2026-09-23-phase-3-rearm-handover.md), then the
   linked split/ordinary-return handovers if touching backend recovery.
6. [Archived review](2026-09-14-architecture-review.md) and
   [critique](2026-09-14-architecture-overhaul-critique.md) for acceptance intent.

The original `/tmp/architecture-review-2026-09-14/report.md` is not a durable source;
use the archived review in the repository. Older handovers saying “active” describe
their historical checkpoint; this pause supersedes them.

## Scope, user decisions, and protected live window

The original task was to point the harness at `~/Repos/tauri-explorer`, supervise
issues, and fix harness gaps systematically to reduce future failure surface.
The user then paused that work to implement the architecture review first, requested
phases with Astra phase agents and a goal, and later resumed phase implementation.
The six-phase architecture goal remains incomplete. A bounded checkpoint is not
completion of a phase or of the goal.

Preserve these Tauri decisions when live issue work is eventually authorized:

- **#696:** remain blocked pending a real interactive Mac to measure startup and
  qualify the half-bounce target. Instrumentation is already merged. Do not
  substitute Linux or simulated evidence. Continue other issues when authorized.
- **#523:** visible branch-status badges approved for richer merged/stale/squash classification.
- **#514:** Gravatar only as explicit opt-in; do not silently send hashed author emails.
- **#722:** reproduction is pasting an image from another app, not copying image files.
- **#723:** `trash:///` attachment-preparation gap recorded, not fixed; do not claim
  the old `/tmp/tauri-723-attachment.png` artifact is still available without checking.
- Exceptional Astra/Fable implementer routing was already committed as `eaa1cc5`.
  Do not implement another competing routing mechanism.

These decisions were not posted to GitHub during this architecture work.

Do not restart live services, mutate the target checkout, buy paid harness runs,
post issues/PRs, activate a workflow family, or migrate live authority. Resuming
implementation alone does not release this separate operational pause. Phase-5
live unattended evidence requires explicit authorization and cannot be replaced
by simulation. Preserve spend caps, trusted-inbound rules, and live state.

Last successful service check, documented at the September 23 checkpoint, found
all four units inactive/dead, disabled, MainPID=0:

```
issue-agent-fix-watcher-xnmp-tauri-explorer.service
issue-agent-review-watcher-xnmp-tauri-explorer.service
issue-agent-steward-xnmp-tauri-explorer.service
issue-agent-dashboard.service
```

A read-only recheck on September 25 could not connect to the user systemd bus
(`Operation not permitted`). Therefore current host-service state is **not freshly
verified**. No restart was attempted. Process inspection in the sandbox found no
test/harness process besides the inspection itself, but that is not host-wide proof.

## All phases and dependencies

The overall plan above is canonical and includes all six phases and detailed
acceptance criteria; this table is a navigation summary.

| Phase | Status | Remaining purpose / dependency |
| --- | --- | --- |
| 1 — correctness | Accepted with 2 at `1703195` | Eligibility, corrupt/unreadable authority, fresh completion and destructive-effect facts. |
| 2 — composition | Accepted with 1 | Durable SQLite/executor/daemon restart and fault composition; stable attempts, accounting and uncertainty. |
| 3 — gates and re-arm | In progress, paused | Finish resolve UI, explicit offline activation/import, legacy retirement and full resolve acceptance; then complete review-rejection slice. |
| 4 — continuations | Not started; requires 3 | Migrate remaining counters/replies/episodes by family, scoped epochs, shadow comparison, old-writer fencing and rollback. One authority only. |
| 5 — measurement | Requires 4 | Incident/intervention records, comparable cohorts, replay and authorized unattended window. Include abandoned/parked work and diagnosis. |
| 6 — policy/verification | Gated on earlier findings | Consolidate demonstrated shell/Python policy duplication; separately enforce revision-bound execution verification. Model review is not execution evidence. |

For phase 3 specifically, visible controls alone are insufficient. Still required:
explicit offline import/activation and rollback rehearsals against isolated copies;
inventory/drain/map legacy unbound work; old-writer fencing; remaining
absence-authorized retry controls; deletion of replaced choreography; full composed
resolve crash acceptance; then the review-rejection gate family. Review the plan's
“Phase 3 remaining checkpoints” before declaring any slice accepted.

## Accepted backend baseline (not current UI verification)

Recent commits, newest first:

```
5e2d004 feat: expose fenced resolve operator controls
82fe5c7 feat: reconcile resolve rearms and verify first paid workspace entry
2ff5151 feat: fence resolve rearm with durable workspace handoffs
```

Schema remains version 21. SQLite owns workflow/run/admission/gate/receipt and
lifecycle-command authority; JSONL is telemetry. No second queue or execution engine.
Read backend handovers before changing these contracts:

- Re-arm request/continue/cancel has durable exact bindings and shared production
  composition. The daemon performs bounded recovery. Continuation cannot buy a
  new allowance or replace the original immutable workspace handoff.
- Preparation acknowledgment and verified remote receipts fence completion. A
  handoff is an obligation, not execution permission. First paid workspace entry
  verifies Git under existing deployment barriers and the worktree lock; ordinary
  admission retains its shared/nonblocking path.
- Prior work is preserved under exact recovery refs before reset to a verified
  pinned upstream base. Never fabricate an AUTHOR baseline or use a local fallback.
- Stable indexed workflow episode identity excludes generation. Corruption is
  target-scoped so healthy peers can continue. Existing accounting admission is
  reused after budget reclaim. Uncertainty holds remain tied to their original handoff.

### Operator protocol

GET `/api/repos/{name}/issues/{number}/resolve-control`, optional `request_id`:
available/inactive → 200; absent → 404; unavailable → 503. Reads are coherent,
read-only SQLite observations. They do not create/adopt/migrate a store, call
Git/GitHub, reconcile, or drain effects. Current and requested historical intent
are distinct. `workflow_held` means SQL holds, **not** remote/budget readiness.
Missing epoch/target with retained authority is unavailable, not harmless absence.

POST the same path plus `/commands`: strict discriminated Request/Continue/Cancel.
Expected binding includes repository, issue, family, epoch, generation, version,
episode. Use stable request/cancellation IDs, and keep/clear/set guidance variants.
Server mints `local-user:<uid>` actor/channel; browser cannot supply authority,
receipts, owner or workspace evidence. Exact browser integers cap at 2^53−1.

| HTTP status | Typed command outcome |
| --- | --- |
| 202 | accepted |
| 200 | replayed |
| 409 | conflict |
| 422 | rejected |
| 503 | unavailable |

Acceptance is durable intent, **not recovery completion**. Canonical replay checks
the original binding/operator/content under existing barriers and SQLite CAS.
Never mint a new ID or edit a payload simply because a response was lost.

Legacy unblock is allowed only for a witnessed current-schema **inactive** family,
under the same three exclusive deployment barriers. Unknown, unavailable, absent
or active authority must never fall back. One validated active launch snapshot
supplies immutable scoped config/env, effects, credentials, paths and admission
provenance; no overlay/per-field fallback. See `app/resolve_control.py` and
`load_operator_configuration`. Supplied stores are never initialized implicitly.

Listener binds literal loopback only; existing Host/Origin/custom-header checks
remain. This is local-account trust, not multi-user authentication. Generated
OpenAPI/TypeScript/Zod retain literal discriminators through the exporter's
const-to-single-enum compatibility step. Do not hand-edit generated contracts.

### Baseline evidence and provenance

The committed operator handover records final frozen full-suite success:
1,624 domain, 1,753 adapter, 2,577 application, 123 hook tests, remaining gate/config
checks, four import contracts, both Pyrefly checks zero; dashboard 247 Vitest and
3 build tests, generation/typecheck, and independent adversarial acceptance.

Historical log paths:

```
/tmp/phase3-operator-full-verified.log
/tmp/phase3-operator-dashboard-verified.log
/tmp/phase3-rearm-production-full-verified.log
```

**September 25 inventory found no `/tmp/phase3-operator*` files in the current
environment.** Counts above are recorded checkpoint evidence, not newly inspected
logs. Do not depend on ephemeral files being preserved or claim a fresh rerun.
None of this accepts the uncommitted UI below.

## Exact uncommitted work inventory

At stop, before this handover was added:

```
 M dashboard/src/api/client.ts
 M dashboard/src/api/hooks.ts
 M dashboard/src/api/types.ts
 M docs/open/2026-09-14-architecture-implementation.md
?? dashboard/src/api/hooks.test.ts
?? dashboard/src/api/resolve-client.test.ts
?? dashboard/src/components/resolve/
```

The resolve directory contains exactly these seven source files, **no tests**:

```
ResolveCommandForm.tsx
ResolveControlView.tsx
model.ts
resolve-control.css
retry-journal.ts
use-resolve-control.ts
view-contract.ts
```

This document and the pause banner are additional documentation changes. Nothing
was committed. `git diff --stat` excludes untracked files: inspect those explicitly.
There is no controller/container wired into the dashboard. `IssueTab.tsx`,
`IssuesTab.tsx`, and `UnblockModal.tsx` remain unchanged. No E2E fixture integration
or UI screenshots were produced for this slice.

### Parent-owned seams already drafted

- `types.ts`: aliases over generated ResolveControl/AvailableControl/Gate/Rearm,
  Command/CommandResult/Guidance schemas, not a second wire model.
- `client.ts`: typed read/command endpoints and explicit status-to-schema maps.
  An unexpected HTTP status or mismatched status/outcome is rejected even on 2xx.
  Shared request boundary parses once, preserves AbortError, handles malformed
  JSON, and extracts FastAPI validation messages without echoing raw inputs.
  `RejectedRequest` distinguishes a presumed definite framework 422 rejection
  from ambiguous invalid response. Its exact classification needs review/tests.
- `hooks.ts`: `usePolled(..., {cache:false})` prevents reuse/storage of global
  successful poll values for authority reads. An effect-local in-flight flag
  serializes interval reads; refresh/key cleanup aborts and ignores old responses.
  Existing API/default cache behavior is retained; this is a shared regression surface.
- `model.ts`: typed draft and operation unions (`idle`, `review`, `sending`,
  `uncertain`, `resolved`, `blocked`); generated-schema validation; pure command
  preparation against offered gate action and matching current intent. Continue
  requires cancelled/superseded intent; cancel requires pending and nonblank reason.
  Cancellation identity differs from its target request ID. Local checks do not
  replace server authority/CAS.
- `view-contract.ts`: pure presentation props and callbacks; no I/O from view/form.
- `retry-journal.ts`: tab-local sessionStorage record, **not workflow authority or
  a queue**. Key `issue-agent.resolve-request.v1:` plus JSON deployment/issue pair.
  Versioned envelope, size cap 262,144 characters, generated validation and issue
  match on decode. Persist/read-back exact command before POST; refuse replacement
  by a different command. Remove only matching acknowledged command, verify removal.
  Malformed/unreadable state blocks actions rather than silently granting a new ID.
  Reload restores uncertain state; no automatic replay. This is same-tab reload
  protection, not cross-tab/closed-tab durable storage.
- `use-resolve-control.ts`: controller hook intended to be mounted with a key by
  deployment AND issue. Restores storage record, polls uncached every five seconds,
  prepares UUID once for explicit review, synchronously locks duplicate submits,
  persists before POST, retains exact command on unknown outcome, acknowledges
  definite outcomes, and changes read key after POST to invalidate old GETs.
  Does not abort an in-flight POST on unmount; result may acknowledge storage in
  background but must not update the wrong mounted scope. This is unverified WIP.

### Presentation drafted by Astra

`ResolveControlView.tsx`, `ResolveCommandForm.tsx`, `resolve-control.css` extend the
existing dark dense operator dashboard tokens and ordinary React/CSS. No assets,
new fonts, framework, dependencies or new visual world were introduced.

The view renders status distinctions, independent gate causes/evidence, current
intent separately from historical lookup, workspace obligations vs live proof,
keep/clear/set guidance, explicit review confirmation, pending cancellation,
continuation without new allowance, and exact-command retry on uncertainty.
Forms hold only local draft input; callbacks delegate command preparation/sending.
Pending intent limits the form to advertised cancellation. Sending/uncertain
operations hide draft forms. Inline headings receive focus on important state
changes; announcements, input labels, details and responsive CSS are drafted but
have not passed browser/accessibility review.

An optional question was asked: inline controls with explicit confirmation, or a
dialog? **No user answer was received.** Inline was the working assumption, not
an approved selection. Do not mistake the tool's preselected option for consent.

### Tests and agent handoff limits

The scoped test agent authored `resolve-client.test.ts` and `hooks.test.ts`.
The latter has five behavioral cases: uncached mount, uncached key change,
serialized intervals, stale changed-key response, stale refreshed response.
Client cases cover encoded identities, exact JSON/CSRF command, typed HTTP outcomes,
framework errors, malformed/mismatched replies and aborts. Inspect full parameterized
cases rather than counting only `it(...)` lines.

Earlier client run reported 24 passing / 8 failing mismatched status-outcome cases;
the parent subsequently added strict status maps. An intermediate typecheck passed,
but that does **not** prove final concurrent edits compile. No final current-tree
test outcome was received before interruption. Old execution session handles are
not durable proof and should not be relied on in a new environment.

The Astra presentation agent had not supplied UI tests or final acceptance. The
independent functional reviewer had been assigned but delivered no findings/final
acceptance before interruption. No live agent remains. Obtain fresh independent
review after resumption; do not label these drafts reviewed.

## Known unfinished seams and review targets

These are handover observations, **not fixes made during the stop**:

1. Confirmed source mismatch: the resolved view offers “Close result”, but hook
   `onDismiss` only clears `review`. That button currently cannot close a resolved
   result. Add an observable regression when implementation resumes.
2. History lookup remains enabled for review/uncertain/blocked view states, but
   the hook refuses lookup while `operationLocksActions` is true. Reconcile the
   UI/controller contract; do not leave enabled actions silently doing nothing.
3. No `ResolveControls` keyed container exists. Without proper issue/deployment
   keying, restored operation/storage state could cross resource navigation.
4. Preparation is locally checked, but confirmation's hook does not itself
   recheck fresh availability. View disables some cases and backend CAS fences
   writes; test status changes between draft/review/submit, including read errors.
5. Test stable payload/ID through network loss, unavailable/malformed responses,
   reload, duplicate click, unmount/remount and storage failures. Verify definite
   vs ambiguous 422 classification cannot discard a possibly accepted command.
6. Test response-loss followed by history lookup, current-vs-requested separation,
   conflict refresh and stale pre-POST GET exclusion. Accepted/replayed must never
   be presented as completed recovery or paid-work readiness.
7. Review sessionStorage envelope validation, tampered scope, size limits,
   replacement/ack races, denied/quota storage, and explicit recovery from corrupt
   records. Never silently delete a record or mint a new ID to escape uncertainty.
8. Legacy endpoint lacks this typed stable-command retry contract. Integration
   must not blindly auto-retry ambiguous legacy failures or claim their completion.
9. Shared request/polling changes need regression coverage beyond resolve; test
   unchanged default caching, errors, visibility changes, StrictMode and aborts.

## Intended integration, still to implement

This was the working direction, not finished code:

- Add one keyed controller in full issue detail near attention/lifecycle information;
  use the drafted hook and presentation contract. Keep API/mutation ownership singular.
- Legacy confirmation only when a fresh authoritative read is explicitly inactive,
  the issue needs human attention, and no read error/locked command exists. Unknown,
  absent, unavailable or active must not expose old unblock as fallback.
- Remove duplicate direct unblock mutation/state from IssueTab after equivalent
  safe behavior exists. Existing `UnblockModal` copy describes legacy behavior;
  it is not appropriate for typed “accepted but pending” re-arm.
- Prefer a board action opening full issue controls through existing `onOpen`,
  instead of a second mutation flow or authority polling in every board row.
  Preserve Park. Update IssueTab/IssuesTab explainability tests for actual outcomes.
- Existing ConfirmModal lacks a robust focus trap. Do not assume wrapping new
  protected workflow in that modal establishes accessible confirmation.

## Resume workflow and acceptance checklist

Only after the user resumes implementation:

1. Re-read instructions and skills, inventory current worktree/agents/goal. Do not
   reset or overwrite WIP. Reconfirm environment, dependencies and source ownership.
2. Use the overall plan and checkpoint to keep this increment bounded. Parent owns
   shared types/ports/client/polling/controller/integration; scoped phase agent owns
   agreed presentation/tests. Include the plan's conventions brief in each prompt.
   User requested Astra phase agents; use less expensive scoped helpers where
   compatible with instructions. Do not fan out conflicting shared-seam edits.
3. First run focused typecheck/tests to establish actual WIP failures, then write
   behavioral model/storage/controller/presentation tests for the targets above.
   Fix root causes rather than introducing another queue, readiness flag or retry ID.
4. Integrate visible controls and legacy authority fencing, update feature-outcome
   tests, and extend isolated E2E fixtures. No live backend proxy or service restart.
5. Validate actual user outcomes in a real browser: accepted but pending, historical
   vs current request, continue/cancel, conflict, response loss/reload/exact retry,
   unavailable/absent no-legacy fallback, and storage failure. Inspect keyboard,
   screen-reader semantics, narrow layout, and long/error content.
6. Obtain independent adversarial functional review and required visual/finish
   review without biasing reviewers toward only the issues listed here. Resolve
   findings; rerun tests on the final frozen tree. Update durable checkpoint evidence.
7. Full `make test` must pass before **any commit**. Commit only coherent accepted
   work: daemons hot-reload checkout HEAD between ticks, even though currently paused.
   Do not commit current WIP based on old checkpoint green results.
8. Continue remaining phase-3 offline activation/import/review-rejection work in
   dependency order. Start phase 4 only after phase 3 acceptance. Keep goal status
   honest; a checkpoint, pause or exhausted context is not completion.

Useful commands on resumption (not run for this handover):

```sh
# In dashboard/
npm run typecheck
npx vitest run src/api/resolve-client.test.ts src/api/hooks.test.ts src/components/resolve/
npm test

# In repository root; Python only through uv
UV_CACHE_DIR=/tmp/uv-cache uv run pytest <focused-test-paths>
make test > /tmp/<unique-final-checkpoint>.log 2>&1
```

Use installed repository tools/scripts. Pyrefly, not mypy/pyright; no pip/poetry.
Inspect dashboard package scripts for build/E2E commands. Generate API artifacts
only if backend wire schemas change, using the existing exporter/generator.
Use `apply_patch` for source edits. Git metadata writes may require escalation in
the current sandbox; source checkout and `/tmp` are writable. No destructive reset.

Playwright is designed around Vite fixture port 4173 and isolated FastAPI fixture
4174. `dashboard/e2e/server.py` currently needs resolve fixture support; inspect
`dashboard/e2e/dashboard.spec.ts`. Set `DASHBOARD_API` explicitly to the fixture
when appropriate; do not accidentally proxy to live port 8321. No live data needed.

## Frontend skill context to preserve

The interrupted UI work used these skills; read their complete current instructions
again when resuming applicable UI work:

```
/home/chong/.codex/skills/frontend-ui-engineering/SKILL.md
/home/chong/.codex/skills/impeccable/SKILL.md
/home/chong/.agents/skills/make-interfaces-feel-better/SKILL.md
```

Existing PRODUCT/DESIGN context was read. The work was treated as a local extension
of the approved operator surface, not a redesign. No image comps were generated.
Follow current skill routing and reassess any required visual evidence rather than
claiming the earlier partial workflow satisfies acceptance. Referenced accessibility
checklist file was missing; core skill accessibility requirements were the fallback.

Impeccable context reported `.impeccable/design.json` stale relative to DESIGN.md.
Do not silently rewrite design documentation; offer the prescribed document repair
if still relevant. No repair was authorized/performed. Browser inspection, detector,
finish review and final skill acceptance were not run. Planned responsive checks:
320, 768, 1024 and 1440 widths, keyboard flow and actual outcome screenshots.

Historical detector entry point, verify before use:
`node /home/chong/.codex/skills/impeccable/scripts/detect.mjs --json <changed-UI-targets>`.
Discover finish-reviewer instructions through the skill rather than guessing an
agent tool or treating absence of one filename as absence of the capability.

## Handoff boundary

Current state is intentionally a **paused, dirty worktree**, not a releasable UI.
No phase was advanced by writing this handover. Preserve all source drafts and
the accepted backend checkpoint. The next authorized implementation step is to
establish WIP test failures and finish the single operator-control integration,
not to restart the Tauri deployment or start phase 4.
