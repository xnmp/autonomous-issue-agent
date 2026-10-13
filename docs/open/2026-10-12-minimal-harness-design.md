# Minimal harness: target design

Draft, 2026-10-12. This is the yardstick for the subtractive refactor. Anything this page doesn't need is deleted, not migrated.

## Rules
1. **GitHub owns workflow state.** Each tick reads it fresh: labels, PR head, mergeability, required checks, bot reviews, comments.
2. **SQLite owns only what GitHub can't hold:** money (reserve, then settle), running legs (leases, sessions), idempotency keys. Attempt counts are derived from the legs.
3. **A machine failure never counts as an attempt.** Retry it with backoff; only money caps bound it.
4. **Agent output is a claim, never authority.**
5. **Only judgment stops work:** spend, access, policy, or a dispute.

## Tick
`observe` (one GraphQL query plus SQLite) → `derive` (a pure function, state never stored) → `act` (idempotent: label, comment, merge, dispatch a leg).

Between ticks a daemon waits for a change, not just the clock. It wakes when either role frees an item of its repository: a claim (lease) seen since the tick began is gone. A role hands its claim back last, after the leg's records and GitHub effects (push, verdict) have landed, so the woken tick sees them. Settlement leaves the claim, rebound to the settling daemon, for its owner to release. `PRAGMA data_version`, checked once a second, gates the lease read (`adapters/control_store.py` `StoreWake`). A change is judged once the store has been quiet for a second, or after 3 s of continuous commits. Ticks stay at least 5 s apart. New claims and other repositories' commits wake nothing, so a busy store can't turn the loop into a hot GitHub poller. One wait spans the poll interval and feeds the watchdog. `POLL_SECONDS` stays the ceiling that picks up GitHub-side events (CI, human comments). After GitHub refuses a tick the interval is never cut short. If the store can't be read, the wait falls back to the timer and logs once. A tick that changed GitHub (a comment, label, merge or PR) re-ticks after the same 5 s gap to act on its own result: requeue after mutation. A tick after a re-tick gap never re-ticks, so a mutation repeated on every tick costs at most one extra observe per interval. A failed write doesn't count, and a refused one backs off.

| Derived state | Action |
|---|---|
| issue `ai-ready`, never stewarded | STEWARD |
| stewarded as a duplicate of an issue that is still open | `needs-human` (duplicate) |
| stewarded, never triaged | TRIAGE |
| triaged, no harness triage comment yet | post the tier, rationale and acceptance criteria |
| issue `ai-ready`, no PR, no lease | RESOLVE |
| required check red on head | FIX_CI |
| conflicted | REBASE |
| CI green, no bot review on head | REVIEW |
| bot requested changes on head | ADDRESS_REVIEW |
| bot approved head + CI green | merge |
| `needs-human` | wait |

Attempts are counted per `(item, action)`: agent-attempt legs since the last reset event on GitHub (a non-harness comment, review or push). The cap is 3. Review rounds are the bot's CHANGES_REQUESTED reviews on the PR, also capped at 3. Hitting either cap means `needs-human` plus a comment saying why. Decisions: [refactor decisions](2026-10-12-refactor-decisions.md).

## Routing
Two light acts run once per admitted issue before RESOLVE, each with no tools, in this order (owner, 2026-10-14):

- **STEWARD** returns testable acceptance criteria and, at most, one suspected duplicate drawn from issue numbers the harness offered. It runs first so a duplicate stops the issue before anything else is spent, and so the grader can read the criteria. An issue whose body has its own acceptance-criteria section is asked for none, so that section stays the contract.
- **TRIAGE** returns the tier (`low`, `med`, `high`, `xhigh`) and a one-line rationale. Its prompt carries the steward's criteria.

Each result is laundered (`domain/triage.py`) and recorded on its leg; malformed output means no criteria and no duplicate stop (STEWARD) or the default tier `med` (TRIAGE), and the comment needs a usable tier. A failed or wall-capped light leg is not retried: the issue proceeds. Once both legs have ended, one keyed harness comment carries the tier, rationale and criteria. A legacy TRIAGE leg recorded by the earlier combined act (its result carries the criteria list) counts as both legs, so existing issues are not re-stewarded; the one case it cannot tell apart, a legacy leg with no usable result, costs one extra steward leg. On Codex, both run read-only, outside any checkout, without the fix contract.

Every fix act (RESOLVE, FIX_CI, ADDRESS_REVIEW, REBASE) runs on `FIX_MODEL_<TIER>` (`runtime:model`), escalated one tier per earlier attempt in its attempt run, capped at `xhigh`. Escalation is derived from the legs, never stored. REVIEW runs at the tier of the fix leg that produced the reviewed head, else the issue's triaged tier, else `REVIEW_MODEL`. An unset tier spec falls back to the role's single model, so a one-model config still works.

