# Critique of the 2026-09-14 architectural review and overhaul plan

Reviewed plan: `/tmp/architecture-review-2026-09-14/report.md` (not checked
in; pinned to `d26e935`). This critique was written against `4c365a8` plus the
uncommitted triage-state working tree of 2026-09-14. Findings were verified by
rerunning the review's four reproductions and by independent read-only
inspection of the domain, adapter, app and test tiers.

## Verdict

Adopt the direction. The diagnosis is correct and the remedy is the right
shape. Modify the plan in four places: ship stage 1 now as ordinary fixes,
add a durable-composition test harness before stage 2, supersede ADR 0009
explicitly before stage 3, and cut stage 4 and the diagnostic-leg item down to
what the evidence supports. Hold the overall scope at *fewer state
representations*, not more capability.

## Diagnosis: confirmed

The remaining failure class is one cause, not scattered bugs. Eligibility for
a paid leg is derived from four representations with no joint owner:

1. GitHub labels, read through `domain/lifecycle.py`;
2. per-role counter and marker files, read through `adapters/store.py`;
3. SQLite lifecycle commands and receipts, `adapters/control_store.py`;
4. journal events, folded by the dashboard.

Every reproduction in the review and every re-arm repair in August is an
inconsistency between two of these.

### Reproductions still fire on the current tree

All four scripts under `/tmp/architecture-review-2026-09-14/` reproduce
against `4c365a8` with the working tree applied:

- `repro_lifecycle.py`: issue phase `NEEDS_HUMAN_REVIEW` yet `RunAgent(RESOLVE)`
  emitted; PR phase `BLOCKED` yet `RunAgent(REVIEW)` emitted.
- `repro_corrupt_counter.py`: `resolve-retries` containing `oops` reads as 0
  and grants a fresh resolve.
- `repro_stale_completion.py`: error-path relabel carries
  `expected_revision=None`; the executor binds the post-hold revision; the
  hold label survives beside `ai-ready`; next tick dispatches again.
- `repro_stale_triage_close.py`: `CloseIssue` closes over a hold added during
  the paid leg; remote ends `closed=True` with
  `['ai-closed', 'needs-human-review']`.

### Mechanism, with locations

- `domain/lifecycle.py:42-67` derives phase by precedence, but
  `is_ready`/`is_working`/`is_pr_open` (`:86-100`) and `PrLifecycle.is_blocked`
  (`:127-129`) test raw label membership. `tests/domain/test_lifecycle.py:36-45`
  asserts phase `DONE` together with all three booleans true, so the
  contradiction is a tested property, not an accident.
- `domain/fix.py` selects via `issue_lifecycle(i).is_ready` (around `:1452`,
  `:1466`); `domain/review.py:536-614` selects via `world.prs_with(trigger_label)`.
  Neither consults `.phase`.
- `adapters/effects.py:945-976` (`_command`): when an action has no
  `expected_revision`, the executor fetches the *current* issue `updated_at` or
  PR head at command-build time and uses that as the fence. `_revision_matches`
  (`:1045`) then compares it against another fresh read. For these actions the
  fence compares a value against itself. `_expected_revision` (`:1205-1220`)
  whitelists only `Relabel` and five PR-head actions; `CloseIssue`
  (`domain/actions.py:95-98`) has no revision field at all.
- `app/workflows/fix.py` (~`:599-631`) and `app/workflows/steward.py`
  (~`:575-680`) read the issue before the paid leg and never thread that
  revision into post-leg actions. `domain/outcome.py:548` builds the error-path
  `Relabel` with no revision. The uncommitted steward diff adds
  `issue_state`/`state_reason` to the triage prompt and does not touch this.
- `domain/ports.py:81-89` promises that read and corruption failures propagate
  and fail the workflow. `adapters/store.py:132-176` swallows `OSError` on
  read and remove and `ValueError` on malformed integers, returning the empty
  or zero sentinel. The comment on `_read_int` (`:162-176`) acknowledges a
  torn write resets a tripped breaker to a full budget. Eligibility-bearing
  counters therefore fail *open*.
