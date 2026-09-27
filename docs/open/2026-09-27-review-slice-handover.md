# Review-rejection slice — handover (increments 1–7 accepted, 8 in progress)

Date: 2026-09-27. Repository: `/home/chong/Repos/autonomous-issue-agent`.
Branch `dev` at the commit that adds this document; last feature commit
**`1cdc834`** (`feat: PR review-control API and dashboard review panel`).
Work-in-progress for increment 8 is on branch **`wip/review-inc8-activation`**
(`40251f3`, based on `1cdc834`). It is untested beyond activation-scoped tests
and is not merged.

## Start here: stop state and authority

The user's latest instruction: **"pls stop, commit wip, write a comprehensive
handover document for the next agent."** Work is stopped, not complete. This
document does not itself authorize resumption. Resume only when the user asks.

- Active goal at stop: «plan is implemented and throoughly tested». Earlier
  wording was "resume implementing the plan and don't stop until it's
  complete". The goal is **not** met: increment 8, phase-3 acceptance and
  phases 4–6 remain.
- No agents or background jobs are running. The increment 8 implementation
  agent was stopped mid-work, and its partial output is the WIP branch.
- Services: read-only check on 2026-09-27 found all four units
  (`issue-agent-{fix-watcher,review-watcher,steward}-xnmp-tauri-explorer`,
  `issue-agent-dashboard`) **inactive and disabled**, and no harness processes.
- `dev` is unpushed and ahead of `origin`. No push was requested.

Read in this order:

1. `CLAUDE.md`, `AGENTS.md`, then this document.
2. [Six-phase plan](2026-09-14-architecture-implementation.md): status, phase table
   and conventions brief.
3. [Review slice plan](2026-09-25-phase-3-review-slice-plan.md). Decisions 1–12
   are binding.
4. [ADR 0015](../adr/0015-generation-fenced-gate-rearm.md): review slice,
   transaction/effect boundary, uncertainty, migration and rollback.
5. `HARNESS.md` sections "Review rejection operator API" and "Resolve family
   activation". Review activation mirrors resolve activation.
6. [Residuals](residuals.md): the review-family rows of the accepted-risk table
   (dismissal after a head move, the bound-submission marker, the
   verdict-escalated release path, hold-fact-only guards and the restore bound).

## Standing constraints (carry into every agent prompt)

- Do not restart live services, mutate the target checkout, buy paid harness
  runs, post issues or PRs, **activate a workflow family on real data**, or
  migrate live authority. Phase-5 live evidence needs explicit user
  authorization and cannot be simulated.
- Preserve spend caps and the trusted-inbound posture.
- Full `make test` must be green before any commit. For dashboard changes,
  `cd dashboard && npm test` and `npm run test:e2e` must also be green. After
  Playwright, run `git checkout -- evidence`.
- Run an independent adversarial review for medium or high-risk changes. Give
  the reviewer the spec sources, never your own suspicions.
- Use `uv` only (never pip, pipenv or poetry); type-check with pyrefly. Never
  hand-edit generated API contracts: use `cd dashboard && npm run generate:api`.
  If it fails offline after a `pyproject.toml` change, run `uv sync` first.
- Schema is **v24 and frozen**. Any schema change is v25, with a migration and
  tests.
- Commit messages end with the Co-Authored-By trailer (see recent commits).
- Tauri decisions to preserve when live work resumes:
  - #696 is blocked pending an interactive Mac.
  - #523 badges are approved.
  - #514: Gravatar only as explicit opt-in.
  - #722 is pasting an image from another app.
  - #723: the `trash:///` gap is recorded, not fixed.
- Subagent hygiene:
  - Never let an agent launch background child agents and then end its turn
    waiting. The results route to the main session, which deadlocks the child.
  - Commands run in the foreground with a timeout.
  - Never poll with `pgrep -f` on a pattern that matches the poller's own
    command line; it loops forever.
  - Worktree agents must verify `git merge-base HEAD dev`.
- **`/tmp` is wiped on reboot.** This session lost every brief, probe and log
  once. Keep anything a later agent needs in the repository (as here) or
  re-derive it.

## What this session delivered (phase 3, review-rejection slice)

