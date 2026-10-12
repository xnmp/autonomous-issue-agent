# Subtractive refactor audit at origin/dev 8251a37

**I did not write `/tmp/refactor-audit.md`.** My instructions for this run forbid creating any file, including in /tmp and via heredoc. The whole report is below; save it from here.

Everything is read from origin/dev; the primary checkout's `src/`, `tests/`, `pyproject.toml` and `Makefile` match it exactly. Line counts come from `git show origin/dev:<f> | wc -l`. Import edges come from an AST pass over `src/`. Core is 104,285 lines (`src/**/*.py` minus 12,249 dashboard lines), which matches the design doc. Slice 1 (`refactor/slice1-drop-review-tests`) is treated as done: it removes a net 763 src lines.

**Headline:** following the design's slices, about 18.8k core lines survive, plus about 1.2k of new code, so the projection is **about 20.0k against the 15k target**. Closing the gap needs about 5.4k more cuts in the adapters, CLI and money path (§1.6).

---

## 0. Findings before the plan

1. **Dead code at 8251a37:**
   - `adapters/review_execution_transition_proof.py` (54 lines) has no importer and no test anywhere.
   - `domain/review_receipt_attribution.py` (186 lines) is imported only by `tests/domain/test_review_receipt_attribution.py`.
   - The `quota_debits` table is DDL only (`control_store.py:994`).
   - `control_store.reset_rebase_allowance` (`:6861`) is called only from tests (`tests/app/test_pr_repair_workflow.py:859…`).
2. **Slice 1 leftover:** `domain/terminal_output.py` (19 lines) has no src importer on the slice 1 branch; only its test uses it.
3. **The core already imports the dashboard, which the design forbids:**
   - `adapters/journal.py:27` imports `domain.dashboard.ISSUE_NUMBERED`.
   - `app/cli.py:44` imports `STALE_AFTER_S`.
   - `app/cli.py:2654-2655, 2745, 2786, 2849` reach `app.dashboard`, `adapters.dashweb.bundle` and `app.dashweb.server`.
4. **Offline import state leaks into the runtime.** These must be cut in slice 5a:
   - `workflow_codec.py:35` imports `continuation_import`.
   - `workflow_rows.py:43` imports `review_execution_activation.MAX_LEGACY_SQL_TEXT_BYTES`.
   - `domain/review_execution.py:9` imports `review_execution_migration`.
   - `control_store.py:6751, 7351, 7374` read `workflow_activation_imports` at runtime.
   - `retain_deferred_pr_repair` from `adapters/continuation_activation.py` is called at runtime from `control_store.py:6069`, `workflow_rows.py:434` and `review_workflow_control.py:784`.
5. **Today's harness PR identity is not a branch prefix.** `domain/refs.py` `BRANCH_PREFIXES` (feat/fix/chore/…) are shared with human branches in this repo. `fixer_prs` keys on author == FIX_BOT (`domain/fix.py:1081`). See Q1.
6. **Test gates that every slice must keep satisfying:**
   - `tests/domain/test_architecture_contracts.py:566` requires every `domain|adapters|app/*.py` to be named in `docs/reference/architecture.md`.
   - `:579` requires each non-Superseded ADR to be referenced by a file it governs.
   - `:610` checks the CLAUDE.md byte and word counts against `docs/reference/prompt-budget.md`.
   - `make test-config-generated` diffs `config/agent.env.example` and `plugin/hooks/checks/_branch_policy.sh` against generators.
   - `tests/typecheck/port_conformance.py` lists the ports.
   - `tests/test_residuals.py` imports `domain.gate` and `adapters.store`.
   - Deleting dashboard contracts forces a frontend regen (`dashboard/src/api/generated*.ts`, bundle freshness via `adapters/dashweb/bundle.py`).
7. **Schema migrations are digest-pinned** (`_KNOWN_SCHEMA_DIGESTS`, `control_store.py:2396`). Slices 2–4 must not edit v8–v41 and should avoid adding migrations, because each new one is permanent until the cutover. Everything dies at the 5d cutover.

---

## 1. Module table

Element codes: obs (tick/observe), der (derive), act, money, lease, att (attempts), idem (idempotency), sup (supervision), rt (runtime adapter), cfg, cli, dash; "none" means the design does not need it.

Fate is KEEP, SLIM (→ surviving lines) or DELETE. Slice 5 is split into 5a–5d (§3). Slice 8a means the telemetry part of slice 8, which can run early.

### 1.1 `domain/` (87 files; `dashboard.py` is in §1.5)

| path | L | what it does | element | fate | slice |
|---|---|---|---|---|---|
| `__init__.py` | 7 | package doc | — | KEEP | — |
| `actions.py` | 498 | Action ADT (Relabel, PostComment, RunAgent, CI actions, self-clear actions) | act | SLIM →150 | 2, 4, 7, 5d |
| `agent_task.py` | 110 | provider-neutral agent intent | rt | SLIM →90 | 5d |
| `checks.py` | 61 | immutable CI evidence | obs | KEEP | — |
| `ci.py` | 233 | CI reduction (pending outranks failure, required checks) | der | SLIM →140 | 5b |
| `ci_history.py` | 148 | per-issue CI failure history for FIX_CI prompts | none | DELETE | 5b |
| `cohort.py` | 379 | measurement cohort arithmetic | none | DELETE | 8 |
| `continuation_components.py` | 1825 | issue-recovery, PR-repair and steward component state, admissions, charges | none | DELETE | 2, 3, 7 trim it; 5d deletes |
| `continuation_import.py` | 526 | offline fix/steward import decisions | none | DELETE | 5a |
| `contracts.py` | 460 | Money/usage/settlement/BudgetScope, LifecycleCommand/EffectReceipt, TestExecution | money, idem | SLIM →250 | 6, 5d |
| `deployment.py` | 8 | deployment identity | cfg | KEEP | — |
| `effect_retry.py` | 77 | ceiling on re-running a durable effect | none | DELETE | 5d |
| `effort.py` | 181 | reasoning effort and difficulty ladder | rt (part) | SLIM →60 | 5b |
| `eligibility.py` | 85 | action eligibility (completion guards) | none | DELETE | 5d |
| `environment.py` | 157 | environment manifest for evidence prompts | none | DELETE | 6 |
| `events.py` | 790 | schema-v2 telemetry envelope and codecs | telemetry | SLIM →150 | 2, 3, 7, 5d, 8 |
| `fix.py` | 2100 | `decide_fix`, holds, breakers, CI remedies | der | SLIM →300 (to policy.py) | 2, 3, 4, 5b |
| `gate.py` | 898 | PR/commit/push gate policy (TDD red/green, evidence, ADR) | none | DELETE | 6 |
| `git_remote.py` | 53 | configured remote identity | rt | KEEP | — |
| `governor_policy.py` | 948 | Bash governor parser | none | DELETE | 6 |
| `identity.py` | 90 | `login_eq`, identity modes | der | SLIM →70 | 5c |
| `infra_breaker.py` | 252 | deployment-wide infra-death breaker | none | DELETE | 3 |
| `jira.py` | 170 | Jira sync decisions | none | DELETE | 7 |
| `labels.py` | 124 | 16 lifecycle labels plus priority labels | labels | SLIM →35 | 4, 7 |
| `leg.py` | 32 | leg description | rt | KEEP | — |
| `lifecycle.py` | 374 | label-derived phases, RetryBreaker, escalations | none | DELETE | 4, 5d |
| `liveness.py` | 162 | stall-detection reporter | telemetry | DELETE | 8a |
| `measurement.py` | 421 | measurement facts | telemetry | DELETE | 8 |
| `model_routing.py` | 35 | per-tier provider/model routes | none | DELETE | 5b |
| `outcome.py` | 859 | leg-result classification and follow-ups | outcome | SLIM →300 | 2, 3, 6, 7, 5b/5c |
| `ports.py` | 468 | core Protocols | seam | SLIM →150 | 3, 7, 8, 5d |
| `pr_body.py` | 130 | PR body linkage and acceptance criteria | act | SLIM →100 | 6 |
| `pricing.py` | 250 | rates and budget policy | money | KEEP | — |
| `projection.py` | 253 | telemetry fold into a read model | dash | DELETE | 8 |
| `recovery.py` | 93 | recovery trigger policy | sup | KEEP | — |
| `refs.py` | 39 | branch↔issue mapping, governed prefixes | der | KEEP (prefix: Q1) | — |
| `repository.py` | 49 | repository identity | cfg | KEEP | — |
| `reservation_recovery.py` | 193 | reservation recovery decision table | money | SLIM →160 | 5d |
| `resolve_activation.py` | 582 | resolve-family activation planning | none | DELETE | 5a |
| `resolve_rearm.py` | 264 | resolve re-arm scheduling | none | DELETE | 5b |
| `resolve_return.py` | 167 | unclassified resolve returns | none | DELETE | 5b |
| `resolve_split.py` | 312 | author/implement split provenance (TDD) | none | DELETE | 6 |
| `resolve_workspace.py` | 186 | resolve workspace obligations | none | DELETE | 5b |
| `review.py` | 785 | review admission, verdict synthesis, rejection counting, multi-review | der | SLIM →280 | 1 (+30), 5c |
| `review_activation.py` | 830 | review-family activation planning | none | DELETE | 5a |
| `review_control.py` | 414 | operator review-hold projection | none | DELETE | 1, 5c |
| `review_dispatch_record.py` | 393 | recorded review dispatch decision | none | DELETE | 5c |
| `review_evidence.py` | 209 | bounded reads for review dispatch | none | DELETE | 1, 5c |
| `review_execution.py` | 1280 | review execution episode and rounds | none | DELETE | 5c |
| `review_execution_activation.py` | 1322 | legacy review-execution sources | none | DELETE | 5a |
| `review_execution_counter_mapping.py` | 216 | legacy counter attribution | none | DELETE | 5a |
| `review_execution_death_import.py` | 135 | legacy death import | none | DELETE | 5a |
| `review_execution_history_import.py` | 362 | pre-activation history classification | none | DELETE | 5a |
| `review_execution_import_plan.py` | 295 | import screening | none | DELETE | 5a |
| `review_execution_migration.py` | 85 | blocked-migration type (used in `review_execution.py:9`) | none | DELETE | 5a |
| `review_execution_session_import.py` | 81 | legacy session classification | none | DELETE | 5a |
| `review_linkage.py` | 57 | issue holds feeding PR review eligibility | none | DELETE | 5c |
| `review_preflight.py` | 110 | round selection | none | DELETE | 5c |
| `review_rearm.py` | 397 | review re-arm and control model | none | DELETE | 5c |
| `review_receipt_attribution.py` | 186 | receipt attribution; test-only | dead | DELETE | 2 |
| `review_rejection.py` | 236 | rejection ceiling facts | none | DELETE | 5c |
| `review_return.py` | 113 | unclassified review output | none | DELETE | 5c |
| `review_rounds.py` | 723 | outer-round finalization | none | DELETE | 3 (strike rule), 5c |
| `review_test_evidence.py` | 268 | test receipts | — | DELETED | 1 |
| `review_workflow_policy.py` | 2040 | review-rejection transitions | none | DELETE | 1, 5c |
| `review_workflow.py` | 951 | review episode, continuation and re-arm values | none | DELETE | 1, 5c |
| `run_usage.py` | 103 | usage within a reservation | money | KEEP | — |
| `self_clear.py` | 343 | machine/budget block self-clear | none | DELETE | 2 |
| `steward.py` | 158 | groom dispositions | none | DELETE | 7 |
| `steward_stall.py` | 149 | steward stall ceiling | none | DELETE | 7 |
| `supervision.py` | 47 | supervision vocabulary | sup | KEEP | — |
| `terminal_output.py` | 19 | ANSI strip; orphaned by slice 1 | dead | DELETE | 2 |
| `triage.py` | 566 | steward triage | none | DELETE | 7 |
| `trust.py` | 44 | untrusted prompt content | rt | KEEP | — |
| `workflow.py` | 1408 | resolve workflow, gates, intents | none | DELETE | 5d |
| `workflow_identity.py` | 185 | workflow keys, fences, families | none | DELETE | 5d |
| `workflow_policy.py` | 955 | resolve transitions, including contract adjudication | none | DELETE | 6 (adjudication), 5d |
| `workflow_ports.py` | 219 | workflow Protocols | none | DELETE | 5d |
| `workflow_reconciliation.py` | 241 | reconciliation scheduling | none | DELETE | 5d |
| `workflow_retry.py` | 347 | retry authorization | none | DELETE | 5d |
| `world.py` | 513 | WorldState/PRView/IssueView, probe signals, path overlap | obs/der | SLIM →300 | 5b |

