# CodexRunner — Codex execution for fixer and reviewer legs

> **STATUS: BUILT (commit 3ad0992).** `adapters/codex_runner.py` +
> `adapters/codex_hook.py` + the `FIXER_RUNTIME=codex` wiring + 24 tests are in.
> Verified live: GPT-5.5 via CodexRunner resolved a divide-by-zero issue (correct
> red→green, existing test untouched, §10 red-fail passing, session id captured).
> Remaining before prod use: AGENT.md→AGENTS.md parity, a token/wall-clock budget
> ceiling (no native cap), register the model in model-catalog.json, and one live
> demo-gate resolve comparing fix quality + cost vs Opus. The sections below were
> the original scope; they match what shipped.


**Goal:** run agent legs on
**GPT-5.5 via the OpenAI Codex CLI** instead of the Claude Agent SDK, to cut cost,
**without touching the domain model**. `REVIEWER_RUNTIME=codex` runs review and
re-review legs through the same runner; the steward remains on Claude.

## Reviewer runtime

`REVIEWER_RUNTIME` defaults to `codex`, with `REVIEW_MODEL=gpt-5.6-terra`.
`REVIEWER_RUNTIME=claude` is the explicit opt-in for Claude. For self-hosting,
mixed fixer/reviewer runtimes remain recommended: they preserve the diversity
guard that prevents one runtime regression from both creating and approving a
bad change. Runtime is independent of GitHub identity: review submission still
uses `REVIEW_BOT`'s App/token identity, so the 422 self-approval constraint is
unchanged.

Codex receives the review task's existing `REVIEW_SCHEMA` through
`--output-schema`; the normal workflow therefore accepts only its structured
APPROVE/REQUEST_CHANGES envelope. `REVIEW_BUDGET_USD` is enforced from rollout
usage using the same in-harness mechanism as `MAX_BUDGET_USD`; pricing remains
cache-blind pending #89, so operators should treat its dollar figure as a
conservative token meter. Settled `role=review` journal rows retain task/result
model and rollout usage, enabling cost-mix analysis by runtime/model.

Codex review supports `REVIEW_MODE=single` only. `multi` and `delegated` rely
on Claude aliases and SDK subagents, so configuration rejects those combinations
instead of silently dropping legs. The reviewer runs in the operator checkout
under Codex's read-only sandbox and a dedicated governor allow-list (`git diff`,
`gh pr diff/view`, `cat`, `ls`), so filesystem and ambient-GitHub mutation paths
are both contained. Because Codex's resume grammar cannot carry a sandbox
setting, every Codex re-review and in-leg transport retry is intentionally a
fresh session; it receives the prior verdict as context and cannot regain a
writable resume mode.

This is a **second runtime adapter**, NOT an OpenAI-SDK rewrite. A fixer run is
`AgentTask → AgentResult`; `AgentRunner` realises that over the Claude SDK, and a
`CodexRunner` realises the *same interface* over `codex exec`. Everything above the
runner (gate, lifecycle, dispatch, follow-up, governor) is unchanged.

## Why not the raw OpenAI SDK

The OpenAI SDK gives a chat/responses API, not an agentic loop with a pluggable
pre-tool gate. Porting the fixer to it means reimplementing the runtime (tool loop,
governor enforcement, worktree mgmt, budget, structured output). Codex *already is*
that runtime — with hooks + sandbox — so it drops in behind our existing interface.

## What is already PROVEN (live, 2026-06-28)

All against `codex exec` 0.142.3 + GPT-5.5 via an OpenAI-compatible gateway
(the deployment/model id and API key come from your gateway; set them via
`CODEX_MODEL` and the `OPENAI_API_KEY`/`OPENAI_BASE_URL` env):

1. **Model routing + headless exec** — PONG round-trip. ✓
2. **Structured envelope** — `--output-schema` returns `{"status":...,"summary":...,
   "needs":...}` matching `AgentResult`. ✓ (Azure strict mode: every property must be
   in `required`.)
3. **Governor fires + blocks** — our `bash_governor.py`, wired as a Codex PreToolUse
   hook, denied `git push --no-verify`, `HUSKY=0 git commit`, AND
   `git push --force origin main` (the new rule). Codex blocked the command and fed
   the reason back to GPT-5.5. ✓
