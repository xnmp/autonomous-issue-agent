# Refactor: bash harness → Python + Claude Agent SDK

**Goal (per request):** a real **domain model** with **behavioural tests inside that
model** — the lifecycle logic, the gate, and the agent-decision rules become pure,
testable Python, with GitHub / the Agent SDK / git / the filesystem pushed to the
edges as adapters.

**Today:** ~2,500 LOC of bash across three daemons (`agent/`, `review/`, `steward/`)
that shell out to `claude -p` for every action and parse its JSON envelope; a Python
PreToolUse "governor" (`plugin/hooks/bash_governor.py`) plus bash gate checkers
(`plugin/hooks/checks/*.sh`); and ~1,150 LOC of tests driven by **fake `gh`/`claude`
binaries** (`tests/fakes/`). Coordination between daemons is *only* via GitHub labels
and comments — which is what lets us cut over one daemon at a time (a Python steward
and a bash fix-watcher interoperate fine).

---

## 1. Why this is worth doing

The decision logic that encodes everything we learned this year is currently tangled
into bash side-effects and only testable end-to-end through fake binaries:

- **CI state precedence** — `pending` beats `failure` (wait for CI to settle before
  declaring a failure) — `agent/watcher-lib.sh`.
- **No self-approval** — review must run as a *distinct* GitHub actor; the fix-watcher's
  "reviewed by someone other than me?" check.
- **Red-fail revert check** — `_pr_common.sh prc_check_red_fails`: revert the impl,
  re-run the agent-declared test command, require red.
- **Triage gating** — candidate = open issues with no lifecycle label; dedupe on
  `updatedAt`; *post-action* re-read to avoid the feedback loop; feed the comment
  thread so answered questions aren't re-asked.
- **Suppression guardrail** — scoped (allow vendored/generated/false-positive/glue,
  reject suppressing the code under change).
- **Retry caps**, priority ordering of `ai-ready`, blocked-issue resume, conditional
  MCP attach (Sonar only when a matching check is present).

In a domain model these become pure functions `WorldState -> [Action]` and
`(ProposedCommand, Worktree) -> Allow | Deny(reason)`, tested with constructed
fixtures — no GitHub, no `claude`, no subprocess. That is the deliverable.

---

## 2. SDK capability map (verified against current docs)

Every CLI surface the harness uses has a first-class Python `ClaudeAgentOptions` field:

| Bash today (`claude -p` flag) | Python SDK |
|---|---|
| `--json-schema S` → read `.structured_output` | `output_format={"type":"json_schema","schema":S}` → `ResultMessage.structured_output` (SDK re-prompts on mismatch) |
| `--append-system-prompt P` | `system_prompt={"type":"preset","preset":"claude_code","append":P}` |
| `--tools 'Read,Bash(git *)'` / `--disallowed-tools 'Edit,Write'` | `allowed_tools=[...]` / `disallowed_tools=[...]` — **same `Bash(git *)` pattern syntax**, scoped denies survive bypass |
| `--permission-mode auto` | **`auto` is TS-only.** Use `permission_mode="bypassPermissions"` + governor hook (the real gate), or `"dontAsk"` + strict allow-list |
| PreToolUse governor (subprocess via `dispatch.sh`) | **in-process async callable** in `hooks={"PreToolUse":[HookMatcher(matcher="Bash",hooks=[governor])]}`; deny via `hookSpecificOutput.permissionDecision="deny"` + `permissionDecisionReason` (reason goes back to the model) |
| `--mcp-config F` (Sonar) | `mcp_servers={...}` (stdio/http/sse) + `allowed_tools=["mcp__sonarqube__*"]` |
| `--model alias` | `model="opus"` (per-query; `fallback_model` available) |
| `--max-budget-usd N` | `max_budget_usd=N` (client-side estimate; subtype `error_max_budget_usd`) |
| `--resume SID` | `resume=session_id` (or `continue_conversation=True`) |
| `--worktree NAME` | **no SDK equivalent** — we manage `git worktree add` ourselves and pass `cwd=<path>` |
| `--settings JSON` (inject perms+hook) | not needed in-process — perms via options, hooks via `hooks=`; `setting_sources=[]` to isolate |
| JSON envelope `.is_error/.total_cost_usd/.session_id` | `ResultMessage`: `subtype`, `result`, `structured_output`, `total_cost_usd`, `usage`, `session_id`, `is_error` |

**Install:** PyPI `claude-agent-sdk`, Python ≥3.10. The SDK **bundles its own Claude Code
binary** — no separate `claude` CLI / Node needed; updating the package updates the embedded
CLI. Concurrency: multiple `query()` in one asyncio process, **isolate by `cwd`** (one
worktree per concurrent agent) and `setting_sources=[]` to prevent settings/memory leak.

---