### 1.2 `adapters/` (73 files; `dashweb/` is in §1.5)

| path | L | what it does | element | fate | slice |
|---|---|---|---|---|---|
| `__init__.py` | 9 | doc | — | KEEP | — |
| `accounting_runtime.py` | 1167 | reserve-before-run boundary; repair charges; machine/budget block recording (`:590-610`, `:886-897`) | money | SLIM →470 | 2, 3, 5b/5d |
| `agent_publish.py` | 1817 | durable publication broker for agent push/PR | act/idem | DELETE (Q11) | 6 |
| `agent_runner.py` | 982 | Claude SDK runner | rt | SLIM →600 | 6, 5d |
| `app_token.py` | 176 | GitHub App token mint | cfg | KEEP | — |
| `child_env.py` | 118 | child environment scrubbing | sup | SLIM →100 | 6 |
| `claude_profile.py` | 140 | GLM through a Claude profile | rt | KEEP (Q12) | — |
| `codex_child_usage.py` | 621 | Codex native-child usage | money/rt | SLIM →450 | 8 |
| `codex_hook.py` | 281 | Codex governor hook installer | none | DELETE | 6 |
| `codex_runner.py` | 2561 | Codex CLI runner | rt | SLIM →1500 | 6, 5d, 8 |
| `continuation_activation.py` | 310 | stopped-source import publication | none | DELETE | 5a |
| `continuation_run_import.py` | 216 | stopped SQL evidence import | none | DELETE | 5a |
| `control_path.py` | 94 | control DB path scope | cfg | SLIM →70 | 5d |
| `control_store.py` | 11786 | SQLite authority (§2) | money/lease/idem | SLIM →1400 (becomes `store.py`) | 2, 3, 7, 8, 5a–5d |
| `deployment_authority.py` | 180 | deployment fencing (ADR 0008) | none | DELETE (Q17) | 5d |
| `durable_effects.py` | 287 | command/receipt coordinator | idem | SLIM →100 | 5d |
| `effects.py` | 2499 | GitHub lifecycle command executor | act | SLIM →350 | 1, 2, 4, 7, 5b–5d |
| `env_file.py` | 76 | env-file loader | cfg | KEEP | — |
| `environment.py` | 68 | environment manifest probe | none | DELETE | 6 |
| `event_log.py` | 569 | schema-v2 telemetry writer | telemetry | SLIM →100 | 8 |
| `gate_transport.py` | 34 | shell gate composition | none | DELETE | 6 |
| `github_ci.py` | 198 | revision-bound required checks | obs | SLIM →120 | 5b |
| `github.py` | 2440 | `gh` reads/writes, world builders | obs/act | SLIM →850 | 4, 7, 5b |
| `governor.py` | 913 | in-process governor | none | DELETE | 6 |
| `handover.py` | 356 | exhausted-leg handover | rt | SLIM →150 | 3 |
| `heartbeat.py` | 48 | scheduler heartbeat read | sup | KEEP | — |
| `journal.py` | 1490 | legacy JSONL readers and run telemetry | telemetry | SLIM →100 | 8 |
| `measurement.py` | 293 | measurement persistence | none | DELETE | 8 |
| `operator_effects.py` | 794 | CLI/dashboard/webhook durable effects | act/cli | SLIM →200 | 4, 8, 5d |
| `process_supervisor.py` | 851 | process-group ownership and teardown | sup | SLIM →650 | 5d |
| `reload.py` | 99 | hot reload between ticks | sup | KEEP | — |
| `repository_guidance.py` | 148 | target-repo guidance as task data | rt | SLIM →120 | 5d |
| `resolve_activation.py` | 445 | offline resolve import | none | DELETE | 5a |
| `review_activation.py` | 286 | offline review import | none | DELETE | 5a |
| `review_dispatch_holds.py` | 75 | review dispatch holds | none | DELETE | 5c |
| `review_effects.py` | 376 | review lifecycle commands | none | DELETE | 1, 5c |
| `review_execution_activation.py` | 1478 | legacy review file inventory | none | DELETE | 5a |
| `review_execution_claims.py` | 102 | parent-claim proof | none | DELETE | 5c |
| `review_execution_control.py` | 1331 | review execution preflight | none | DELETE | 1, 3, 5c |
| `review_execution_counter_links.py` | 72 | legacy counter linkage | none | DELETE | 5a |
| `review_execution_import_commit.py` | 102 | import commit | none | DELETE | 5a |
| `review_execution_import_observation.py` | 87 | import source read | none | DELETE | 5a |
| `review_execution_import_replay.py` | 344 | import retry | none | DELETE | 5a |
| `review_execution_import_sources.py` | 304 | import sources | none | DELETE | 5a |
| `review_execution_transition_proof.py` | 54 | dead: no importer, no test | dead | DELETE | 2 |
| `review_execution_workflow_census.py` | 187 | import census | none | DELETE | 5a |
| `review_linkage.py` | 66 | issue/SQLite join for review | none | DELETE | 5c |
| `review_round_sources.py` | 109 | round reconstruction | none | DELETE | 5c |
| `review_test_execution.py` | 245 | review test execution | — | DELETED | 1 |
| `review_workflow_control.py` | 3010 | review-rejection SQLite transitions | none | DELETE | 1, 5c |
| `runtime_plan.py` | 225 | runtime launch contract | rt | SLIM →180 | 5d |
| `runtime_selection.py` | 46 | provider stack selection | rt | KEEP | — |
| `single_instance.py` | 135 | one daemon per checkout and role | lease/sup | KEEP | — |
| `spend_recovery.py` | 214 | Codex spend recovery after crash | money | SLIM →180 | 5d |
| `sql_read_budget.py` | 220 | checkpoint/audit read bounds | none | DELETE | 5d |
| `store.py` | 319 | advisory files (predicted paths, CI history, infra deaths, stall markers, Jira, test_cmd) | none | DELETE | 3, 6, 7, 5b |
| `supervised_exec.py` | 213 | durable process-group registration | sup | KEEP | — |
| `systemd_notify.py` | 65 | sd_notify | sup | KEEP | — |
| `typed_journal.py` | 97 | schema-v2 sink | telemetry | DELETE | 8 |
| `untrusted.py` | 17 | compatibility re-export (only `handover.py` imports it) | dead | DELETE | 2 |
| `wake.py` | 78 | webhook wake file | none | DELETE | 5b |
| `workflow_codec.py` | 492 | tagged workflow codec | none | DELETE | 1, 2, 5d |
| `workflow_commit.py` | 310 | workflow compare-and-swap helpers | none | DELETE | 5d |
| `workflow_control.py` | 1844 | resolve workflow transitions | none | DELETE | 5d |
| `workflow_rows.py` | 1752 | workflow reads | none | DELETE | 5d |
| `workspace_admission.py` | 74 | pristine first-entry check | none | DELETE | 5b |
| `worktree.py` | 1965 | worktrees, contract checkout, gate context (`:1665`), `revert_and_run` (`:1870`) | rt | SLIM →700 | 6, 5b |