| Commit | Increment | Content |
| --- | --- | --- |
| `6905184` | resolve acceptance | Composed resolve fault matrix; resolve slice **accepted** |
| `dd8844d` | docs | Records resolve acceptance; review slice plan |
| `282c320` | 1–2 | Review types (`CountedReview`, `ReviewRejectionCeiling`, continuation `FreshReviewRequired \| ReviewedBaseline`), family-scoped authority, shared commit helpers |
| `313f2a2` | 3 | Pure policy `domain/review_workflow_policy.py`; `validate_review_transition` re-derives every transition from harness-owned `ReviewTransitionFacts` |
| `5327ae6` | 4 | `DISMISS_REVIEW` owned by the review executor (a vanished review is refused, never success; head-fenced); `pull_request_reviews` → `CompleteReviews \| PartialReviews`; bound `SUBMIT_REVIEW` with idempotency marker; family-aware drain |
| `c2f1d67` | 5 | `SQLiteReviewWorkflowControl`: commits re-derive from the stored row; request/continue/cancel re-arm, `authorize_retry` through the shared retry machinery; ceiling open/refresh/`RELEASE_CEILING`; stale-generation command retirement |
| `fadfee6` | 6 | Runtime switch for ACTIVE repos (`compose_review_daemon`, `app/review_runtime.py`); durable consequences decided at receipt settlement; bounded evidence/history reads; fix-role guidance from SQLite; escalation signals claimed once per escalation; composed acceptance suite |
| `1cdc834` | 7 | PR review-control HTTP API and dashboard panel; view renders the daemon's own recorded dispatch decision; shared "holds a re-arm keeps" predicate; `/workflow-families` endpoint |

Test counts at `1cdc834`:

| Suite | Passed |
| --- | --- |
| domain | 2,802 |
| adapters | 2,282 |
| app | 2,832 |
| hooks | 123 |
| governor | 13 |
| vitest | 479 |
| e2e | 21 |

Also at `1cdc834`: 10 strict-xfail residuals (pre-existing), 4/4 import
contracts, pyrefly 0 errors.

Each increment went through implement → unbiased adversarial review → fixes,
repeated until a pass found nothing of MEDIUM or higher. That took two to four
passes, and every pass found real defects.

### Invariants the review slice now guarantees (do not regress)

- **One authority.** For an ACTIVE review family, review dispatch, baselines,
  ceilings, guidance and re-arm live only in SQLite. Every submission is bound;
  the control store refuses an unbound `SUBMIT_REVIEW`. Inactive repositories
  still use the legacy path until increment 8 switches them to
  migration-required.
- **Durable consequences at receipt settlement.**
  - `settle_submission` decides baseline, ceiling gate, escalation hold
    projection and operator signal in one transaction, never on the synchronous
    dispatch path.
  - A lost reply or crash never buys a second review or leaves a silent hold.
  - A verdict ESCALATE records the head as reviewed; the NHR projection is a
    guarded workflow command.
- **Holds are always visible and releasable.**
  - A refused ceiling never gets NHR.
  - An open ceiling restores a removed hold at most `MAX_HOLD_RESTORES=3` times,
    then journals `hold_contested` once.
  - A refused escalation projection is re-planned at most
    `MAX_ESCALATION_REPLANS=2` times, then becomes a `ReviewProjectionRefused`
    gate plus one signal.
- **Bounded loops (ADR 0012).** Unreadable review history, exemption evidence,
  first observation, held-PR re-reads, departed and closed PR reads, and
  dispatch-record failures each have backoff, a ceiling and a journal event.
- **Guards use hold facts only.** Projections are guarded by
  `PULL_REQUEST_HOLD_FACT_LABELS` (lifecycle, trigger, opt-out, trusted holds,
  head, open state). Advisory labels never refuse them.
- **Truthful operator view.**
  - The panel renders the review role's recorded decision (`review-dispatch-decision`
    rows, harness-owned, written after admission as a projection that can never
    block dispatch), with its as-of time.
  - Only MERGED refuses actions. A CLOSED PR keeps its lever, and acceptance
    re-reads GitHub.
  - Effect text and acceptance share `request_rearm_kept_holds` and
    `continue_rearm_kept_holds`.
- **Escalation signals** are claimed once per escalation identity, across owner
  retirement (`_owner_lineage`). Malformed signal rows are quarantined and
  journaled, and never wedge the drain.