4. **Real resolve** — GPT-5.5 fixed a divide-by-zero issue: red→green commit order,
   existing test untouched, **§10 red-fail verified** (revert impl → new test fails),
   correct fix, ~52k tokens. ✓

## The interface to satisfy

`CodexRunner.run(task: AgentTask) -> AgentResult` (async), drop-in for `AgentRunner`.
Map each `AgentTask` field to a `codex exec` flag:

| AgentTask field | codex exec |
|---|---|
| `prompt` | positional prompt arg (pass `</dev/null` on stdin to avoid the stdin-wait hang) |
| `schema` | write to a temp file → `--output-schema <file>` (add ALL keys to `required`) |
| `append_system_prompt` (AGENT.md) | Codex has no system-preset append; inject as an `AGENTS.md` in the cwd, or prepend to the prompt. **Parity gap — see risks.** |
| `model` | `-m <model>` (default the GPT-5.5 id) |
| `cwd` | `-C <worktree>` |
| `resume` | `codex exec resume <session_id>` (resume/address-review/fix-ci legs) |
| `max_budget_usd` | no native budget FLAG; enforced in-harness (#29) — the stderr pump samples the session's streamed token counters each poll slice and kills the process group once THIS LEG's priced spend crosses it, returning `ExitReason.BUDGET_EXHAUSTED` |
| `mcp_servers` | `codex mcp add` / config; SonarQube CI-fix only (not wired in Python yet anyway) |
| `env` | process env when spawning codex |
| sandbox | `--sandbox danger-full-access` (Codex hardcodes `.git` read-only under workspace-write — no knob; full-access + governor is the SAME posture as the Claude fixer's bypassPermissions+governor) |
| provider cfg | `-c model_providers.<id>.{name,base_url,env_key,wire_api}` + `-c model_provider=<id>` (inline; do NOT rely on `[profiles.x]` — 0.142 rejects the legacy profile table with `-p`) |
| hook trust | `--dangerously-bypass-hook-trust` (headless can't do interactive `/hooks` trust; this is what KEEPS the governor on, not extra danger) |

**Result parsing** (mirror `result_to_agent_result`): pass `-o <file>` (last message)
and/or `--json` (JSONL events). Read the JSON envelope from `-o`, map
`status`/`summary`/`needs` + keep the full dict in `AgentResult.structured`.
`is_error`: non-zero exit AND no parseable envelope = error (same "read the envelope,
not the exit code" contract as the Claude runner — note the post-result-raise fix in
commit 7926413 was Claude-SDK-specific; Codex signals differently, judge by envelope +
exit together).

## Build plan

1. **`adapters/codex_runner.py`** — `CodexRunner(model, provider_cfg, hook_script,
   sandbox="danger-full-access")` with pure `build_codex_argv(task) -> list[str]`
   (unit-tested, no subprocess) + async `run()` that spawns `codex exec`, captures
   `-o` JSON, returns `AgentResult`. Mirror agent_runner.py's split (pure argv builder
   + thin spawn) so argv assembly is tested offline.
2. **Governor as a Codex hook** — emit `<cwd>/.codex/config.toml` with
   `[[hooks.PreToolUse]]` (matcher `^Bash$`) → `[[hooks.PreToolUse.hooks]]`
   type="command" pointing at a tiny `gov.sh` that pipes stdin to
   `uv run … bash_governor.py`. Codex hook stdin JSON is field-identical to Claude's
   (`tool_name`, `tool_input.command`, `cwd`), so the governor runs UNMODIFIED; exit 2
   = block. Worktree setup writes this config alongside the worktree.
3. **`FIXER_RUNTIME` knob** — `from_env`: `FIXER_RUNTIME=claude|codex` (default claude).
   In `roles.build_fix_daemon`, pick `AgentRunner` vs `CodexRunner` for the fixer ONLY;
   review/steward stay Claude. Add `FIX_MODEL` default = the GPT-5.5 id when codex.
4. **AGENT.md parity** — port the fixer instructions Claude gets via the system-prompt
   preset into an `AGENTS.md` Codex reads from the cwd (Codex's native instructions
   file), so red→green discipline + `.harness` markers + envelope contract reach GPT-5.5.
5. **Tests** — `tests/adapters/test_codex_runner.py`: build_codex_argv maps every field
   (schema→tempfile+required, model, cwd, resume, provider -c flags, sandbox, hook
   config); envelope parse (incl. is_error on bad/empty output); a fake `codex` binary
   for run().

## Open risks / decisions before/while building

- **`.git` read-only ⇒ danger-full-access.** Confirmed no Codex config knob (OpenAI
  issues #12280/#14338 open). full-access widens blast radius beyond workspace-write
  (writes outside the repo aren't sandbox-blocked; only git/bypass are governed). On
  the real fork the worktree + GitHub branch protection bound it, but document it.
- **PreToolUse doesn't intercept ALL shell yet** (Codex `unified_exec` caveat). Our
  git-push/commit/bypass cases DID trip it (verified), but re-verify after Codex
  upgrades; the gate's value depends on it firing.
- **No native budget cap — RESOLVED in-harness (#29).** `max_budget_usd` still has no
  Codex flag, so `run()` enforces it: the stderr pump samples the session's rollout
  token counters once per poll slice, prices them with the same `rates_for_model`
  lookup the ledger uses, and kills the process group on crossing —
  `status="error_max_budget_usd"`, `exit_reason=BUDGET_EXHAUSTED`, with the spend it
  had burned carried on the result so the ledger and both spend caps still see it.
  Three things this depends on, in order of how badly they bite:
  1. **Machine-readable root identity.** Codex 0.146.0 removed the human-oriented
     `session id:` header. Fresh runs discover the newest post-start
     `session_meta` matching `originator=codex_exec`, exact resolved cwd, and
     `source=exec`; the last constraint excludes newer same-cwd subagent
     rollouts. Resumed runs use the persisted ID directly because their
     `session_meta` predates the current leg.
  2. **Per-leg, not cumulative.** A resumed session's counter includes the previous
     leg's already-ledgered tokens; the check runs on the `usage_delta`, or every
     follow-up leg dies instantly on its resolve's spend.
  3. **Rollout counters, not arbitrary stderr.** Codex stderr includes
     agent-controlled tool and test output. Usage-shaped text echoed there must
     never price or kill an in-flight leg. The legacy stderr parser is used only
     after exit and accepts a terminal footer.
  4. **Priced or inert.** Both rates 0 → every run reads as $0.00 and the check is
     deliberately silent (`budget_overrun_usd`), because a dollar threshold cannot
     bound a runtime nobody is pricing. `describe()` and
     `codex_budget_not_enforced_warning` both report that regime as NOT ENFORCED.
  5. **`CODEX_ENFORCE_BUDGET=0`** returns to wall-cap-only bounding, and is likewise
     reported. On a subscription the dollars are synthetic — they are a token meter —
     so an operator whose rates over-state reality can opt out rather than have real
     work cut short.
  History: before this, `AGENT_MAX_RUNTIME_SECONDS` was the only per-run bound and a
  keifu resolve completed at $28.74 against a configured `MAX_BUDGET_USD=20` with
  `exit_reason=ok`, while every boot logged `fix_budget=$20.00` as if it applied.
- **Journal telemetry.** The journaling decorator measures wall duration for
  every runtime. Codex additionally counts rollout `token_count` events as paid
  model turns, subtracting a resumed session's baseline, so settled `agent_run`
  entries carry `duration_s` and `turns` alongside cost and token usage.
- **AGENT.md parity is the quality lever.** GPT-5.5 followed an inline TDD prompt well
  in the smoke test, but the full fixer contract (markers, adversarial verdict, PR body
  shape) lives in AGENT.md — port it faithfully or fix quality regresses.
- **Model metadata warning** — register the GPT-5.5 id in `~/.codex/model-catalog.json`
  so cost/context accounting is real (cost is the whole point).
- **Decision:** keep the reviewer on Claude/opus (decorrelation). Only the fixer moves.

## Effort

~1 focused day: codex_runner.py + argv builder + tests (½ day), governor-hook config
emission (small, governor itself unchanged), FIXER_RUNTIME wiring + AGENTS.md port +
one live demo-gate resolve on the throwaway repo to compare fix quality + cost vs Opus.