### 1.3 `app/` (49 files, plus the root `src/issue_agent/__init__.py`, 10 lines, KEEP; `dashboard.py` and `dashweb/` are in §1.5)

| path | L | what it does | element | fate | slice |
|---|---|---|---|---|---|
| `__init__.py` | 5 | doc | — | KEEP | — |
| `cli.py` | 3663 | operator CLI: init, start/stop, status, doctor, ready, triage, activation (`:3414`), test-cmd discovery (`:1124-1500`) | cli | SLIM →1100 | 6, 7, 8, 5a, 5d |
| `codex_resolution.py` | 350 | which `codex` binary services run | cli | SLIM →200 | 5d |
| `config.py` | 1805 | env → HarnessConfig (about 130 knobs) | cfg | SLIM →600 | all slices |
| `continuation_activation.py` | 295 | offline fix/steward import | none | DELETE | 5a |
| `dispatch.py` | 581 | RunAgent → leg; death keys (`:372`) | act | SLIM →250 | 3, 7, 5b |
| `execute.py` | 1013 | Action → adapter calls; self-clear handlers | act | SLIM →300 | 2, 7, 8, 5d |
| `gate_check.py` | 67 | shell gate entry | none | DELETE | 6 |
| `identity_pools.py` | 414 | identity/rate-pool doctor | cli | SLIM →200 | 7, 5d |
| `infra_breaker.py` | 169 | breaker holder | none | DELETE | 3 |
| `launchd_main.py` | 39 | launchd entry | sup | KEEP | — |
| `loop.py` | 909 | Daemon observe→decide→execute, shutdown | obs/sup | SLIM →450 (becomes `tick.py`) | 3, 5d |
| `main.py` | 508 | role entry point and adapter wiring | sup | SLIM →300 | 6, 7, 5d |
| `mcp.py` | 103 | FIX_MCP_CONFIG for CI-fix legs | none | DELETE (Q13) | 5b |
| `measurement.py` | 85 | measurement CLI | none | DELETE | 8 |
| `planners/__init__.py` | 1 | — | — | KEEP | — |
| `planners/run.py` | 149 | plan composition | rt | SLIM →100 | 5d |
| `planners/schemas.py` | 251 | structured-output schemas | outcome | SLIM →120 | 6, 7, 5c |
| `planners/tasks.py` | 1990 | prompt builders (resolve, author, implement, adjudicate, review modes, triage, groom, jira) | rt | SLIM →700 | 6, 7, 5b, 5c |
| `recovery.py` | 182 | boot/wake recovery coordinator | sup | SLIM →150 | 5d |
| `resolve_activation.py` | 285 | offline resolve activation | none | DELETE | 5a |
| `resolve_control.py` | 111 | resolve re-arm composition | none | DELETE | 5b |
| `review_activation.py` | 353 | offline review activation | none | DELETE | 5a |
| `review_control.py` | 52 | review re-arm composition | none | DELETE | 5c |
| `review_execution_activation.py` | 496 | offline execution activation | none | DELETE | 5a |
| `review_execution_import.py` | 293 | offline import composition | none | DELETE | 5a |
| `review_execution_import_remote.py` | 76 | offline import reads | none | DELETE | 5a |
| `review_identity.py` | 64 | review GitHub identity | cfg | KEEP | — |
| `review_runtime.py` | 763 | active review-family runtime | none | DELETE | 1, 5c |
| `roles.py` | 3847 | composes three daemons: `_fix_state` (`:1686`), reporters (`:1948-2438`), `compose_fix` (`:2469`), `compose_review` (`:3085`), steward (`:3505`) | wiring | SLIM →450 | 1, 2, 3, 6, 7, 8a, 5b–5d |
| `service_doctor.py` | 344 | installed-service doctor | cli | SLIM →150 | 7, 5d |
| `webhook.py` | 142 | `gh webhook forward` wake process | none | DELETE | 5b |
| `workflows/__init__.py` | 1 | — | — | KEEP | — |
| `workflows/base.py` | 40 | single leg path | rt | KEEP | — |
| `workflows/completion.py` | 47 | completion decisions | none | DELETE | 5d |
| `workflows/fix.py` | 1999 | fix legs; split/implement loop (`:1137-1376`); infra charge (`:1376`) | act | SLIM →500 (becomes `app/fix.py`) | 3, 6, 5b |
| `workflows/jira.py` | 313 | Jira sync | none | DELETE | 7 |
| `workflows/resolve_rearm.py` | 190 | resolve re-arm workflow | none | DELETE | 5b |
| `workflows/review.py` | 958 | single/delegated/multi review (`:709` multi) | act | SLIM →330 (becomes `app/review.py`) | 1, 6, 5c |
| `workflows/steward.py` | 686 | steward workflow | none | DELETE | 7 |
| `world_probe.py` | 175 | ETag change probe | none | DELETE | 5b |
| `world_reporter.py` | 267 | change-only world events | telemetry | DELETE | 8a |

### 1.4 `plugin/` (live; outside the src count)

- **Python and shell, 453 lines, all DELETE in slice 6:** `bash_governor.py` (334), `checks/*.sh` (72), `dispatch.sh` (17), `edit_check.sh` (38), two `__init__.py` (2).
- `settings.json` is dead on the SDK path (`planners/tasks.py:88`): DELETE in slice 6.
- `AGENT.md` and `REVIEW_PROMPT.md` are prompts loaded by `app/main.py:117,133`. KEEP; the TDD guidance moves here in slice 6.

### 1.5 Dashboard (separate component, 12,249 lines, outside the core budget)