## Increment 8 — remaining work (authoritative brief)

This brief was in `/tmp` and is reproduced here verbatim in substance.

### Activation/import (offline, operator-only; mirror resolve activation)

Mirror these resolve activation files and their tests:
- `domain/resolve_activation.py`;
- `adapters/resolve_activation.py`;
- `app/resolve_activation.py`;
- `cli.py` `cmd_resolve_activation` and `_activation_liveness`.

Commands:
- `issue-agent review-activation inventory | shadow | rehearse --copy-to | activate --confirm-plan-digest`.
- Liveness and quiescence refusals are the same as resolve: watchers, fresh or
  unreadable heartbeats, in-flight runs, leases, claims, non-terminal commands,
  active reservations, and unreadable, malformed or duplicated legacy records
  across state roots. `--legacy-state-dir` is supported.

Pure plan, per OPEN PR:
- Import `last_sha` and `patch_id` as `ReviewedBaseline(ImportedBaseline)` only
  when both are present and well formed AND a fresh COMPLETE read shows the
  bot's latest undismissed review at that head. Otherwise the PR gets
  `FreshReviewRequired`; never guess.
- Import guidance verbatim into the episode.
- Import a ceiling only from fresh complete reads under the runtime counting
  rule, and only for PRs the legacy runtime holds at the ceiling (the NHR label,
  or the keifu #173 dead state).
- Closed PRs' files are evidence only (decision 9).

The activation itself:
- The digest covers epoch, sources, inventory and every decision. Any change
  since `shadow` refuses.
- One transaction, with a `workflow_activation` journal entry.
- A replay with the same digest → `already_active_with_this_plan`. Activation
  is never repeated or reversed.
- Shadow shows legacy vs new dispatch per PR, and the paid reviews activation
  would cause (target: none for PRs whose baseline imports).
- Document activating both families together (plan decision 2).

### Switch and delete

- **Inactive review family ⇒ `review_migration_required`.** The role shows it
  on the dashboard and journals it at a bounded interval, and never dispatches
  through the legacy path. Fix-role guidance for an inactive family defers.
- **Delete:**
  - `_rearm_rejections`, `_reset_review_baseline` and `_apply_operator_guidance`
    (`app/dashweb/service.py`), with their routes and UI;
  - `operator_effects.dismiss_review`;
  - `stale_rejections`;
  - the legacy `decide_review` dispatch path;
  - the Store authority for last-sha, patch-id and operator-guidance
    (`get/set_last_sha`, `get/set_last_patch_id`, `get_operator_guidance`, plus
    the port methods). Keep only an adapter-private read-only reader for import.
- **Unproductive-round counter:** migrate it to SQLite, or record why not. It
  still lives in the legacy JSON and feeds `external_holds`.
- Keep `legacy_unblock_authority` refusing when any family is active.
- Annotate ADR 0015 (review slice status) and ADR 0009 (superseded for review).
  Update HARNESS.md with the activation procedure and remove legacy review
  operator text.
- Provide grep proof that no production caller of a deleted API remains.

### Carried from increments 6–7 (binding)

1. **Stale epoch/episode checks.** Review operator commands check them only in
   the HTTP app layer. Enforce them inside `SQLiteReviewWorkflowControl`
   command methods (inside BEGIN IMMEDIATE) and test re-activation replay.
2. **`migration_required` in the inc-7 view** currently means "another family
   is active and review is not". After increment 8 an inactive review family is
   migration-required regardless of other families. Update the view, UI and
   docs.
3. **Pre-existing leak.** When `_admit` raises after a partial reserve, the
   reservation leaks until restart and is now shown as `in_flight`. Admission
   must release partial reservations on failure.
4. **`authorize_retry` read order.** It does its GitHub terminal pre-read
   before the replay and stale-revision checks. Check those first, as
   request/continue do.
5. **Local vs remote read errors.** A local `sqlite3` error during a departed
   PR read is recorded as that PR's failed remote read, with up to 6h backoff.
   Distinguish store-unavailable from a remote failure.
6. **Fix-side exemption UNVERIFIABLE loop.** The merge-collector loop is still
   unbounded. Bound it per ADR 0012 (journal, ceiling, visible gate), or record
   it in `residuals.md` with its trigger.
7. **Import and dispatch records.** Import must not create stale dispatch
   records. The WIP already records none; the daemon records its first from its
   own read.

### State of the WIP branch (`wip/review-inc8-activation`, `40251f3`)

- **`domain/review_activation.py` (705 lines):** the pure plan.
  - Legacy records, open-PR listing (complete or unavailable), baseline and
    ceiling import facts, `ImportedReviewTarget`, `ReviewActivationPlan`.
  - `plan_review_activation`, `imported_snapshot`, digests, and
    `shadow_compare` with per-PR legacy and new dispatch.
  - `legacy_ceiling_hold`, which covers the keifu #173 escalated-at-head case.
- **`tests/domain/test_review_activation_plan.py` (414 lines):** passes.
- **`adapters/review_activation.py` (217 lines):** started. It covers the
  legacy file reader and the barriers/transaction skeleton, using
  `family_inventory` and `persist_workflow_snapshot`. It is not finished or
  tested.
- **`adapters/resolve_activation.py`:** inventory generalised to
  `family_inventory(db, repository, family, activation)`, and
  `copy_for_rehearsal` takes a `legacy_name` pattern. Resolve activation tests
  pass unchanged.
- **`domain/review_workflow_policy.py`:** `review_ceiling_gate(key, cause,
  opened)` is extracted for import.
- **Not started:** app composition, CLI, migration-required switch, deletions,
  docs, adapter and composed tests.

The first step is to review the WIP critically before building on it; it has
had no adversarial review. Then rebase or merge it onto `dev`, verifying the
merge-base, and finish the increment. Full `make test` has **not** been run on
the WIP.

## After increment 8

1. **Accept the review slice and phase 3.** Needs an independent adversarial
   review of the whole slice against ADR 0015 and the plan. Then:
   - ADR 0015 status: review slice accepted at `<commit>`; still no live
     activation;
   - ADR 0009 annotation;
   - plan doc status and the remaining-checkpoints list;
   - residuals.
2. **Phase 4 (continuations).** Migrate the remaining eligibility-bearing
   counters, consumed replies and continuation episodes one family at a time,
   with shadow comparison, scoped epochs, old-writer fencing and rollback
   reconciliation. Retire Store suffixes and temporary flags. Two items are
   already owed:
   - the release path for verdict-escalated PRs (plan decision 3; the
     verdict-escalated row in `residuals.md`);
   - the unproductive-round breaker, if increment 8 does not migrate it.
3. **Phase 5 (measurement).** Offline work (incident records, cohort readouts,
   intervention replay) can proceed. **Live windows require explicit user
   authorization.**
4. **Phase 6 (policy/verification).** See the plan's phase table.

## Known low-severity items (not blocking; decide when touched)

- The escalation alert is at-most-once: a crash between the claim and the
  alert loses the page, but the event survives. This matches legacy.
- The first activation of a PR without an importable baseline buys one fresh
  review per open PR (expected; shadow reports it).
- The worst-case delay before a human dismissal is noticed while a hold is
  unchanged is the backoff maximum (1800s).
- A contested hold stays contested, journal-only, while the PR shows the
  trigger (the hold-fact/restore-bound row in `residuals.md`).
- The e2e review specs use a hand-written fixture double (`dashboard/e2e/server.py`
  `ReviewFixture`). Server/UI refusal drift is caught by
  `tests/app/test_review_operator_api.py`, not by e2e (same as the resolve
  precedent).

## Practical gotchas

- Run pytest per tier (`tests/domain`, `tests/adapters`, `tests/app`) or via
  `make`. A single `pytest tests/ -k …` across tiers hits collection errors
  from duplicate module basenames.
- IDE TypeScript diagnostics seen mid-edit were stale snapshots every time.
  Trust `npx tsc --noEmit`.
- `dashboard/e2e/dashboard.spec.ts` "Verification ledger" now uses
  `locator(".section-title span", { hasText })`. The old `getByText(…, { selector })`
  silently ignored `selector` and matched ambiguously (flaky).
- Session rate limits (HTTP 429) kill agents mid-task. Resume them with a
  message once the limit resets; they keep their context.
- Reviewer prompts that listed "areas to focus" were still effective. Keep
  them free of your own hypotheses about specific bugs.