- ADR 0013 (`docs/adr/0013-human-gate-ledger.md`) remains `proposed`. No
  `gates` table exists in `control_store.py` (full `CREATE TABLE` inventory at
  `:416-687`). The only re-arm implementation is
  `app/dashweb/service.py:683-862` plus `_clear_retry_counters` (`:1642`),
  which is imperative, touches three role state directories directly, and
  journals only after every step has run. Re-escalation dedup still keys on
  `not pr.has(Label.NEEDS_HUMAN_REVIEW)` (`domain/review.py:428-470`, `:580`),
  the mechanism ADR 0013 blames for the PR #173 dead state.
- Successive re-arm repairs: `8b7df3a` (08-02, rejection ceiling),
  `9e70231` (08-04), `5045a44` (08-09, review baseline), `53fa3c9` (08-10,
  resolve episode). Each found one more piece of state the unblock forgot.
- `unknown_outcome` commands retry every drain with no age, backoff or
  poison handling (`adapters/effects.py:380-397`, `:589-620`;
  `domain/effect_retry.py:29-46` excludes them by design). Accepted in
  `docs/open/residuals.md:166-172`. The same log-only no-ceiling shape recurs
  at `residuals.md:192-210`.

### One correction to the review

The review implies competing counter-reset lists. There is one:
`_RETRY_SUFFIXES` at `adapters/store.py:83`, consumed by `Store.clear_retries`,
called only from the dashboard, with `resolve-retries` and `infra-deaths`
handled by two inline exceptions in the same method. The brittleness is
scattered state, not duplicated lists. The conclusion is unchanged.

## Remedy: right shape, already decided once

One validated workflow record in SQLite, a pure transition function, gates as
typed rows with a generation, remote writes as outbox commands. This is ADR
0013 extended to continuation state. The review's contribution is showing
that the gate ledger is not a dashboard convenience but the missing centre of
the eligibility model.

The rejected alternatives are correctly rejected:

- A rewrite discards the parts that work: money (ADR 0002), admission and
  commands (ADR 0003), deployment fencing (ADR 0008), receipts
  (`adapters/durable_effects.py:59-262`), supervision (ADR 0005).
- A workflow engine supplies durable timers and history. The problem is that
  the state machine has never been written down as one thing; no engine
  writes that.
- Module splitting alone leaves all four representations in place.

Migration precedent exists and should be copied:
`docs/reference/control-store-migration-evidence.json` records the
file-to-SQLite spend migration with a shadow-compare phase, digested
evidence, canary scope, and flags retired to `{}` in `migration-flags.json`.
`tests/domain/test_architecture_contracts.py:515` already requires every live
migration flag to name an executioner.

## Required changes to the plan

### 1. Stage 1 is a bug fix. Ship it now, outside the overhaul.

Three changes need no new schema and close the reproduced holes:

- Derive dispatch eligibility from `.phase`, and make `is_ready`-style
  booleans either phase-derived or private. Update
  `test_lifecycle.py:36-45` to assert the contradiction is *rejected*, not
  tolerated.
- Make `Store` reads fail closed for eligibility-bearing values. Distinguish
  absent from unreadable and from malformed; raise on the latter two so the
  poll fails rather than re-arming. Advisory values (`ci_history`, guidance,
  Jira mirror) may keep the lenient path, but the port contract must then say
  which methods are which.
- At completion, re-observe and re-run the hold check before emitting a
  lifecycle disposition. The duplicate-close grooming path already does this;
  make it the shared contract for `resolve` error completion and triage close.
  Add an `expected_revision` field to `CloseIssue` and populate it from the
  fresh read. Do not bind to the pre-leg `updated_at`: the agent's own comments
  bump it and valid completions would be rejected, as the review notes.