| path | L | what it does | fate | slice |
|---|---|---|---|---|
| `domain/dashboard.py` | 2442 | pure lifecycle and metric model over journal events | SLIM →600 (rebuilt over derived state plus legs) | 8 |
| `app/dashboard.py` | 643 | journal collection, text/HTML render | SLIM →150 | 8 |
| `app/dashweb/__init__.py` | 24 | doc | KEEP | — |
| `app/dashweb/contracts.py` | 530 | HTTP response contracts | SLIM →250 | 8 |
| `app/dashweb/guidance.py` | 54 | operator guidance for typed re-arm commands | DELETE (forced) | 5b |
| `app/dashweb/ports.py` | 64 | HTTP ports | SLIM →40 | 5b/5c/8 |
| `app/dashweb/resolve_contracts.py` | 394 | resolve re-arm wire contracts | DELETE (forced) | 5b |
| `app/dashweb/resolve_control.py` | 169 | resolve re-arm projection and commands | DELETE (forced) | 5b |
| `app/dashweb/review_contracts.py` | 528 | review re-arm contracts | DELETE (forced) | 5c |
| `app/dashweb/review_control.py` | 348 | review re-arm projection and commands | DELETE (forced) | 5c |
| `app/dashweb/server.py` | 574 | FastAPI routes (resolve/review-control `:296-372`, start `:281`, cost chart `:266`) | SLIM →300 | 5b, 5c, 8 |
| `app/dashweb/service.py` | 1447 | service façade, park/priority writes | SLIM →400 | 8 |
| `app/dashweb/snapshots.py` | 3106 | journal-side collectors | SLIM →600 | 8 |
| `adapters/dashweb/__init__.py` | 1 | doc | KEEP | — |
| `adapters/dashweb/actions.py` | 129 | "start deployment" action (not a label change) | DELETE | 8 |
| `adapters/dashweb/build.py` | 103 | frontend build publication | KEEP | — |
| `adapters/dashweb/bundle.py` | 266 | SPA freshness contract | SLIM →200 | 8 |
| `adapters/dashweb/costchart.py` | 94 | operator's cost-chart script | DELETE | 8 |
| `adapters/dashweb/ghdata.py` | 251 | cached `gh` reads | SLIM →120 (reuse core observe) | 8 |
| `adapters/dashweb/system.py` | 465 | fs/process/git readers | SLIM →200 | 8 |
| `adapters/dashweb/usage.py` | 443 | provider headroom meters | DELETE (Q19) | 8 |
| `adapters/dashweb/verification.py` | 174 | field-verification ledger parser | DELETE | 8 |

- **Projection:** about 3.0k lines.
- **Forced earlier cuts:** slice 5b removes 617 lines and slice 5c removes 876, because those files import the workflow model. The frontend `dashboard/src/components/{resolve,review}/*`, the e2e specs `resolve-control.spec.ts` and `review-control.spec.ts`, and `generated*.ts` change with them.
- **Imports into the core to drop:** the dashboard imports `domain.fix.harness_merges_branch`, `domain.effort.TIER_LADDER`, `domain.outcome.ExitReason`, `adapters.store.Store` and `adapters.operator_effects`; each of these goes when its slice deletes the symbol.
- **Slice 8 adds** an import-linter "core must not import dashboard" contract and fixes the edges in §0.3.

### 1.6 Totals

**By fate (core, 191 files, 104,285 lines):**

| fate | files | lines | surviving lines |
|---|---|---|---|
| KEEP | 29 | 1,953 | 1,953 |
| SLIM | 51 | 55,862 | about 16,825 |
| DELETE | 111 | 46,470 | 0 |
| **Total** | 191 | 104,285 | **18,778** |

**By slice (core lines removed, estimated):**

| slice | removed | new | net |
|---|---|---|---|
| 1 (done) | 763 | — | −763 |
| 2 | 1,669 | +30 | −1.6k |
| 3 | 1,607 | +80 | −1.5k |
| 4 | 560 if run before 5 (about 1.5k more label code dies inside 5) | +40 | −0.5k |
| 5 | 61,418 (5a −10k, 5b −8k, 5c −16k, 5d −27k) | +900 | −60.5k |
| 6 | 8,113, plus 453 in `plugin/` | +80 | −8.0k |
| 7 | 5,039 | 0 | −5.0k |
| 8 | 6,338 core, plus about 7.8k dashboard | +100 | −6.2k |

**Projection:** 18.8k carried plus about 1.2k new is **about 20.0k, roughly 5k over 15k.**

**Where the excess sits:** in SLIM survivors that no design slice targets:

| file | surviving lines |
|---|---|
| `codex_runner` | 1,500 |
| `control_store` → `store` | 1,400 |
| `cli` | 1,100 |
| `github` | 850 |
| `worktree` | 700 |
| `planners/tasks` | 700 |
| `process_supervisor` | 650 |
| `agent_runner` | 600 |
| `config` | 600 |
| `workflows/fix` | 500 |
| `accounting_runtime` | 470 |
| `codex_child_usage` | 450 |
| `loop` | 450 |
| `roles` | 450 |

**Cuts that close it (about −5.4k; fold into 5d and 8, or add a slice 9 "adapter trim"):**

| # | cut | lines |
|---|---|---|
| 1 | Codex: runner →1,000; child usage →150 (disable native sub-agents, drop cost-blindspot / budget-not-enforced warnings and oversize-prompt routing) | −800 |
| 2 | CLI/doctor: cli →550; `identity_pools` + `codex_resolution` + `service_doctor` →200 total | −900 |
| 3 | Store →950: caps as SUM over legs, so no `reservation_limits` or `reservation_recovery` tables | −450 |
| 4 | Money path: `accounting_runtime` + `spend_recovery` + `reservation_recovery` →550 | −260 |
| 5 | One tick process instead of per-role composition: `loop` + `main` + `roles` →800 | −400 |
| 6 | Prompts: move prompt text into `plugin/*.md` templates (not src), `tasks` →350; `schemas` →80 | −390 |
| 7 | `effects` + `durable_effects` + `operator_effects` →350 | −300 |
| 8 | `worktree` →400 | −300 |
| 9 | `config` →350 | −250 |
| 10 | `github` →600 | −250 |
| 11 | `process_supervisor` →450, `supervised_exec` →150 | −260 |
| 12 | Telemetry: `events` + `event_log` + `journal` →120 | −230 |
| 13 | `agent_runner` →450 | −150 |
| 14 | `handover` →0 (resume through session id) | −150 |
| 15 | Drop GLM `claude_profile`, `runtime_selection`, `reload`, `launchd_main` (Q12) | −324 |

Result: about 14.6k.

**Tests:** 149,557 lines now. Whole-file deletions are about 1.3k (slice 2), 1.3k (3), 0.6k (4), 75k (5), 9.8k (6), 4.1k (7) and 15k (8). Rewrites of `test_control_store`, `test_fix_workflow`, `test_roles` and `test_effects` are needed to reach 20k.

---

## 2. SQLite (`control_store.py`, SCHEMA_VERSION 41 at `:114`)

**Tables mapped to the design's 4 tables:**

| table (DDL line) | today | design |
|---|---|---|
| `schema_migrations` (851) | migration history and digests | DELETE; use `PRAGMA user_version` |
| `runs` (857), plus v19/v20/v25/v36 columns `workflow_binding_json`, `workflow_episode_key`, `review_round_id`, `review_child_id`, `continuations_json` | leg identity and state | legs+money (base columns only) |
| `leases` (871) | pid/pgid/start ticks per run | leases |
| `dedupe_keys` (882) | namespaced claims (below) | idempotency (few keys survive) |
| `level_state` (892) | `holds`, `review_fix_hold`, `fix-admission-refusals` (`:4843`) | DELETE (3, 8a) |
| `lifecycle_commands` (901), plus `refusals`, `workflow_*_json`, `attempt_owner`, `prewrite_read_json` | durable remote commands | idempotency (key, kind, target, digest, state) |
| `effect_receipts` (922) | command receipts | idempotency (merged into the same row) |
| `budget_reservations` (936), plus `workload_repository` | reservation per run | legs+money (`reserved_micros`) |
| `reservation_limits` (962) | per-scope caps per run | DELETE; caps are a SUM over legs by period |
| `charges` (978), plus `origin` | settlement | legs+money (`charged_micros`, basis, usage, receipt id) |
| `quota_debits` (994) | unused (DDL only) | DELETE |
| `event_outbox` (1002) | telemetry outbox | DELETE (8) |
| `world_observations` (1013) | `world_reporter` dedupe | DELETE (8a) |
| `migration_imports` (1022) | legacy ledger import (`import_legacy_spend` `:11148`) | DELETE (5d) |
| `run_cancellation_intents` (1034) | cancel reason, handover flag | legs (`end_reason`) |
| `dispatch_claims` (1046), plus `occurrence_id` | per-target claim | leases |
| `dispatch_claim_runs` (1087) | claim↔run binding | leases (`leg_id` column) |
| `retired_owners` (1104) | repoint lineage | DELETE |
| `reservation_recovery` (1122) | spawn_pending/running/reconcile_only | legs (`state` enum) |
| `workflow_authority_epochs`, `workflows`, `workflow_gates`, `workflow_rearms`, `workflow_rearm_commands`, `workflow_evidence`, `workflow_retry_authorizations` (v19 `:1214-1305`, v23) | ADR 0015 model | DELETE (5d) |
| `workflow_activation_imports`, `workflow_activation_evidence` (v22 `:1557`) | import decisions | DELETE (5a stops reading, 5d drops) |
| `workflow_components` (v25/v36) | component counters (become attempts) | DELETE |
| `review_execution_rounds` (v25 `:1658`) | review rounds | DELETE |