## 3. Target architecture (three layers)

```
domain/          PURE. No I/O, no SDK, no gh, no subprocess. 100% unit-tested.
  labels.py        Label enum + lifecycle state machine (allowed transitions)
  world.py         WorldState, IssueView, PRView, ReviewState, CIState (frozen dataclasses)
  actions.py       Action ADT: DispatchFix | ResumeBlocked | ReviewPR | FixCI |
                   AutoMerge | Cleanup | TriageIssue | PromoteIssue | Relabel |
                   PostComment | CloseIssue | NoOp
  decide.py        decide_fix(state) / decide_review(state) / decide_steward(state)
                   + ci_state(), pick_ready_issue(), review_by_other?(), retry-cap logic
  gate.py          PURE gate rules: branch-name, commit-order, red-fail, marker checks,
                   suppression guardrail → Allow | Deny(reason). (port of check-*.sh)

adapters/        IMPURE. Thin. Each wraps one external surface; faked in tests.
  github.py        GitHubGateway: read WorldState; apply Relabel/Comment/Merge/Close.
                   Three identities = three configured instances (token source differs).
  app_token.py     GitHub App installation-token mint (port of bin/gh-app-token.sh)
  agent_runner.py  AgentRunner.run(AgentTask)->AgentResult over claude_agent_sdk.query()
  governor.py      PreToolUse HookMatcher callable -> calls domain/gate.py
  worktree.py      git worktree add/remove, cwd resolution
  store.py         marker files (.harness/*), session files, triage .at dedupe; Clock

app/             ORCHESTRATION. Thin loops: observe -> decide -> execute -> sleep.
  loop.py          generic daemon: heartbeat, sleep, role dispatch
  fix.py review.py steward.py   role wiring (which decide_* + which identity)
  execute.py       interpret Action -> adapter calls (+ run AgentTask via AgentRunner)
  main.py          entrypoints (replaces the three .sh + launchd/systemd targets)
```

**Key types**

```python
@dataclass(frozen=True)
class AgentTask:
    prompt: str
    schema: dict | None
    allowed_tools: list[str]
    disallowed_tools: list[str]
    model: str | None
    max_budget_usd: float
    cwd: str | None              # worktree path
    resume: str | None           # session id
    append_system_prompt: str
    mcp_servers: dict | None

@dataclass(frozen=True)
class AgentResult:
    ok: bool                     # subtype == "success"
    structured: dict | None      # ResultMessage.structured_output
    text: str | None
    cost_usd: float
    session_id: str
    error_subtype: str | None
```

`AgentRunner` is the *only* place that imports the SDK. Everything else speaks
`AgentTask`/`AgentResult`, so the domain stays SDK-agnostic and the runner is fakeable
(return canned `AgentResult`s in tests).

---

## 4. Phasing (clean rewrite — no need to keep the harness live)

Decision (confirmed): we do NOT run Python and bash daemons side by side. The bash
harness stays as the reference while we build the Python one; we cut over in one
move once the acceptance gate is green, then retire the bash. This is simpler than
the strangler-fig (no per-daemon label interop to maintain).

**Acceptance gate (what "done" means — domain tests alone are NOT sufficient):**
1. **All domain behavioural tests green** — proves the *brain* (`decide_*`, `gate`)
   over constructed fixtures. These run against in-memory value objects, so they
   cannot prove the adapter boundaries (GitHubGateway endpoints, AgentRunner ↔ SDK,
   the governor hook actually firing + denying, worktree/app-token).
2. **One green end-to-end run through `examples/demo-target/`** — a self-contained
   repo with seeded issues, exercised with the REAL SDK + real git + real `gh`
   against a scratch repo. This is the repeatable gate that touches every adapter
   once and closes the gap the fakes leave open.

Status: **Phase 0 + Phase 1 COMPLETE** (2026-06-26). `src/issue_agent/domain/` +
`tests/domain/` (87 tests); `make test-domain` / `make test`. The harness remains
fully bash-live and unchanged.

### Phase 0 — Domain extraction + behavioural tests ✅ DONE (2026-06-26)
- `uv` project (`pyproject.toml`, `src/issue_agent/` package). Domain layer is
  **dependency-free stdlib** by design; SDK/pydantic deferred to adapter phases.
- `domain/`: `labels.py` (lifecycle state machine + priority), `world.py` (frozen
  `WorldState`/`IssueView`/`PRView`/`Review`/`Check`/`Comment`, `CIState`),
  `actions.py` (Action ADT), `refs.py` (branch↔issue parsing), `ci.py`
  (`ci_state`/`latest_other_review`), `fix.py` (`decide_fix` + cores), `review.py`
  (`decide_review`), `triage.py` (`triage_candidates`/`actions_for_verdict`),
  `outcome.py` (envelope + ground-truth follow-up).