A route spec is `runtime:model[:effort]` (e.g. `claude:claude-sonnet-5-5:high`, `codex:gpt-6-luna:low`), so each tier is a (runtime, model, effort) triple; `TRIAGE_MODEL` and `STEWARD_MODEL` take the same grammar (blank `STEWARD_MODEL`: `TRIAGE_MODEL`'s route, then the `low` tier's; both light legs reserve `TRIAGE_BUDGET_USD`). Effort is the last colon-segment when it is all letters (a Bedrock `...-v1:0` stays part of the model). Valid efforts follow what each runner can render: Claude `low|medium|high|xhigh|max` (the SDK's `EffortLevel`), Codex `low|medium|high|xhigh` (`model_reasoning_effort`). An invalid effort fails config validation, like an invalid runtime. An omitted effort keeps today's behaviour, so existing configs are unchanged: fix legs use `FIX_EFFORT` when set; triage and review legs leave the runtime default (Codex: `CODEX_REASONING_EFFORT`). A model tag ending in letters needs an explicit effort (`codex:qwen3:latest:low`). The leg's plan carries the effort, and `result_json` records it beside `tier` when the plan set one.

Scarce fix slots go PR-stage acts before issue-stage acts (finish work in progress before starting new), then to the item that has waited longest (its newest leg end; never served counts as oldest), then by number (`fix.dispatch_order`).

STEWARD and TRIAGE hold no fix dispatch slot (`AgentKind.holds_dispatch_slot`): they never wait behind a heavy leg and never crowd one out. At most `FIX_DISPATCH_CONCURRENCY` light legs run at once, a second pool of the same size, still under headroom, backoff and the per-issue lease.

The records are flat objects, so a later act can add fields without new state: future tech-debt tracking would extend STEWARD (docs only; nothing built).

## Labels
- `ai-ready`: a human admits the issue.
- `needs-human`: the only stop, with one writer. A human reply or removing the label releases it.

Harness PRs are identified by branch prefix, not by a label.

## Quality
Quality comes from the repo's required CI checks plus one model review on the exact head. PRs touching `.github/workflows/**` get `needs-human`.

## Agents
STEWARD and TRIAGE run before RESOLVE in the fix role (it is not a third role). Agents push and return; they never wait on CI. They ask a question only when they need judgment. A comment counts as a reply only if it is newer than the leg's end. A wall-cap hit is a machine failure: resume once.

## Defaults (owner may veto)
- The TDD gate becomes prompt guidance plus reviewer judgment.
- The steward is deleted.
- Cutover: drop the local workflow tables and re-derive everything from GitHub.

## Target

| | Start (2026-10-12) | Target | Now (slice 10) |
|---|---|---|---|
| core src lines (excludes dashboard) | 104,285 | **≤ 15,000** | 14,989 |
| test lines | 148,404 | ≤ 20,000 | 21,739 (target retired by owner 2026-10-13: the remaining gap was layout, not surplus; ruff-normalised measurement: `ruff format --isolated --line-length 120 --config "format.skip-magic-trailing-comma = true" tests/`, then `git ls-files 'tests/*.py' \| xargs cat \| wc -l`) |
| stop states | 34 | **1** | 1 (`NeedsHuman`, six reasons: duplicate added 2026-10-13) |
| labels | 16 | **2** | 2 (`ai-ready`, `needs-human`) |
| SQLite tables | many workflow tables | **3**: legs+money, leases, idempotency | 3 |
| roles | fix, review, steward | fix, review | fix, review |
| ADRs | 17 | this page + money/supervision | this page + 7 (0001–0003, 0005–0008); 9 archived |

Kept, slimmed: money accounting, process supervision, the Claude/Codex runtime adapters, the domain/adapter split.

Kept by owner decision 2026-10-13: hot reload (`adapters/reload.py`, exit-87 restart between ticks), so the daemons pick up merged harness fixes without a manual restart.

**The dashboard is a separate component, outside the core budget** (12,249 src lines now, across `domain/dashboard.py`, `app/dashboard.py` and `app/dashweb/`). It is read-only: it consumes the derived state and the telemetry, and holds no authority. The core must not import it. It may only trigger the operations the design keeps, which means label changes on GitHub. Slice 8 trims it to match the core.

**Target layout** (a guide, not a straitjacket):

```text
domain/   state.py (derive) · policy.py (state → actions) · money.py · outcome.py (classify a leg result) · labels.py
adapters/ github.py · store.py (3 tables) · claude.py · codex.py · process.py (supervise, kill) · worktree.py
app/      tick.py · fix.py · review.py · cli.py · config.py
dashboard (separate, read-only consumer)
```

## Deletion order
Each slice keeps `make test` green and must shrink the line count.
1. Review-time operator tests.
2. Self-clear and machine blocks.
3. Machine-failure-as-debt.
4. Labels down to two.
5. The local workflow model: fences, re-arms, releases, gates, continuation and imports.
6. Fixer publication gates: the TDD red/green gate and the governor checks, replaced by prompt guidance.
7. The steward.
8. Dashboard and telemetry, trimmed to the derived state.
9. Adapter trim (Codex, CLI, store, money path, config, prompts) to reach ≤15k.

The execution order is 2, 3, 7, 6, 8a, 5a–5d (with the two-label contract), the slice 4 residue, 8b, then 9 (from the [audit](2026-10-12-refactor-audit.md)).