The v27–v34 `review_archive_*` tables are already dropped. About 45 triggers and all partial indices die with their tables.

**Dedupe namespaces (`dedupe_keys`):**

- **Survive as idempotency, renamed:**
  - one key per submitted review, `review:{pr}:{head}` (replaces `review-recorded-submission`, `review-round-submission*` and `review-submission-settled`);
  - one per harness comment, `comment:{target}:{n}:{reason}:{head}`;
  - one per merge or PR-open, if the broker goes (Q11).
- **Become leg columns:** captured leg results `pr-repair-result`, `continuation-return` and `workflow-return*` become `legs.result_json`; `dispatch-claim-lifecycle` becomes leases.
- **DELETE in 5:**
  - workflow-*: `workflow-admission`, `-work-latest`, `-contract-reset(-completed)`, `-split-consumed`, `-return-disposition`, `-return-command`, `-return-observation`, `-request`, `-cancellation`, `-result`, `-rearm-observation`, `-workspace-completed`;
  - `continuation-admission`, `continuation-deferred-pr`;
  - review-execution-*: `review-execution-admission`, `-child-admission`, `-launch`, `-return`, `-refusal`, `-release`, `-preflight-stop`, `-origin`, `-checkpoint`, `-generation`;
  - review-round-*: `review-round-preflight`, `-stage`, `-seal`, `-abandonment`, `-outcome`, `-preparation`, `-stale-productivity`, `-superseded-productivity`, `-ceiling-productivity`, `-recovery-wait`, `-recovery-block`;
  - other review keys: `review-hold-observation`, `review-first-observation`, `review-hold-restores`, `review-escalation-resolved`, `review-dispatch-decision`, `review-release-resolved`, `review-activation-plan`;
  - resets and no-ops: `ci-episode-reset`, `rebase-allowance-reset`, `issue-reply-noop`, `pr-reply-noop`.