- `tests/domain/` — 87 named behavioural tests; every lesson encoded
  (`test_pending_beats_failure`, `test_no_automerge_when_only_self_approved`,
  `test_no_feedback_loop_after_recording_post_action_updatedat`, …).

### Phase 1 — Gate rules as pure functions ✅ DONE (2026-06-26)
- `domain/gate.py`: pure ports of `check-*.sh` + `_pr_common.sh` — branch name,
  commit prefix + TDD ordering, commit-log re-derivation, PR base/body/--web,
  required-test-in-diff, evidence, test-immutability, markers, adversarial verdict,
  test-cmd resolution. The two impure rules (§1 run-tests, §10 red-fail revert) are
  higher-order functions over an injected callable, so the decision logic is tested
  purely and only a ~3-line shell-out remains for the adapter.
- The fail-CLOSED bypass core (`--no-verify`/`HUSKY=0`/`core.hooksPath`) already
  lives as pure functions in `plugin/hooks/bash_governor.py`
  (`extract_commands`/`classify`/`bypass_reason`); Phase 2's `adapters/governor.py`
  reuses it directly rather than re-transcribing.

### Phase 2 — AgentRunner + in-process governor ✅ DONE (2026-06-26)
- `claude-agent-sdk` pinned as the `[sdk]`/`[dev]` extra (v0.2.110; bundles its own
  CLI binary, no Node). Domain layer stays zero-dep.
- `domain/agent_task.py` — `AgentTask`/`McpServer`, the SDK-agnostic description of
  one invocation (result side reuses `domain.outcome.AgentResult`).