### 2. Build the durable-composition harness before stage 2.

`tests/app/test_convergence.py:36,264` runs the real daemon through
`tests/fakes/executor.py` `ImmediateExecutor`, which bypasses the command
store by design. `tests/adapters/test_durable_effects.py` exercises the runner
against SQLite in isolation. `tests/app/test_recovery.py` uses hand-written
fakes. Nothing runs a daemon tick through `DurableEffectRunner` and SQLite,
kills it, and restarts against the same database and GitHub double. Stages 2
and 3 move authority across exactly that seam; without the harness their exit
criteria are unverifiable. Put it in stage 1's exit criteria.

### 3. Stage 3 reverses ADR 0009. Write the superseding ADR first.

ADR 0009 (accepted 2026-08-01) assigns session ids, consumed-reply
timestamps, branch memory and bounded retry counters to the file-backed role
`Store`, and states there is no stateful resume manager that also owns remote
state or money. Moving those into one transaction with gates and outbox is
defensible, but it must land as a new ADR that marks 0009 superseded with the
argument written down. Migrate one counter family at a time, shadow-compare
first, fence old writers, retire suffixes. The review's own evidence on the
217-file cutover (`0965126`) argues against a single stage-3 move.

### 4. Stage 4 is overscoped. Reduce it to the gate stacks.

The capability vocabulary is centralised (`domain/agent_task.py:58`
`RUNTIME_CAPABILITIES`, two consumers). Unsupported role/runtime combinations
already fail before spend: config parse (`app/config.py:1146-1168`) and
import-time registry exhaustiveness (`app/roles.py:1319`, `:1433`, `:1575`).
The three per-role registries are parallel but guarded. The real duplication
is `plugin/hooks/checks/*.sh` versus `domain/gate.py`, and that pair has a
parity suite with golden, absence, mutation and pinned-divergence tests
(`tests/test_gate_parity.py:972-1213`) built after the last drift incident.
Stage 4 should be gate-policy consolidation only, ranked below stage 5.

### 5. Give Finding 4 a home in stage 2.

Ageing an `unknown_outcome` command into an open gate is exactly the shape
the gate table provides. Do the same for the two sibling no-ceiling loops in
`residuals.md:192-210`.

### 6. The operator-action table is a feature roadmap. Gate it on measurement.

The plan grows into a platform: diagnostic leg, provider failover, standing
decision policies, evidence contracts. Each adds representations that must be
kept consistent, which is the disease being treated. The August 20 handover
(`docs/open/2026-08-20-handover-tauri-takeover.md:79-84`) classifies three of
four operator actions as mechanical: a stale CI block reply, a provider
switch, a watcher restart. Those map to the first and third planned items.
The bounded diagnostic leg has the weakest evidence and the largest new
surface, including its own spend accounting and authorisation boundary.
Sequence: build the workflow model, delete the paths it replaces, measure
interventions per merge, then decide which automation to add.

### 7. Name the deployment risk.

SQLite is machine-wide authority (ADR 0008) and daemons hot-reload between
ticks. A gates-table migration reaches every live deployment at once. Schema
changes must be additive, new writers must sit behind a migration flag with an
executioner, and live batches should be paused for the stage-2 and stage-3
cutovers.

## The real risk is scheduling, not design

ADR 0013 went unbuilt for six weeks because supervised batches consumed the
time, and the state model cannot be migrated in the gaps between deployments.
If this plan is adopted it needs a window with no supervised batches. Without
one it will join `docs/open/2026-07-30-architectural-review-and-plan.md` as a
plan of record that operations outran.

## Measurement note

The repo's north star is cost per merged PR. Operator minutes per merge and
interventions per 100 admitted workflows are the right metrics for this work,
but report them beside cost per merge and first-pass approval rate rather than
as the headline, per the guardrail rule in `CLAUDE.md`.