- **DELETE in 2:** `fix-budget-refusal`.
- **DELETE in 3:** the `fix-infra-outcome` and `fix-admission-refusals` ledger.
- **DELETE in 4/8:** `review-escalation-signal`, `-command`, `-owed`, `-quarantined` (`:452-464`, used for the #339 board).
- **DELETE in 7:** `groomed`, `triage_close`, steward `recurrence`.
- **DELETE in 8/8a:** `measurement-observation`; roles telemetry keys `path-prediction`, `ci-pending`, `duplicate-pr`, `ci-flake`, `recurrence` (`roles.py:1654-2414`).
- **Already gone in slice 1:** `review-round-test-*`.

**Target schema (about 60 lines of DDL):**

```
legs(leg_id PK, item_kind, item_number, head_sha, act, runtime, model, owner,
     state∈{reserved,running,ended,released}, end_class∈{ok,attempt,machine,budget,asked,cancelled},
     end_reason, started_at, ended_at, session_id, reserved_micros, charged_micros, basis,
     usage_json, rate_card, provider_receipt_id UNIQUE, result_json)
leases(item_kind, item_number, PK(item_kind,item_number), leg_id FK, pid, pgid, start_ticks, owner, acquired_at)
attempts(item_kind, item_number, head_sha, act, count, reset_marker, last_leg_id, PK(item_kind,item_number,head_sha,act))
idempotency(key PK, kind, target, payload_digest, state∈{pending,done,failed}, remote_id, created_at, completed_at)
```

Q4 asks whether `attempts` could be a view over `legs`.

**Cutover (5d), drop and re-derive:**

1. **Last old release.** A one-shot CLI `project-stops` (deleted afterwards) applies `needs-human` plus a reason comment to every open gate or hold in the old DB that GitHub cannot see. Without this, internal-only stops are lost (Q6). Optionally it exports today's per-scope spend and per-open-issue lifetime spend (Q5).
2. **Preconditions.** All daemons are stopped (`single_instance` locks free). No run is in reserved/starting/running with a live lease; boot recovery under old code settles these first.
3. **New store open.** If it finds `schema_migrations`, version ≤ 41, it refuses while any active run exists. Otherwise it renames the file to `control.sqlite3.retired-v41-<ts>` (never read by code; the operator deletes it later), creates the fresh schema with `user_version=1`, and optionally seeds carry-over charged legs.
4. **First tick** re-derives everything from GitHub. Attempts start at 0, `needs-human` labels persist, harness PRs are found by branch prefix, and there are no leases.
5. **Code deleted:**
   - schema history: `_SCHEMA_MIGRATIONS` v8–v41 (`:830-2390`), `_KNOWN_SCHEMA_DIGESTS`, `LEGACY_V8_SCHEMA_DIGEST`;
   - legacy adoption: `adopt_legacy_owner` (`:3062`), `adopt_projection_feed` (`:3157`), `import_legacy_spend` (`:11148`);
   - integrity checks: `_validate_migration_history` (`:2729`), `_assert_semantic_integrity` (`:2859`), the authority witness (`:2630-2705`).

---

## 3. Slice plans 2–8

### Slice 2: self-clear and machine blocks (net about −1.6k)

**Scope:**
- **Domain:**
  - `domain/self_clear.py`: whole file.
  - `domain/actions.py`: `FinalizeMachineBlockRetirement`, `FinalizeBudgetBlockRetirement`, `ReportSelfClearCeiling`, `DiscardSelfClearMarker` (`:378-441`) and `Relabel.self_clear` (`:103-120`).
  - `domain/fix.py`: the `FixState` machine/budget/self-clear fields (`:200-214`), the `self_clear_actions` call (`:1552-1571`) and the `:2090` branch.
  - `domain/continuation_components.py`: `RecordedMachineBlock` (`:166`), `RefusedBudgetBlock` (`:178`), the issue-recovery fields `self_clears`, `machine_block`, `budget_block`, `self_clear_ceiling_reported` (`:220-247`), the PR-repair `self_clears` (`:438, 468`), `SELF_CLEAR` (`:938`), `PRRepairResult.machine_block` (`:1343, 1377-1380`) and the projections (`:679-692`).
  - `domain/outcome.py`: `AdmissionBudgetBlock` (`:384`); `budget_actions` and `pr_budget_actions` (`:565-600`) become wait-only for daily caps.
  - `domain/events.py`: the BudgetBlockRetired, MachineBlockRetired and SelfClearCeilingReached events.
- **Adapters:**
  - `adapters/accounting_runtime.py` `:590-610` and `:886-897`.
  - `adapters/control_store.py`: `record_fix_budget_refusal` (`:5158`) and the projection methods `:7298-7581` (`_require_machine_projection`, `_require_budget_projection`, `retire_budget_block`, `mark_self_clear_ceiling`, `retire_machine_block`, …).
  - `adapters/effects.py`: the self-clear relabel paths.
  - `adapters/workflow_codec.py` `:40, :176`: keep decoding the legacy fields, then drop them.
- **App:**
  - `app/execute.py` `:202-231, 280-290, 349-390, 482-488, 695-743`.
  - `app/roles.py` `:1840-1891`, `_budget_self_clear_worlds` (`:1895`), `:2597-2611` and `:2787`.
  - Self-clear references in `app/resolve_control.py` and `app/dashweb/resolve_*`.
- **Imports:** the machine-block import branch in `domain/continuation_import.py:228`.
- **Hygiene:** the dead files from §0.1–0.2, and `adapters/untrusted.py` (point `handover` at `domain.trust`).

**Steps:**
1. Delete the dead modules and their tests.
2. Add the daily-headroom hold to `decide_fix`: when the reservation won't fit, emit no dispatch and no label. Route the admission-refusal path to "no label".
3. Treat a `blocked` result that carries a machine blocker as a machine failure: no label.
4. Make the codec tolerate and drop the legacy fields.
5. Delete the actions, executor handlers and store methods.
6. Delete `self_clear.py`.
7. Update `architecture.md`.

**Tests that die:** `test_self_clear.py`, `test_self_clear_wiring.py`, `test_review_receipt_attribution.py`, `test_terminal_output.py`, and the self-clear sections of `test_control_store`, `test_fix_workflow`, `test_accounting_runtime`, `test_continuation_components`, `test_continuation_import`, `test_resolve_control` and the `accounted_workflow` fake.

**New behavioural tests:**
- daily headroom short: no dispatch, no label; dispatches once headroom returns;
- a machine-blocker result: no `ai-blocked`, re-derived next tick;
- a legacy component with `machine_block`/`budget_block`/`self_clears` loads and the issue proceeds.

**Risks:** a reservation race refused at admission must stay label-free. The per-issue lifetime cap keeps its escalation until slice 4. Do not add a migration; the v38 index stays.

**Dependencies:** none.

### Slice 3: machine failure as debt (net about −1.5k)

**Scope:**
- **Breaker:** `domain/infra_breaker.py`, `app/infra_breaker.py`, `roles._infra_breaker` (`:1107`), and the `infra_breaker*` and `max_infra_deaths` config knobs.
- **Fix-side ceiling:** `domain/fix.py` `infra_ceiling_hit` (`:1474-1497`) and its arms `:1765, 1813, 1851, 1892-1929, 1954, 1986`; `FixState.infra_deaths` and `infra_control_errors`.
- **Components:** `continuation_components` `infra_failures` (issue recovery and PR repair); `CICharge.INFRA` and `RebaseCharge` infra become NOT_ATTEMPT.
- **Store:**
  - the refusal ledger `:4843-4916`;
  - fix-infra projection/admission/return (`:7581-7702`) and `UnclassifiedFixInfraHistory` (`:2406`);
  - a launched-but-unreturned repair is charged as ATTEMPT at `:8806-8826`; change it to NOT_ATTEMPT.
- **App:** `workflows/fix.py` `_charge_infra_death` (`:1376`) and its callers; `dispatch.py` `_death_key` (`:372`) and the death charge on the exception path.
- **Advisory files:** `adapters/store.py` infra deaths (`:162-182`).
- **Review side:** `review_rounds.py:696-711` (a non-deterministic child failure or a refused submission becomes an infra death, not a strike); `review_execution.py:1086-1090` (pin deferrals stop striking).
- **Handover:** `adapters/handover.py` slims to "wall cap → resume once".

**Replacement:** a pure backoff function. `next_eligible = last_machine_end + min(60s·2^(n−1), 1h)`, where n is the number of consecutive machine-class legs for (item, kind) since the last non-machine end. It is derived from `runs` (kind, dispatch target, outcome, completed_at), with no new table and no knob (constants, per CLAUDE.md).

**Steps:**
1. Add the backoff derive and query, with tests.
2. Flip charges so machine never counts.
3. Delete the ceilings and arms.
4. Delete the breaker.
5. Delete the ledger.
6. Clean config, docs and the env example.

**Tests that die:** `test_infra_breaker.py`, `test_infra_class_counters.py`, `test_refusal_ledger_rearm.py`, `test_return_authority_refusal_ceiling.py`, the infra arms in `test_fix.py`, death keys in `test_dispatch.py`, debt assertions in `test_pr_repair_terminal_recovery*`, and infra deaths in `test_store.py`.

**New behavioural tests:**
- a machine death leaves attempts unchanged and re-dispatches only after the backoff;
- N machine deaths never escalate but stop at the money cap;
- an unreturned launched repair adds no attempt.

**Risk:** the deployment-wide breaker is gone, so a systemic fault burns until the daily caps (Q-extra in §5).

**Dependencies:** none; 2 and 3 can run in parallel.

### Slice 4: labels down to two (net about −0.5k before 5; recommended inside or after 5)

**Scope:**
- `domain/labels.py` keeps `AI_READY` and `NEEDS_HUMAN` (P1–P3 depend on Q13).
- `AI_BLOCKED` (51 refs), `NEEDS_HUMAN_REVIEW` (47), `NEEDS_BUDGET_REVIEW` (16) and `AI_PARKED` (7) map to `NEEDS_HUMAN`, written by a single writer.
- `AI_WORKING` (40), `AI_PR_OPEN` (41), `AI_DONE` (18), `AI_REVIEW` and `NO_AI_REVIEW` are removed; state comes from the lease, PR existence and the branch prefix.
- `github.py build_world` lists by label (`:1531-1557`); it moves to branch-prefix PR listing.
- `fix.py` `label_repair_actions` (`:739`) and the reap/cleanup relabels (`:778-975`).
- `lifecycle.py` phases.
- `cli REMOTE_LABELS` (`:3054`) and prepare-remote.
- Dashboard park becomes `needs-human`.
- An ops one-shot (not src) relabels live issues.

**Steps:** introduce `NEEDS_HUMAN` as the only escalation writer, then stop writing the derived labels, then switch the observe query, then delete the enum members and the board lanes.

**Tests that die:** `test_lifecycle.py`, `test_lifecycle_eligibility.py`, `test_blocked_*` (3 files), `test_human_wait_dashboard_115.py`, and the label assertions in `test_fix`/`test_github`.

**New behavioural tests:**
- escalation writes only `needs-human` plus a reason comment;
- a human comment newer than that comment releases it;
- removing the label releases it;
- no harness write of `ai-working`/`ai-pr-open`/`ai-done`.

**Risk and dependencies:**
- Release-by-removal conflicts with stored gates until 5d (today `control_store.py:2581-2602` refuses legacy unblock under active families), so before 5 the typed re-arm must remain the release for gate-backed stops.
- Branch-prefix observe needs Q1.
- Steward labels belong to 7.
- **Recommendation:** make the 2-label contract part of the new core in 5a/5b, so slice 4 shrinks to the GitHub relabel script plus CLI/dashboard.

### Slice 5: the local workflow model (net about −60.5k)

**5a. Delete offline import/activation and land the pure core (net about −9.9k).**

- **Delete:**
  - app: the 6 `app/*activation*`/`*import*` modules;
  - adapters: 11 modules (`{resolve,review,review_execution,continuation}_activation`, `continuation_run_import`, `review_execution_import_*`, `_counter_links`, `_workflow_census`);
  - domain: 10 modules (`{resolve,review}_activation`, `review_execution_{activation,counter_mapping,death_import,history_import,import_plan,migration,session_import}`, `continuation_import`);
  - CLI: `_cmd_workflow_activation` (`:3414`) and its parser.
- **Cut the runtime leaks in §0.4:** drop the codec registrations; treat import targets as absent at `control_store.py:6751/7351/7374`; replace `retain_deferred_pr_repair` calls with identity; drop `BlockedReviewExecutionMigration` from the union; inline `MAX_LEGACY_SQL_TEXT_BYTES`.
- **Add `domain/state.py` and `domain/policy.py`** (pure, about 500 lines) plus the attempt-count rules, tested against the design table.
- **Tests that die:** about 12k lines, including `test_*activation*`, `test_review_execution_import*`, `test_continuation_*import*`, `test_continuation_activation_cli` and `tests/fixtures/workflow_fix_import_pre_f*.json`.
- **New tests:** one per design table row (7); attempt cap 3 and the reset rules (Q3); needs-human released by reply or removal; a workflow-touching PR goes to `needs-human`.
- **Risk:** a fresh DB can no longer activate families. Tests seed epochs through `tests/fakes/workflow_authority.py`; production must stay off from here to 5d.

**5b. Switch the fix role (net about −8k core, −617 dashboard).**

- **New observe:** one GraphQL query in `github.py` (ai-ready/needs-human issues, prefix-branch open PRs with head, mergeability, required checks, reviews with `commit_id`, comments, changed files) replaces `build_world`.
- **Delete:** `world_probe`, `webhook` and `wake`.
- **Rewire `roles.compose_fix_daemon`** to observe → derive → policy → act. Fix legs reserve through the plain path (`control_store.reserve`, `:5144`, with no workflow and no continuations). Attempts go in a v42 `attempts` table (it survives the cutover) or are derived (Q4).
- **Delete from domain:** `decide_fix` and its body (`fix.py`, down to about 300 lines), `resolve_rearm`, `resolve_return`, `resolve_workspace`, `effort` ladder, `model_routing`, `ci_history`.
- **Delete from app:** `FixState`/`_fix_state` and the reporters (`roles.py:1686-2438`), `workflows/resolve_rearm`, `app/resolve_control`, `mcp`, and `DispatchPathReservations` (`roles.py:808`).
- **Delete from adapters:** `workspace_admission`, and the resolve/re-arm/repair paths in `workflows/fix.py`.
- **Dashboard:** `dashweb/resolve_*` and `guidance`.
- **Tests that die:** most of `test_fix.py`, `test_fix_characterization`, `test_resolve_*`, `test_rearm_task_workspace`, `test_episode_workspace`, `test_workspace_*`, `test_world_probe`, `test_webhook`, `test_wake`, `test_route_journal_decision`, `test_configurable_model_tiers`, `test_effort`, `test_mcp`, `test_ci_history`, `test_pr_repair_*`, `test_issue_reply_admission`, `test_sessionless_resume`; `test_fix_workflow`, `test_convergence` and `test_roles` are rewritten.
- **New tests:** RESOLVE once under a lease; FIX_CI, REBASE and ADDRESS_REVIEW per row; 3 attempts → `needs-human` plus comment; machine death → no attempt; approved+green → merge (Q10).
- **Risk:** `AccountingRuntime._admit_and_run` (`:306`, 485 lines) assumes repair inputs, so plain-leg settlement for every fix kind is the hard part.

**5c. Switch the review role (net about −16k core, −876 dashboard).**

- REVIEW becomes one leg on the exact head that submits through idempotency key `review:{pr}:{head}`.
- **Delete from domain:** the review-family files (`review_workflow*`, `review_execution`, `review_rounds`, `review_preflight`, `review_control`, `review_rejection`, `review_rearm`, `review_dispatch_record`, `review_evidence`, `review_linkage`, `review_return`); `domain/review.py` drops to about 280 lines (multi and delegated modes go, Q13).
- **Delete from adapters:** `review_workflow_control`, `review_execution_control`, `_claims`, `review_round_sources`, `review_dispatch_holds`, `review_effects`, `review_linkage`.
- **Delete from app:** `app/review_runtime`, `app/review_control`, and the review modes in `planners/tasks`.
- **Dashboard:** `dashweb/review_*`.
- **Tests that die:** about 25k lines, including all `test_review_*`, `test_current_review_*` and the fakes `review_authority`/`review_workflow`.
- **New tests:** green with no bot review on head → REVIEW; a crash after submit → no duplicate review; approve plus red required check → no merge; a push after approval → REVIEW again.

**5d. Delete the residual model, cut over the store, wiring (net about −27k).**

- **Delete from domain:** `workflow*.py`, `workflow_identity`, `continuation_components`, `effect_retry`, `eligibility`, `lifecycle`.
- **Delete from adapters:** `workflow_codec`, `workflow_commit`, `workflow_control`, `workflow_rows`, `sql_read_budget`, `deployment_authority`.
- **Rewrite `control_store`** as `adapters/store.py` with 4 tables and the §2 cutover.
- **Collapse** `durable_effects` and `effects` into idempotent act (about 350 lines).
- **Slim:** `operator_effects`; `roles`/`loop`/`main` become `app/tick.py`; config and CLI.
- **Rule updates:** the import-linter "Daemon modules" contract (`pyproject.toml:209`, which names `app.roles` and `app.workflows`) and `port_conformance.py`.
- **ADRs:** mark 0009, 0012, 0013, 0015 and 0016 Superseded (needed by the test at `:579`).
- **Tests that die:** `test_workflow_*`, `test_retry_authorization*`, `test_rearm_reconciliation`, `test_return_command_recovery`, `test_continuation_admission`, `test_durable_workflow_composition`, `test_effect_retry_ceiling`, `test_sql_read_budget`, `test_drain_*`, `test_durable_effects`; `test_control_store` (3.7k) and `test_effects` (2k) are rewritten to about 1k.
- **New tests:** a legacy v41 DB is renamed and a fresh schema created, with refusal while a lease is active; reserve/settle/release on the new store; a dead-pid lease is reclaimed and the leg settled; an idempotent comment survives a crash.

**Slice 5 dependencies:** best after 6 and 7, which remove split/adjudication and the steward family from the model. 5b needs Q1, Q3 and Q4 answered.

### Slice 6: fixer publication gates (net about −8.0k core, −453 plugin)

**Scope:**
- Gate and governor stack: `domain/gate.py`, `domain/governor_policy.py`, `adapters/governor.py`, `gate_transport`, `codex_hook`, `app/gate_check`, `plugin/hooks/*`, `plugin/settings.json`.
- `worktree.build_gate_context` (`:1665`) and `revert_and_run` (`:1870`).
- `contracts` `TestExecution` and `classify_test_exit_code` (`:268-358`).
- `store.HarnessMarkers.test_cmd`.
- TDD split: `resolve_split`, the author/implement/adjudicate paths (`workflows/fix.py:952-1376`, `planners/tasks.py:297-470`, `outcome.py:642-736`), and contract adjudication in `workflow_policy` (`:174-181`); note `AUTHOR_TESTS_SPLIT` defaults to False (`config.py:546`).
- Evidence-gate prompt context: `environment` (domain and adapter).
- CLI test-cmd discovery (`cli.py:923-1500`).
- Config: `TEST_CMD`, `GATE_TEST_FAILURE_EXIT_CODES`, `EDIT_CHECK_CMD`, `EVIDENCE_STACK`, `MAX_CONTRACT_ROUNDS`.
- `agent_publish` (Q11).
- Governor wiring in `agent_runner`/`codex_runner` and `modified_existing_tests_from_diff` in `workflows/review.py:34`.

**Replacement:**
- TDD guidance goes into `plugin/AGENT.md`; the reviewer judges tests.
- If the broker goes: the agent pushes its prefixed branch, and act opens the PR idempotently (`open-pr:{branch}`, body from `pr_body.py`).

**Steps:** move the guidance into the prompt, then delete the split path, then the gates and governor, then the plugin and Make targets (`test-governor`, `test-hooks`, `test-parity`, the second diff in `test-config-generated`), the import-linter plugin contract and `root_packages` "plugin", then the residual registry entries, then mark ADRs 0011 and 0014 Superseded (0001 partly).

**Tests that die (about 9.8k):** `test_gate`, `test_gate_parity`, `test_governor`, `test_hooks.sh`, `test_governor_adapter`, `test_codex_hook`, `test_gate_check`, `test_hook_fixture_isolation`, `test_contract_worktree`, `test_split_*`, `test_resolve_split`, `test_split_return_policy`, `test_agent_publish`, `test_pr_repair_publication`, `test_environment*`, `test_author_handover_provenance`, `test_reauthor_reset`, `test_adjudication_release_proof`, `test_test_suite_workspace`.

**New behavioural tests:** a push without gates leads to exactly one PR; a workflow-touching PR gets `needs-human` (already exists); a push to the base branch is refused (branch protection, documented).

**Risk:** push validation is lost; the act-side branch and issue check plus GitHub branch protection replace it.

**Dependencies:** best before 5.

### Slice 7: the steward (net about −5.0k)

**Scope:**
- Domain: `steward`, `steward_stall`, `triage`, `jira`.
- App: `workflows/steward`, `workflows/jira`; `roles.build_steward_daemon` (`:3505`, 342 lines) plus the steward runtime bindings (`:1474-1598`) and `steward_capability_line`.
- Planners: triage, groom and jira tasks (`planners/tasks.py:1708-1990`) and their schemas.
- Store: steward admission, outcome, recovery and delivery (`control_store.py:6679-7298`); the steward family in `continuation_components` (`:495-860, 1385-1470`).
- `main.py` role.
- Config: `STEWARD_*` and `JIRA_*`.
- GitHub reads: `_open_steward_prs` (`:1668`), `recently_merged_issues` (`:1971`).
- Labels: `ai-triage`, `ai-suggestion`, `needs-design`, `ai-closed`, `maybe-resolved`, `recurrence`.
- Actions: `CloseIssue`/`AddDependency` if steward-only.
- Advisory files: `store.py` Jira and stall markers.
- CLI: `cmd_triage`.
- Ops: launchd/systemd steward units, and `point-at.sh`.

**Steps:** stop the role, then delete the workflows and planners, then the store methods and components (the codec drops `StewardState`), then the labels and config.

**Tests that die (about 4.1k):** `test_steward_*` (7 files), `test_triage`, `test_jira`, `fakes/accounted_steward`.

**New behavioural test:** `issue-agent start` runs only fix and review.

**Risk:** low.

**Dependencies:** none; do it early.

### Slice 8: dashboard and telemetry (net about −6.2k core, about −9.3k dashboard)

**8a, telemetry (can run before 5):**
- roles reporters (`:1948-2438`), `liveness`, `world_reporter`;
- `measurement` (domain, adapters, app), `cohort`, `projection`;
- `typed_journal`, `event_outbox`, `world_observations`;
- `journal` and `event_log` down to a single JSONL leg log, `events` slimmed;
- CLI measurement and spend status lines, and the Codex warning surfaces.

**8b, dashboard (after 5):**
- rebuild the dashboard over `domain/state.derive` plus store read-only queries;
- delete `actions`, `costchart`, `usage`, `verification`;
- park and priority become label changes only;
- regenerate the frontend;
- add an import-linter contract "core must not import `domain.dashboard`, `app.dashboard`, `app.dashweb`, `adapters.dashweb`"; fix `journal.py:27` and `cli.py:44,2654,2745,2786,2849`; launch the dashboard from a separate entry point;
- mark ADR 0004 Superseded.

**Tests that die:** dashboard about 15k (`test_dashweb_*`, `test_dashboard*`, `test_dashboard_board`/`interventions`/`run_number`), telemetry about 3k (`test_event_log*`, `test_journal`, `test_measurement*`, `test_world_reporter`, `test_stall_reporting`).

**New tests:** the board shows the derived state for a fixture world; the only POST is a label change.

**Risk:** CLAUDE.md's metrics (cost per merged PR, leg mix) must come from the `legs` table (Q15).

### Recommended order

Run **2, 3, 7, 6, 8a, then 5a–5d (with 4's contract built in), then the 4 residue, then 8b.** Steward and gates leave first because that removes the split, adjudication and steward families that slice 5 would otherwise have to port.

---

## 4. Shared seams (manager-owned)

1. **Action types (`domain/actions.py`).** Target: `AddLabel | RemoveLabel | Comment(target, n, body, key) | Merge(pr, head, method) | OpenPR(branch, issue)? | Dispatch(act, item, head)`, with `Act = RESOLVE | FIX_CI | REBASE | REVIEW | ADDRESS_REVIEW`. Today `AgentKind` has 15 kinds (`:44`), plus the Relabel workflow binding and self-clear payload. Touched by 2, 4, 5, 7 and 8.
2. **WorldState/PRView (`domain/world.py`).**
   - Keep: `IssueView` (number, labels, comments, author, updated_at); `PRView` (number, head_ref_name, head_ref_oid, base, mergeable, merge_state, required-check CIObservation, reviews with `commit_id`, comments, changed_files, state); `now`.
   - Drop: `declared_paths`, `predicted_paths`, `blocked_by_open`/`open_blocker_numbers` (Q13), `closing_refs`, `content_known`, `ProbeSignals`.
   - The single producer is the new GraphQL observe (5b).
3. **Derived state (new `domain/state.py`).**
   - `derive(obs, local, policy_cfg) -> Mapping[ItemKey, DerivedState]`, where `DerivedState = Dispatch | Merge | Wait(reason) | NeedsHuman(reason) | Busy | Backoff(until)` and `local = (legs, leases, attempts, headroom)`.
   - Then `policy.actions(states) -> list[Action]`.
4. **Outcome classification (`domain/outcome.py`).**
   - `classify(AgentResult) -> Succeeded | Attempted(reason) | MachineFailure(reason) | BudgetStop | AskedHuman(question)`.
   - It reuses `ExitReason`, `infra_class_failure` (`:280`), `budget_exhausted` (`:173`) and `declared_status` (`:107`).
   - Slices 2 and 3 change the machine branch, 5 writes attempts from it, and 6 removes the author/implement statuses.
5. **Daemon loop (`app/loop.py` `Daemon`, `:96`).**
   - Target: `tick(observe, read_local, derive, policy, act, pool)` with SIGTERM drain, heartbeat and systemd notify.
   - Remove `wake_check` (5b), `dispatch_key` and path-lease hooks (5b), and `tick_gate` as the infra breaker (3).
   - One process or two is Q9.
6. **Config (`app/config.py`).**
   - About 130 fields (`:317-731`) drop to about 35: repo, base, target_dir, fix_bot, review_bot, state_dir, worktree_root, poll_seconds, concurrency, merge_method, runtime and model per role, the budget per leg/issue/daily/global, codex settings, `agent_max_runtime`/idle, prompt paths.
   - Every knob deletion regenerates `config/agent.env.example` (`make test-config-generated`).
7. **Store API.** `reserve(leg)`, `start(leg, pid)`, `settle(leg, usage)`, `release(leg, reason)`, `lease/unlease(item)`, `attempts(item, head, act)`, `idempotent(key)`. Slices 2–3 must not add migrations; 5b may add v42 `attempts`.
8. **Docs and gates:** the `architecture.md` manifest, ADR statuses, CLAUDE.md (its "durable commands and receipts" rules conflict with the design) together with `prompt-budget.md`, `port_conformance.py`, the import-linter contracts, the residual registry and the dashboard `generated.ts`.

**Conflict hot spots** (serialize edits): `control_store.py`, `roles.py`, `config.py`, `actions.py`, `fix.py`, `execute.py`, `effects.py`, `cli.py`, `continuation_components.py`, `workflow_codec.py`, `architecture.md`.

---

## 5. Questions

1. **Branch prefix.** Current prefixes are shared with humans (`refs.py` `BRANCH_PREFIXES`). Should harness PRs get a dedicated prefix (e.g. `ai/<n>-slug`), or stay on author == FIX_BOT (`fix.py:1081`)? And how are in-flight `feat/` harness PRs handled at cutover?
2. **The review ⇄ address loop is unbounded except by money once rejection ceilings go.** Accept that, or count CHANGES_REQUESTED rounds per PR rather than per head?
3. **"A new push resets the count" with keys per (item, head, action).** The harness's own FIX_CI push produces a new head, so FIX_CI never caps. Should only non-harness pushes, reviews and comments reset? Should FIX_CI and REBASE be keyed per (pr, act) and reset on green or a human event?
4. **Attempts storage.** Keep an `attempts` table, or derive counts from `legs` (end_class=attempt since the last reset event seen on GitHub)? The design says 4 tables.
5. **Money continuity at cutover.** Drop-and-re-derive resets today's daily spend and per-issue lifetime spend. Seed carry-over legs?
6. **Stops visible only in SQLite** (gates and holds with no label) are lost at cutover. Run a one-shot projection to `needs-human` first?
7. **Spend stops.** Is a daily or global cap a silent wait, and only the per-issue lifetime cap `needs-human`? Rule 5 lists spend as judgment.
8. **Reply detection.** Is the anchor for "reply" the harness's own needs-human comment, rather than the label event or the leg end? The design says "newer than the leg's end".
9. **One tick process or separate fix and review processes?** They hold different GitHub identities today.
10. **Merge mechanics.** Direct merge or auto-merge? Should a BEHIND (not conflicted) PR get update-branch or REBASE?
11. **Publication broker.** Keep `agent_publish` (durable push and PR), or let the agent `git push` and have the harness open the PR?
12. **Runtimes.** Keep GLM (`claude_profile`)? Keep Codex native sub-agent accounting (`codex_child_usage`), or disable sub-agents? Delete the ROUTE classifier and difficulty ladder?
13. **Extras not in the design, with incident history** (default is delete):
    - dependency (blocked-by) holds (keifu #130/#157);
    - path-overlap holds and predicted paths;
    - the duplicate-PR guard;
    - CI free rerun, cancel-stalled and the pending deadline (#49);
    - FIX_MCP_CONFIG and CI-history prompt context;
    - multi and delegated review;
    - external-PR review (`ai-review`, `review_adopt_unlabeled`);
    - P1–P3 ordering;
    - webhook wake and hot reload.
14. **Slice order.** May it run 2, 3, 7, 6, 8a, 5 (with 4's contract), 4 residue, 8b?
15. **Metrics.** Do CLAUDE.md's primary metrics stay? If so, are they computed from `legs` plus GitHub, so the schema-v2 events and outbox can go?
16. **Systemic machine faults.** With the breaker gone, is a per-runtime global backoff acceptable, or are money caps alone enough?
17. **Deployment repoint fencing.** Can `retired_owners` and `deployment_authority` go, leaving only `single_instance`?
18. **ADR retirement plan.** Supersede 0004, 0009–0016 and part of 0001; keep 0002, 0003, 0005–0008 in slimmed form. Does the owner sign off?
19. **Dashboard writes.** Is park equal to `needs-human`, and priority equal to a P label (if kept)? Are the provider usage meters (`adapters/dashweb/usage.py`) in scope?
20. **Daemons off.** Are daemons off for the whole refactor? From 5a to 5d a fresh DB cannot activate families, and during 5b–5c the fix and review roles run different models.

### Critical files for implementation
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/adapters/control_store.py
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/app/roles.py
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/domain/fix.py
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/adapters/accounting_runtime.py
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/adapters/github.py
- /home/chong/Repos/autonomous-issue-agent/src/issue_agent/domain/continuation_components.py
- /home/chong/Repos/autonomous-issue-agent/docs/open/2026-10-12-minimal-harness-design.md