- `adapters/agent_runner.py` — the ONLY SDK importer (lazy). `build_option_kwargs`
  (pure: AgentTask→ClaudeAgentOptions kwargs — claude_code preset+append, json_schema
  output_format, tools/disallowed/model/budget/cwd/resume/fork/mcp/env, setting_sources)
  and `result_to_agent_result` (pure: ResultMessage→AgentResult, envelope-not-prose)
  are both unit-tested with no live call; `AgentRunner.run` drives `query()`.
  Default `permission_mode="bypassPermissions"` (RESOLVED: SDK *does* expose `auto`
  in Python, but the governor hook is the gate by design, so we don't rely on it).
- `adapters/governor.py` — in-process `HookMatcher(matcher="Bash")` callback. REUSES
  `bash_governor.find_violations`/`classify` for fail-CLOSED bypass detection; routes
  governed commands to `domain/gate.py` against an injected `GateContext` (the
  git/fs-backed builder is Phase 4). `evaluate_command` is pure + fully tested; the
  hook returns the SDK deny shape (`permissionDecision="deny"` + reason) and fails
  OPEN on its own errors. Verified the real `ClaudeAgentOptions(**kwargs)` accepts our
  mapping (not just fakes). 33 adapter tests; `make test-adapters`.

### Phase 3 — GitHubGateway + executor + app-token ✅ DONE (2026-06-26)
- `adapters/github.py` — PURE parsers (`parse_issue`/`parse_pr`/`parse_world`:
  gh `--json` dicts → domain value objects, the real-vs-fake seam, tested against
  documented shapes) + impure `GitHubGateway` (runs `gh` as an `Identity` =
  ambient / static PAT / App-token provider; reads issues+PRs incl. the separate
  `pr checks` endpoint; applies relabel/comment/close/enable-auto-merge via an
  injected `runner`, faked in tests).
- `adapters/app_token.py` — installation-token mint (port of gh-app-token.sh). The
  cache/freshness DECISION (`token_is_fresh`, the >5-min guard with the UTC bug
  lesson) is pure + clock-injected; `AppTokenMinter` mints-on-miss with an injected
  `mint_fn`; `default_mint_fn` is the thin live shell (PyJWT RS256 + urllib POST).
- `app/execute.py` — `Executor` interprets each `Action` (Relabel/PostComment/
  CloseIssue/EnableAutoMerge handled directly; RemoveWorktree + RunAgent delegated
  to injected callables). Flat dispatch, no logic; tested with a fake gateway.
- 64 adapter tests (Phase 2 + 3); `pyjwt[crypto]` added to the `[sdk]` extra.

### Phase 4 — App loop + remaining adapters + task builders ✅ DONE (2026-06-26)
- `adapters/store.py` — typed STATE_DIR state (sessions, resume/ci/review retry
  counters, resumed-at, automerge marker, triage `.at`; pure path helpers + thin IO
  + a `clear_issue` mirroring cleanup.sh) and `HarnessMarkers` (the worktree
  `.harness/` reads: ac-posted/test-plan-posted/adversarial-verdict/test-cmd).
- `adapters/worktree.py` — `Worktrees.ensure/remove` (port ensure_worktree) and
  `build_gate_context(cwd)` — the git/fs fact-gatherer the governor's
  `make_pretooluse_hook(build_ctx)` needs, incl. the §10 `revert_and_run` (revert
  impl → run tests → restore HEAD). git injected for tests.
- `domain/schemas.py` (FIX/ROUTER/REVIEW/TRIAGE, verbatim) + `domain/tasks.py` —
  pure per-AgentKind `AgentTask` builders (resolve/resume/address-review/fix-ci/
  router/review/re-review/triage): prompts, schema, tool policy, budget, model,
  resume, MCP — ported from the bash `claude -p` calls (incl. the fix-ci
  anti-suppression guardrail + triage anti-re-ask instruction).
- `app/loop.py` — generic `Daemon` (observe→decide→execute→`tick`; `run_forever`
  with the heartbeat + suspend-stall warning). Crash-isolated per pass + per action.
- 89 adapter tests + 96 domain (incl. task builders); `make test` green.

### Phase 5 — Wiring complete; live gate + cutover remain (operator) ✅ (2026-06-26)
- **Follow-up logic** (pure, in domain): `outcome.resolve_actions/resume_actions/
  address_review_actions/fix_ci_actions` + `review.review_submission` — the script
  tails as Action lists / typed effects, tested in `tests/domain/test_followup.py`.
- **`app/dispatch.py`** — `AgentDispatcher`: maps `RunAgent(kind, n)` → gather inputs
  (gateway reads incl. the ground-truth `pr_for_issue`) → `build_*_task` →
  `AgentRunner.run` → apply the pure follow-up via the executor + persist session/
  counters / triage `.at`. Async; all collaborators injected → faked in
  `test_dispatch.py`.
- **`app/config.py`** (env → `HarnessConfig`) + **`app/roles.py`** (build_fix/review/
  steward_daemon, binding observe + bound `decide_*` + the async dispatcher as the
  executor's `run_agent`) + **`app/main.py`** (`python -m issue_agent.app.main
  <fix|review|steward>`, identity wiring: fixer=ambient/PAT, review+steward=App
  token; governor hook built from `build_gate_context`). Verified the full fix
  daemon `tick()` end-to-end with fakes (resolve → PR-opened follow-up).
- `AgentResult` gained `.structured` + `.field()` so the review schema's
  decision/body are reachable. 110 domain + 100 adapter tests. `make run-*-py` +
  `bin/demo-gate.sh` (`make demo-gate DEMO_REPO=…`).

**Remaining (operator, needs live auth + SDK spend):**
- Run `make demo-gate DEMO_REPO=you/calc-demo` — the live end-to-end gate (§4):
  one issue → governed SDK resolve → PR → ai-pr-open. (Bounded via
  `ISSUE_AGENT_MAX_TICKS`.) Enable `run-review-py` to drive the review/merge legs.
- Once green: point launchd/systemd at the Python entrypoints, delete the bash
  daemons + `tests/fakes/` + `dispatch.sh`; refresh docs + the swimlane diagram.

---

## 5. Effort & risk

- **Bulk of the value** is Phase 0+1 (the domain model + gate as pure tested Python):
  a few focused days, and it's pure transcription + tests — no live risk.
- **Full cutover**: ~2 weeks of careful work, gated by live validation on the fork
  per daemon. Most remaining bash is boilerplate the SDK erases (arg assembly, envelope
  parse, dispatch subprocess, settings injection).
- **Top risks (each = a named behavioural test before its code is touched):**
  - regressing `ci_state` pending-precedence → fix-ci misfires;
  - losing the distinct-actor invariant → 422 self-approval / silent merge;
  - weakening the red-fail revert check → tautological tests slip through;
  - reintroducing the triage updatedAt feedback loop;
  - over/under-scoping the suppression guardrail.
- **Identity/secrets unchanged:** App `.pem` at the path in `GH_APP_KEY_FILE`,
  env files + `config/mcp/` stay gitignored; never commit secrets. App-token mint moves
  from bash to `adapters/app_token.py` but the three-identity model is identical.

---

## 6. Open decisions (resolve before Phase 2)
1. `bypassPermissions`+governor vs `dontAsk`+allow-list (recommend former).
2. `query()` per action vs one long-lived `ClaudeSDKClient` per issue (resume by
   `session_id` works either way; per-action `query()` is simpler and matches today).
3. Pydantic models vs frozen dataclasses for the domain (dataclasses keep domain
   dependency-free; pydantic only at adapter boundaries for schema gen).
4. Keep the `.harness/` marker-file IPC, or — since the governor is now in-process —
   pass the agent's declared test command through SDK state instead. (Recommend keep:
   the marker is the *agent's* on-disk declaration the gate validates; in-process
   doesn't change that the agent is a separate `claude` subprocess.)
