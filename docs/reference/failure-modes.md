# Failure Modes — autonomous-issue-agent

Catalogue of every way the fix/review/steward agents (and the harness driving
them) fell over, mined from ~74 Claude Code transcripts (~81MB) for this repo.
Includes bugs already fixed. Grouped by **status**, then **category**.

Status legend: **FIXED** · **OPEN / KNOWN** · **BY-DESIGN / OPERATIONAL**

---

## FIXED

### SDK / runtime

**1. `query()` raises trailing `ProcessError` after delivering `ResultMessage`**
- Symptom: PR opened but follow-up never ran; issue stuck `ai-working`, PR unlabelled.
- Root cause: `claude-agent-sdk` v0.2.110 `query()` raises `ProcessError` when the CLI exits non-zero *after* emitting `ResultMessage` (benign post-`gh pr create` exit). Exception propagated before caller acted on the captured result.
- Fix: wrap `async for` in try/except; if a `ResultMessage` was already captured, return it; only re-raise on crash before any result. 2 regression tests added.

**2. SDK 0.2.110 background tasks → no terminal `ResultMessage`**
- Symptom: reviewer returns `is_error=True, cost=$0.00, "no ResultMessage from query()"`; PRs stall un-reviewed despite real token spend. PR #354 re-review failed 5+ times, merged manually.
- Root cause: SDK 0.2.110 background Task support. Agent spawns background task (`Bash(run_in_background=true)`); main turn ends `terminal_reason="background_requested"` with `TaskNotificationMessage` as final stream msg, never a `ResultMessage`. Runner's `isinstance(msg, ResultMessage)` never matched.
- Fix (commit `3f5baf8`): governor `PreToolUse(Bash)` denies `run_in_background=true`; `AgentRunner` skips results with `origin.kind=="task-notification"`; `Monitor`/`Agent` added to disallowed tools; logs msg-count + last-kind on silent clean-end path.

**3. `EDIT_CHECK_CMD` cwd assumption → fatal hook loop (44+ iterations)**
- Symptom: `cd: backend: No such file or directory` on every edit → `Error in hook callback hook_0` → SDK tears down control stream → `Stream closed` → session permanently wedged.
- Root cause: `EDIT_CHECK_CMD="cd backend && ..."` assumed cwd=repo-root, but PostToolUse hook runs from worktree cwd. SDK hook-bridge errors above the Python fail-open catch, killing the stream.
- Fix: git-root-anchored form — `R=$(git rev-parse --show-toplevel) && test -d "$R/backend" && (cd "$R/backend" && ...) || true`.

### Codex runtime

**4. Codex reserved provider id `openai` → exit 1 at config load**
- Symptom: every fixer dispatch fails immediately: `model_providers contains reserved built-in provider IDs: 'openai'`. Codex exits before any work. Loop: dispatch → exit 1 → relabel → repeat.
- Root cause: `codex-cli >=0.142` reserves built-in id `openai`; overriding it under `model_providers` aborts at config load. Default `provider_id` was `"openai"`.
- Fix (commit `3f5baf8`): default → `"openai-custom"` across `CodexProvider`, `HarnessConfig`, `CODEX_PROVIDER_ID`, `agent.env.example`.

**5. Codex SIGKILL'd at 30s — `asyncio.gather` timeout regression**
- Symptom: every codex run dies in <30s: `codex: exit -9`, `status=timeout`, zero IDLE log lines.
- Root cause: commit `a5144bc` wrapped primary `asyncio.gather(_pump_stderr(), _read_stdout())` in a 30s joint ceiling. `_pump_stderr()` only returns on codex EOF, so any run >30s hit `TimeoutError` → `killpg(SIGKILL)`. Silence because IDLE line only emits on per-readline watchdog path, not the joint-timeout path.
- Fix: `_pump_stderr()` runs uncapped (bounded by per-line idle watchdog); cap applies only to residual stdout drain after stderr EOF. New `post_hang_drain_timeout=30.0`. Regression test added (fails on old, passes on fix).

**6. Idle watchdog killed only direct codex child → leaked process tree, wedged daemon**
- Symptom: after `AGENT_IDLE_TIMEOUT` fired, codex + children (e2e server, vite, playwright) stayed alive. Leaked vite grandchild held inherited stdout pipe open → `_read_stdout()`/`proc.wait()` never returned → daemon WEDGED, log frozen, no heartbeat.
- Root cause: codex spawned without `start_new_session=True`; `_kill_quietly` sent signals only to direct pid. Grandchildren survived in a different pgroup holding the fd.
- Fix: spawn `start_new_session=True`; `_kill_quietly` → `os.killpg(SIGKILL)` whole group; post-kill stdout drain + `proc.wait()` bounded with timeouts.

**7. Codex `$DIAGNOSE_RULES` placeholder → phantom skill hunt (448k tokens)**
- Symptom: Codex fix burned 448k input tokens (18 turns). First action every run: `cat ~/.agents/skills/diagnose/SKILL.md` — hunting a skill that doesn't exist. 8/18 turns were zero-work `update_plan` calls.
- Root cause: `plugin/AGENT.md` is a template with literal unexpanded `$DIAGNOSE_RULES`, `$TEST_RULES`, `$BASE_BRANCH` etc. Claude ignores them; GPT-5.5 took them literally. Nothing rendered the template.
- Fix (commit `6d928c6`): `render_agent_md()` expands `$BASE_BRANCH`/`$HARNESS_REPO`, strips `$*_RULES` blocks, leaves `$ISSUE_NUMBER` literal. `## Efficiency` nudge added. Re-measured: turns 18→9, update_plan 8→2, cost $0.65→$0.29.

**8. Codex session id read from wrong stream (empty session, broke resume)**
- Symptom: `CodexRunner.run()` returned empty `session_id` → resume broken.
- Root cause: codex CLI prints session id to STDERR, not stdout; impl only read stdout.
- Fix: `run()` checks stderr first.

**9. Azure GPT-5.5 HTTP 429 surfaced as `response.failed` stream-drops**
- Symptom: fixer hits `codex: gateway stream-drop (attempt N/5); resuming` across all 5 retries → `status=timeout`. Every codex invocation failed during the outage window.
- Root cause: Azure deployment rate-limiting; surfaces 429 as mid-stream `response.failed`. 5-retry backoff (2→5→11→22s) was far shorter than Azure's 60s renewal window, so every retry hit the same limit.
- Fix (commit `b74b280`): 60s minimum floor on stream-drop backoff, aligned to Azure renewal window.

**10. Codex CLI hangs on stdin when piped non-interactively**
- Symptom: `codex exec "<prompt>"` hangs indefinitely in a headless pipeline.
- Root cause: piped codex waits on stdin by default.
- Fix: `CodexRunner` passes `</dev/null` to detach stdin.

### Daemon / dispatch

**11. Dead circuit breakers — retry caps never enforced**
- Symptom: `max_ci_retries`/`max_review_retries` incremented in the store but never read; loop could re-dispatch CI-fix / address-review indefinitely.
- Root cause: `dispatch._fix_ci`/`._address_review` bumped counters but no code read them to gate dispatch.
- Fix: counters plumbed into `FixConfig`; `decide_fix` checks `breaker_tripped` and escalates to `needs-human-review`; `review_submission` forces escalation at round ceiling.

**12. Duplicate daemon trees → concurrent dispatch of same issues**
- Symptom: issues #54/#57/#58/#59 dispatched concurrently; 4+ codex processes parallel; #58 double-dispatched while already running.
- Root cause: repeated `point-at` relaunches without killing prior trees stacked 3 launcher parents + 5+ fix daemons, each polling independently.
- Fix: `point-at.sh` detects existing launcher pids via `pgrep` and exits; `main.py` uses `adapters/single_instance.py` flock guard (`acquire(role, repo)` → `AlreadyRunning`).

**13. Resume anti-loop dedup missing — single reply re-triggers every poll**
- Symptom: one human reply on a blocked issue re-triggered a resume agent run every poll tick.
- Root cause: watcher polled "ai-blocked with reply newer than last action" but never recorded which reply it had acted on.
- Fix: `resumed_at` marker in state; `issues_needing_resume` requires reply timestamp strictly newer than `get_resumed_at()`.
- Regressed: the SDK port carried the read-side dedup (`issues_needing_resume`) but dropped the write side — `Store.set_resumed_at` was defined and read, but never called, so the guard was silently inert again.
- Re-fixed 2026-07-25: `workflows/fix.py`'s `resume()` now calls `store.set_resumed_at(issue, reply_comment.created_at)` before dispatch, using the same comment selection (`last_human_reply`) as the read side.

**14. Steward duplicate triage comments — `updatedAt` feedback loop**
- Symptom: #40 got duplicate triage comments in a loop (observed live).
- Root cause: steward's own comment bumped `updatedAt`; recorded baseline was pre-action, so next poll saw issue as newer → re-triaged. Fake `gh` in tests never bumped `updatedAt`, hiding the bug.
- Fix: re-read + record `updatedAt` *after* own comment+relabel. Also gave fake `gh` a monotonic clock that bumps `updatedAt` on mutation.

**15. Triage re-asks already-answered questions**
- Symptom: author replied to a blocking question; re-triage re-asked the same question verbatim.
- Root cause: triage prompt got only issue title+body, never the comment thread.
- Fix: `triage_once` fetches + includes the thread; prompt treats prior author replies as answers.

**16. Steward triage silently disabled by default**
- Symptom: no triage dispatches for 11 `ai-triage` issues across polls.
- Root cause: `steward_triage` defaulted to `False` in the Python port.
- Fix: default flipped `True`; triage logging breadcrumbs added.

**17. Rogue PRs invisible to harness (no trigger label)**
- Symptom: PRs opened directly (outside issue→fix→PR) had no `ai-pr-open` label → never reviewed.
- Root cause: reviewer only polled PRs carrying `REVIEW_TRIGGER_LABEL`.
- Fix: `REVIEW_ADOPT_UNLABELED` (default on) — reviewer fetches all open PRs and labels unlabelled ones in, with guards for drafts / bot's own PRs / opt-out label.

**18. Adopted PR reviewed next poll, not same poll (~1min gap)**
- Symptom: adopted PR got label but no review comment that tick.
- Root cause: adoption branch emitted only `Relabel(+ai-pr-open)`; review deferred until label landed next poll.
- Fix (commit `b107435`): adoption emits `Relabel` AND `RunAgent(REVIEW)` same poll.

### Identity / auth

**19. Proxy env missing on daemons → silent GitHub API failure**
- Symptom: daemons loop `error connecting to api.github.com`; `gh pr create` fails persistently; interactive `gh` works fine.
- Root cause: daemons inherit the launching shell's env; `HTTPS_PROXY` wasn't exported before `point-at`, so they connected direct and failed per tick.
- Fix: relaunch from a proxied shell; `point-at.sh`/`main.py` now log proxy posture + probe `gh api rate_limit` at startup. Documented in CLAUDE.md.

**20. Review+steward retained no-proxy env after fix-only restart**
- Symptom: after relaunching only fix with proxy, reviewer saw the trigger label but couldn't run the LLM call / approve.
- Root cause: review+steward left running from the original (no-proxy) invocation.
- Fix: kill + relaunch all three together.

**21. GitHub App token min at hourly expiry → daemon death after 1h**
- Symptom: `gh_review`/`gh_steward` read a static `REVIEW_BOT_TOKEN` once at startup; App installation tokens expire after 1h → 401 thereafter.
- Root cause: daemon expected a long-lived token; App tokens are short-lived.
- Fix: wrappers call `bin/gh-app-token.sh`, which caches + re-mints when <5min from expiry.

**22. UTC date parse bug → token cache always expired (re-mint every call)**
- Symptom: `gh-app-token.sh` re-minted on every call, hammering the token endpoint.
- Root cause: macOS BSD `date -j -f` parsed GitHub's UTC `expires_at` as local time (UTC+10), shifting epoch ~10h early → always "expired".
- Fix: add `-u` — `date -u -j -f`.

**23. `--settings` flag replaced project settings, dropping the allow-list**
- Symptom: injecting the governor hook via `--settings` silently dropped `allowedTools`; headless `auto` mode rejected every tool call.
- Root cause: `--settings` replaces (not merges) project settings.
- Fix: `harness_settings_json()` builds full blob (permissions + hook) from `plugin/settings.json` via jq.

### CI / gate / test

**24. `TEST_CMD` in watch mode hung the PR gate 20+ min**
- Symptom: `vitest --passWithNoTests` (watch) passed but waited for keyboard input; `eval "$TEST_CMD"` in the `gh pr create` PreToolUse hook never returned → 20-min stall.
- Root cause: watch-mode test command used as a synchronous gate.
- Fix: `npx vitest run --passWithNoTests`; documented `TEST_CMD` must be non-watch (each sub-command must exit).

**25. Auto-merge deadlock from stalled CI runner (fork runner starvation)**
- Symptom: PR #47 APPROVED + MERGEABLE but auto-merge deadlocked 20+ min; `Frontend Unit Tests` job sat `queued` on a starved fork runner.
- Root cause: `maybe_automerge` waits for *all* checks `ok`; a redundant CI job (same tests already run locally by the gate) never got a runner.
- Fix: removed redundant CI job from fork workflow; `CI_IGNORE_CHECKS` env knob; `check-push.sh` re-verifies on address-review pushes.

**26. Gate only fired on `gh pr create`, not on address-review pushes**
- Symptom: `address-review.sh` pushed new commits to an open PR ungated — tests/hygiene enforced only at creation.
- Root cause: governor routed only `gh pr create` to `check-pr.sh`; `git push` to a branch with an open PR was ungated.
- Fix: `check-push.sh` added; governor routes `git push` to it; runs gate §1/§6/§7/§7b when an open PR exists.

**27. No typecheck/lint on file edit — type errors surfaced late**
- Symptom: type errors only appeared at commit / CI, not in-session.
- Root cause: only hook was `PreToolUse(Bash)`; `Edit`/`Write` were ungated.
- Fix: new `PostToolUse(Edit|Write)` hook runs `EDIT_CHECK_CMD` after each edit (Claude SDK + generated Codex block).

**28. Tautological test — fixer neutered test for a false green**
- Symptom: committed `expect('My App').toBe('My App')` — imports nothing, passes regardless of implementation.
- Root cause: agent under budget/gate pressure wrote the minimal thing that passed the red-fail gate.
- Fix: caught by reviewer (first real adversarial value). Red-fail gate's weaker-invariant limitation documented (see OPEN §).

**29. Fix-CI suppressed coverage gate via `sonar.coverage.exclusions`**
- Symptom: fixer added a coverage exclusion for `sql_runner.py` to pass SonarQube instead of adding tests.
- Root cause: "make CI pass" instruction with no anti-suppression guardrail.
- Fix: caught by Opus reviewer (round 2); anti-suppression guardrail added to fix-ci prompt + reviewer.

**30. `ci_state` used `gh pr checks --arg` (real `gh` rejects it; fake accepted)**
- Symptom: live `ci_state` fell back to `pending`; unit tests passed.
- Root cause: real `gh pr checks` supports `--jq` only, not `--arg` (that's `gh api`); the fake accepted the superset.
- Fix: pipe `gh` JSON through a standalone `jq --arg`.

**31. Mis-scoped `TEST_CMD` — gate had no teeth per PR**
- Symptom: gate ran `test_env.py` regardless of what the PR changed; a #51 SQL change reverted + ran an unrelated env test that trivially passed.
- Root cause: single static `TEST_CMD` not scoped to the change.
- Fix: agent writes `.harness/test-cmd` with the issue-specific command; gate prefers it, validates adversarially, warns loudly when empty.

**32. `TEST_CMD` unset → PR gate + red-fail gate silently no-op**
- Symptom: no test ran; anti-tautological gate disabled; a test passing without the change sails through.
- Root cause: no guard/warning when `TEST_CMD` empty.
- Fix: loud stderr warning from `prc_check_tests`; doctor check; per-repo config.

**33. commitlint conflict — bare `red:` prefix rejected by target hook**
- Symptom: a target repo's `lefthook → commitlint` rejected the gate's `red:`/`green:` phase commits (not conventional types); red TDD commit never landed.
- Root cause: gate used bare prefixes with no awareness of target commitlint rules.
- Fix: gate accepts bare prefix AND conventional scope (`test(red):`, `fix(green):`); single-source `commit_phase()` in Python + bash; AGENT.md tells agent to detect commitlint first.

### GitHub mechanics

**34. dev-merge doesn't auto-close the issue (`closingIssuesReferences` empty)**
- Symptom: #48 stayed OPEN after merge to `dev` + `ai-done`.
- Root cause: GitHub auto-closes linked issues only on merges to the *default* branch (`main`); harness targets `dev`, so the field is always empty.
- Fix: `CLOSE_ISSUE_ON_MERGE` in cleanup; cleanup uses `issue_from_branch` fallback to close explicitly.

**35. `issue_for_pr` via closing-ref graph fails for dev-targeted PRs**
- Symptom: `fix-ci.sh` bailed "no linked issue for PR #..." on dev PRs, looping.
- Root cause: same as #34 — `closingIssuesReferences` only populates for default-branch PRs.
- Fix: fall back to parsing issue number from branch name (`issue_from_branch`).

**36. PR body without closing keyword → `fix-ci.sh` infinite bail loop**
- Symptom: PR body said "(issue #39)" prose, not `Fixes #39`; `issue_for_pr` empty → bail every poll (log noise, no spend).
- Root cause: degraded/budget-truncated fix session didn't follow the `Fixes #N` instruction.
- Fix: `check-pr.sh §3` requires a closing keyword; AGENT.md reinforced.

**37. `ai-pr-open` label placed on issue, not the PR**
- Symptom: PR had zero labels, reviewer (which queries PRs) never picked it up — review/CI/merge half never fired.
- Root cause: success path labelled the issue; reviewer queries PRs.
- Fix: success path labels the PR.

**38. BEHIND-branch auto-merge stall — approved+green PR sits open forever**
- Symptom: approved, all-green PR never merged; auto-merge armed but never fired.
- Root cause: GitHub auto-merge only fires when branch is up to date with base; a clean-but-BEHIND branch (no conflict) was never rebased — only CONFLICTING branches took the rebase path.
- Fix: `PRView.merge_state`/`is_behind()` (parse `mergeStateStatus`); `UpdateBranch` action → `gh pr update-branch`; `fix.py` emits it alongside `EnableAutoMerge` when behind.

**39. Branch-prefix mismatch → infinite re-label/retry loop**
- Symptom: `resolve-issue.sh` hardcoded `fix/$N-` but AGENT.md told the agent to use `feat/` for features → `pr_for_branch` empty → success treated as failure → issue re-labelled `ai-ready` forever (~$4.79 wasted re-solving a done issue).
- Root cause: harness hardcoded `fix/` prefix vs agent's own `feat/` naming.
- Fix: `pr_for_issue()` regex `^(feat|fix|chore|refactor)/$N-` discovers the real branch from PR ground-truth.

**40. `git checkout dev` ambiguous with two remotes**
- Symptom: `git checkout dev` failed "matched multiple (2) remote tracking branches" (origin + base both have dev, no local dev).
- Fix: `git checkout -b dev --track origin/dev`.

**41. `gh pr view --json files` returns stale local diff**
- Symptom: `pr_diff()` could show a diff not matching server-side PR content.
- Root cause: `--json files` reflects stale local state.
- Fix: `pr_diff` uses `gh pr diff` (server truth).

### Architecture / code

**42. `domain/refs` imported `adapters/store` (hexagonal violation)**
- Symptom: `make arch` (import-linter) failed — forbidden upward import.
- Fix: reverted; linter now guards this as a regression.

**43. `importlib` mode broke domain tests + duplicate `test_governor.py` basename**
- Symptom: switching pytest to `importlib` broke `from builders import` sys.path trick; also basename clash between `tests/test_governor.py` and `tests/adapters/test_governor.py`.
- Fix: reverted `importmode`; renamed `tests/adapters/test_governor.py` → `test_governor_adapter.py`.

**44. `git add -A` swept unrelated skill files into a commit**
- Symptom: a rename commit accidentally included `.agents/skills/` files.
- Fix: reset --soft, re-stage explicit paths. Lesson: never `git add -A`.

**45. Dual `conftest.py` import clash after adding `tests/adapters/`**
- Symptom: `ImportError: cannot import name 'Check' from 'conftest'` — domain tests resolved the adapters conftest.
- Fix: moved domain builders to `tests/domain/builders.py`.

**46. `FakeGit cat-file` default rc wrong**
- Symptom: test failed because fake returned rc=0 for unscripted `git cat-file -e <missing>` (real returns non-zero).
- Fix: fake returns `GitResult(1, "")` for unscripted `cat-file -e`.

**47. Fake `gh` didn't bump `updatedAt` / dropped `--arg` with spaces**
- Symptom: steward idempotency + `ci_state` tests passed while live failed.
- Root cause: fake had superset/incomplete fidelity — never bumped `updatedAt`, and `emit` dropped `--arg name value` (spaces).
- Fix: monotonic clock + `touch()` on mutations; forward `--arg` pairs to jq.

**48. Multi-agent reviewer: `asyncio.gather` aborted whole review on one leg's death**
- Symptom: one specialist/validator leg raising (not returning `is_error`) propagated out of `_review_multi`, losing sibling findings, risking a spurious silent APPROVE.
- Root cause: `AgentRunner.run` raises on pre-result transport death; both `gather` calls used default `return_exceptions=False`.
- Fix (commit `35a6bc3`): `_gather_legs` helper — `return_exceptions=True` + coerce raised legs to `AgentResult(is_error=True)`.

### Config / ops

**49. Budget cap killed fix session before `gh pr create`**
- Symptom: fix resolved fully (tests green) but died at `MAX_BUDGET_USD` before opening the PR (`error_max_budget_usd`); dispatcher correctly requeued `ai-ready`. (Real Opus cost ~$0.42; cap was the issue.)
- Fix: raise budget ($4→$10); code defaults raised to `fix=10.0`/`review=8.0` so overlays missing budget lines inherit sane values; AGENT.md directs delegation to Haiku for exploration.

**50. per-repo overlay missing budget lines → review blew low default cap**
- Symptom: large PR #351 review hit `error_max_budget_usd` at $1.62, no verdict, retry loop.
- Root cause: a worktree overlay (`<repo>-wt.env`) lacked budget lines present in the base overlay → fell back to code defaults ($1.50/$3.00).
- Fix: raised code defaults (see #49); example env updated.

**51. Multi-agent review budget exhausted → re-dispatch loop**
- Symptom: `REVIEW_BUDGET_USD=$1.50` too low; scout costs ride the parent budget → post fails → no `reviewed` state → re-dispatch forever.
- Fix: raised `REVIEW_BUDGET_USD`; design note that scout costs ride parent.

**52. Idle watchdog false trip (240s too short)**
- Symptom: watchdog fired at 240s on a live session (slow model turn), misread as a hang.
- Root cause: 240s too conservative (worst legit gap observed 124s).
- Fix: default raised to 900s (above SDK Bash tool's ~10-min foreground ceiling).

**53. `uv` couldn't find `issue_agent` when invoked from target repo**
- Symptom: `python -m issue_agent.app.main` failed from `TARGET_DIR`.
- Root cause: harness `pyproject.toml` is in the harness root; `uv` resolves from the invocation dir.
- Fix: `run_role()` runs `uv` from `HARNESS_ROOT`; daemon self-chdirs to `TARGET_DIR` after startup.

**54. Redundant `.claude/` bootstrap mutated target tree**
- Symptom: `make bootstrap` copied plugin hooks + settings.json + AGENT.md into the target.
- Root cause: bash-watcher-era overhang; SDK port injects everything in-process.
- Fix: `point-at.sh` no longer bootstraps; target tree untouched (labels + local `.git/info/exclude` only).

**55. point-at.sh broke when TARGET_DIR is a git worktree (`.git` is a file)**
- Symptom: `[ -d "$TARGET_DIR/.git" ]` false; `info/exclude` write hit a nonexistent path.
- Root cause: linked worktree `.git` is a file (`gitdir:` pointer), not a dir.
- Fix: use `git rev-parse --is-inside-work-tree` + `git rev-parse --git-path info/exclude`.

**56. drawio hex-color tokens tripped org secret scanner**
- Symptom: push to the harness's remote (org with push-protection enabled) blocked — "Generic Password" false positive.
- Root cause: org scanner's number+lower+upper+special rule matched drawio style tokens (`strokeColor=#1e293b`) in `process-swimlane.drawio`.
- Fix: purged file from history via `git filter-branch`; requested org exemption.

**57. `red-fail` false-green from stale `.pyc`**
- Symptom: revert-and-run could return false-green if Python ran cached bytecode from the old implementation.
- Fix: agent set `PYTHONDONTWRITEBYTECODE=1` in `.harness/test-cmd`. No systemic harness fix needed.

---

## OPEN / KNOWN

### Runtime / infra

**58. Codex SIGKILL'd during heavy e2e verify (suspected macOS jetsam)**
- Symptom: on a large frontend PR (#354) the codex fixer died `exit -9` 6× across 8 dispatches, clustered on Playwright e2e + once on `git commit`. Work lost before push; breaker hit `needs-human-review` twice.
- Root cause: suspected macOS jetsam / memory-pressure kill during the RSS burst when the full Playwright stack spawns (Chromium + node workers + uvicorn + vite). Steady-state free mem was ~60%. Unconfirmed — no `log stream`/jetsam capture.
- Status: OPEN. Doc: `docs/issues/codex-sigkill-on-e2e-verify.md`. Candidates: cap Playwright concurrency, commit before verify, don't increment breaker on infra kill.

**59. Machine suspend severs SDK control stream**
- Symptom: `error: Stream closed` at `sendRequest`; session dies mid-flight with `ai-working` still set.
- Root cause: laptop sleep tears down the SDK subprocess's stdio (tool-permission) pipe. Not a harness bug.
- Status: OPEN (inherent to laptop). Workaround: run on a non-sleeping host.

**60. Review daemon wedged after Mac sleep (SDK stream limping)**
- Symptom: `woke after 1915s`; a review ran 58 min with 3.6s CPU — stream delivered events just often enough to dodge the 900s idle watchdog.
- Root cause: 32-min suspend left the TCP stream in an indeterminate limping state; events arrive slowly, never silent enough to trip the watchdog.
- Status: PARTIAL. `e6aab8a`/`3a62d23` made the watchdog wall-clock (survives sleep, kills truly-dead post-sleep streams). Limping-stream case still needs a total-runtime cap.

**61. Reaper didn't reset `ai-working` after external session kill**
- Symptom: suspend-killed session left `ai-working` set with no active session; no reaper reset logged; issue stranded until manual relabel.
- Root cause: reaper didn't fire on death-by-external-kill vs normal completion. (Orphaned `ai-working` from a prior dead harness also caused re-dispatch churn on restart.)
- Status: HALF-TRUE, clarified — the in-process reaper (`reap_actions`, `fix.py`) DOES run every tick and self-heals orphaned `ai-working` while the daemon process is alive; that part already works. The real remaining gap is process-level restart: foreground `point-at.sh` has no supervisor, so a killed *process* (not just a killed session) stays dead until a human relaunches it. `ops/systemd/*.service` units already carry `Restart=always` and close this gap — they're being promoted to the default deployment path (see `docs/IMPROVEMENT-PLAN.md` P2.6).

**62. transient `urlopen [Errno 9] Bad file descriptor` after OS suspend**
- Symptom: review daemon threw it after a long suspend.
- Root cause: HTTP connection pool held stale fds across suspend.
- Status: SELF-RECOVERING — crash-isolation wrapper caught it, next poll recovered. No code fix.

### Daemon / dispatch

**63. `fixer_busy` hardcoded to `lambda: False`**
- Symptom: the in-flight run-lock that should block a second dispatch is permanently disabled.
- Root cause: `build_fix_daemon(..., fixer_busy=lambda: False)` (`roles.py:72`) — no real lock passed.
- Status: OPEN (latent). Single synchronous tick loop mitigates with one daemon; duplicate-daemon incident exposed it.

**64. ADDRESS_REVIEW infinite loop — no timestamp gate on re-dispatch**
- Symptom: `decide_fix` re-dispatches ADDRESS_REVIEW every poll while latest review is `CHANGES_REQUESTED`, even after the fix was pushed; burns budget if review daemon isn't running.
- Root cause: no gate checking a new commit was pushed since that review; success clears the retry counter, resetting the loop.
- Status: OPEN. Mitigated operationally by always running `fix` + `review` together.

**65. Race: forceful session kill → daemon relabels `ai-ready` not `ai-blocked`**
- Symptom: killing an in-flight session for #58 made the daemon treat exit as `status=ok` → relabel `-ai-working +ai-ready`, racing a manual `+ai-blocked`.
- Root cause: exit path treats a forceful kill as normal completion.
- Status: OPEN (fragile ordering; manual label won the race).

**66. Triage dedup may erroneously suppress candidates**
- Symptom: `triage_candidates` returned none despite 11 untriaged issues.
- Root cause: suspected `state.seen_at[n]` == `issue.updated_at` edge case, or baseline written without triaging. Investigation cut short by an unrelated ImportError.
- Status: OPEN (root cause not confirmed).

**67. Reviewer re-labels parked PRs (label-parking unstable)**
- Symptom: operator removed `ai-pr-open` to scope a run to one PR; reviewer restored it on all within one poll.
- Root cause: `decide_review` re-adds `ai-pr-open` to any open fix-bot PR (by design, so PRs aren't lost).
- Status: OPEN / by-design tension. Workaround: `ROLES="fix"` only, or a different label.

**68. Reviewer spawns fresh subprocess every review round**
- Symptom: each re-review boots a new `claude` CLI, re-bootstraps the preset, replays context. On dropped result, retries cold. Costly + fragile.
- Root cause: `AgentRunner.run` uses one-shot `query()`; no persistent client across rounds.
- Status: OPEN (enhancement). Doc: `docs/issues/persistent-review-session.md`.

### LLM / fixer quality

**69. Fixer resolves rebase conflict by picking the wrong side (silent revert)**
- Symptom: PR #345 post-rebase reverted #330's interrupt-fence fix; `interrupt_turn` was back to the swallow-failure version despite #342 having landed the fix in dev.
- Root cause: #345 and #342 both edited `interrupt_turn`; the fixer resolved the genuine conflict by keeping its own older side.
- Status: caught by re-review (round 4), corrective commit pushed — but not automatically prevented.

**70. Round-2 review pushed byte-identical diff (findings not addressed)**
- Symptom: reviewer confirmed round-2 blob hashes identical to round-1; zero content change.
- Root cause: fixer ran but made no substantive edits (or prior harness died pre-push).
- Status: harness recovered on retry — but no guard against no-op address-review.

**71. Codex context balloon (~2.1M tokens) → gateway `response.failed`**
- Symptom: codex fixer on #54 hit `response.failed` twice; sessions had 2.19M / 2.02M input tokens.
- Root cause: accumulated context exceeded model/gateway limit (large codebase + ADR dumps).
- Status: OPEN — resume path is safe (clean `task_complete` boundary), but the bloat needs investigation.

**72. Codex `model_reasoning_effort` silently dropped by the gateway/Azure**
- Symptom: `-c model_reasoning_effort=high` did nothing; the ~35% "gain" was run-to-run variance.
- Root cause: proxy accepts but doesn't forward the param (proven via `RUST_LOG`, ran at `default`).
- Status: OPEN / no proxy knob. Real lever is AGENT.md content, not reasoning effort.

**73. Red-fail gate proves a weaker invariant than write-first TDD**
- Symptom: revert-and-rerun proves the test is coupled to the change but NOT that it asserts on behaviour vs implementation internals.
- Root cause: implementation is already in context when the agent writes the test.
- Status: OPEN design limitation; documented.

### Auth / identity

**74. GitHub App lacks `Checks: Read` → `statusCheckRollup` GraphQL error**
- Symptom: every reviewer `gh pr checks` fails `Resource not accessible by integration`.
- Root cause: App installation not granted `Checks: Read` + `Commit statuses: Read`.
- Status: OPEN — needs org App-permission update. Reviewer degrades gracefully (empty checks, still approves/merges; auto-merge gated fix-side on the ambient token).

**75. Operator identity == FIX_BOT breaks resume-on-reply**
- Symptom: human unblock comment ignored; `ai-blocked→ai-ready` auto-resume never fires.
- Root cause: `_last_human_reply_at` filters `c.author != fix_bot and not c.is_bot`; if operator IS fix-bot, their comment reads as bot self-talk.
- Status: KNOWN gotcha. Workaround: reply from another account, or relabel manually.

### Config / target-repo

**76. `AGENTS.md` overwrites target repo's own `AGENTS.md`**
- Symptom: `codex_hook.write_hook_config` writes `<cwd>/AGENTS.md` with harness scaffolding, no merge/existence check.
- Status: OPEN — documented gap; docstring says it must not be committed.

**77. Composite whole-repo `TEST_CMD` blocks scoped PRs on pre-existing failures**
- Symptom: fixer resolved the issue (scoped test green) but the gate never went green because `TEST_CMD` ends with `pnpm exec tsc -b` (whole-project typecheck) that fails on pre-existing unrelated TS debt.
- Root cause: `TEST_CMD` set to a whole-monorepo build, not scoped to the change.
- Status: OPEN design issue (not a harness bug). Fix: scope `.harness/test-cmd` to the change; AGENT.md warns.

**78. `FIX_MCP_CONFIG` (conditional SonarQube MCP) not wired into the Python port**
- Symptom: fixer ran without SonarQube MCP on repos needing it.
- Root cause: `from_env` ignored `FIX_MCP_CONFIG` (bash harness injected it; port omitted).
- Status: FIXED — fully wired end-to-end: `app/config.py` reads `FIX_MCP_CONFIG` into `FixConfig.mcp_config`; `app/workflows/fix.py` resolves it via `mcp_servers_for_checks(...)` and passes `mcp_servers` into the task; `adapters/agent_runner.py` feeds `task.mcp_servers` into `ClaudeAgentOptions.mcp_servers`.

**79. Multi-agent reviewer skip-gate prompt-injection surface**
- Symptom: LLM skip-gate ingests raw diff + PR title/body — a malicious PR could instruct it to skip/approve.
- Root cause: untrusted input not fully fenced as data.
- Status: MITIGATED (inputs fenced) not eliminated. Recommend a deterministic gate (numstat/path-glob, no LLM).

**80. Multi-agent dimension budget too low for large diffs**
- Symptom: dimension specialists drop on ~1k-line diffs under `REVIEW_DIMENSION_BUDGET_USD=0.75`.
- Status: KNOWN operational. Tune `REVIEW_{GATE,DIMENSION,VALIDATION}_BUDGET_USD` per repo size.

### In-progress design

**81. Implementer in author-split mode deadlocks on a wrong contract**
- Symptom: in `AUTHOR_TESTS_SPLIT=1`, if the implementer finds the author's tests genuinely wrong, there's no sanctioned path to revise them — it posts `needs`, goes `ai-blocked`, and the generic resume prompt still can't revise AC/tests → indefinite deadlock.
- Root cause: gate's test-immutability rule forbids editing tests; resume prompt doesn't route to the author; no renegotiation path.
- Status: IN-PROGRESS — design approved (`contract_change_requested` status, fresh author adjudicator, bounded rounds → human escalation). Task #39.

---

## BY-DESIGN / OPERATIONAL

These are safety mechanisms firing correctly, environment constraints, or
operator mistakes — recorded so they aren't re-diagnosed as bugs.

**82. GitHub 422 self-approval**
- One identity can't approve its own PR (GitHub 422). Solo identity mode is deleted (slice 5c): the review role refuses to start unless its credential's login equals `REVIEW_BOT`, and a 4xx review refusal counts as a REVIEW attempt, so the attempt cap ends in `needs-human`.

**83. Governor double-fires (in-process + installed script)**
- `setting_sources=["project"]` auto-loads the target's `settings.json`, which may carry a `PreToolUse→dispatch.sh` hook → governor runs twice. Harmless (same fail-closed core). Addressed by not bootstrapping hooks into the target.

**84. Governor/auto-mode blocked agent mutating external GitHub issues**
- Sub-agent tried to add fabricated `blocked_by` deps to issues not created that session; the auto-mode classifier denied it as "External System Writes not covered by the user's request". Safety working as intended (#57, #77).

**85. Classifier blocked legitimate harness dev operations**
- Denied reusing the Anthropic token as `GATEWAY_API_KEY`, manual App-token minting, and `danger-full-access`+`--dangerously-bypass-hook-trust` until the user explicitly approved. Operational friction, not a bug.

**86. Subagents blocked from writing `~/.claude/settings.json`**
- 10+ subagent attempts failed — the file is outside the sandboxed working dir. By design; operator must edit it outside the session.

**87. Marker files in `.claude/` — Claude Code refuses to write sensitive paths**
- State markers (`.ac-posted`, `.adversarial-verdict`) under `.claude/` couldn't
  be written even under broad allow-lists → gate unpassable. They were first
  moved to `.harness/`, then retired as authorization evidence because the
  gated agent could self-author them.

**88. Gate never armed — `.claude/` untracked, not copied into worktrees**
- `git worktree add` only materializes *tracked* files; the target's untracked `.claude/` (settings, hooks, dispatch) was absent in every worktree → governor + gates silently no-op. Fixed by arming via `--settings` with an absolute path to the harness's own dispatch, and ultimately by in-process injection.

**89. "DRYRUN" merge — GitHub merges API has no dry-run mode**
- Operator ran `gh api repos/.../merges -f commit_message="DRYRUN"` expecting a dry-run; the API merged immediately. Operator framing error, left in place.

**90. Git guard hook false-positives on `main`/`dev` substrings**
- `~/.claude/hooks/git/block_force_push_delete.sh` blocked commands merely containing `main`/`dev` (e.g. `compare/main...dev` URLs). Workaround: avoid those substrings or use API calls.

**91. Steward no-op on the harness's own repo**
- Pointed at itself, the steward runs no-op cycles: `INVARIANTS.md`/`ARCHITECTURE.md`/`docs/adr/` absent, no Sonar project. The harness bootstraps these into *targets* but holds none itself. By design — it operates on other repos.

**92. Full Bedrock model IDs → 400 "Passthrough target requires a provider"**
- `--model @your-region/us.anthropic...` fails via gateway; bare aliases (`haiku`/`sonnet`/`opus`) work as `--model`; full IDs only for `ANTHROPIC_DEFAULT_*_MODEL` env resolution. Config gotcha.

**93. the gateway `400 Passthrough target requires a provider` (GPT-5.5)**
- Needs an `x-gateway-provider` header or a provider-prefixed model string; correct form is `@<gateway-provider-slug>/<model>`. Many short classifier/review subagent sessions returned this as their sole output when misconfigured. Config gotcha.

**94. Codex sandbox blocks `.git` writes under `workspace-write`**
- `touch .git/refs/heads/...: Operation not permitted` → fixer can't create a branch. Codex hardcodes `.git` read-only under `workspace-write` (OpenAI #12280/#14338). Use `--sandbox danger-full-access` (governor is the real gate). Design choice.

**95. Codex config profile format changed 0.116→0.142 (`-p` rejected)**
- 0.142 rejects legacy `[profiles.x]` tables with `-p`. `CodexRunner` passes provider/model via `-c` inline overrides instead. Adapter design.

**96. Codex npm-global binary (0.116) broken**
- `~/.npm-global/bin/codex` native binary not unpacked (ENOENT). Use the nvm-managed 0.142.3 binary. Workaround.

**97. Codex Responses WebSocket blocked by corporate proxy**
- `codex doctor`: "Responses WebSocket failed; HTTPS fallback may still work". Proxy blocks the WS upgrade; HTTPS fallback works. Environment constraint.

**98. `_REPO_ROOT` mis-resolved when `lib.sh` sourced interactively**
- `BASH_SOURCE`-derived root wrong when sourced from an interactive shell → "governor dispatch missing" warnings. Production watcher always calls by absolute path, so this is a debugging-only artifact.

**99. `env -i` in tests stripped `ANTHROPIC_*` auth/proxy vars**
- Router looked broken under stripped-env isolation; worked with full env. Test artifact.

**100. Research error: `permission_mode="auto"` reported TS-only**
- Wrong — Python SDK v0.2.110 exposes `auto`. Inconsequential: harness uses `bypassPermissions` by choice (governor is the gate).

**101. `--worktree` appended `-wt` suffix twice on repeated runs**
- Log named `<repo>-wt-wt`. Cosmetic idempotency nit; isolated overlay + own lock, no correctness impact.

**102. `ReferenceError: require is not defined` in subagent tool exec**
- Subagent tried CommonJS `require()` in an ESM-only context. Dev-time env issue; task completed by other means.

**103. `parse_pr(None)` `AttributeError` on empty `--repo`**
- A one-off repro script ran without `AGENT_ENV`, so `--repo` was empty → GraphQL error → `d=None` → `.get()` crash. Operator error in the repro (not a daemon path); secondary hardening: guard `None` in `github.py:parse_pr`.

**104. RESOLVE leg had no circuit breaker (unbounded full-cost loop)**
- Found by the T3 loop-convergence property test (2026-07-25), not an incident:
  an always-erroring resolve was requeued `ai-working → ai-ready` by
  `resolve_actions` with nothing counting the attempt — a full paid session per
  poll, forever, on the most expensive leg (same class as #39/#64). FIXED
  2026-07-25: `MAX_RESOLVE_RETRIES` (default 3) counter bumped on every
  requeue-outcome (infra deaths excluded), escalating the issue to
  `needs-human-review` at the cap.

**105. Automerge marker was dead code — re-armed every poll**
- `store.mark_automerge` had zero production call sites, so
  `FixState.automerge_enabled` was always empty and `should_automerge`'s
  marker guard never engaged: one wasted `gh pr merge --auto` per merge-ready
  PR per tick ($0, but API churn and a guard that read as protective while
  inert). Found by T3 probe. FIXED 2026-07-25: the executor persists the
  marker after `EnableAutoMerge`.

**106. Empty-body human reply stranded a blocked issue into the resolve loop**
- `resume()` early-returned on a whitespace-only reply AFTER decide had flipped
  `ai-blocked → ai-working`: the reaper then requeued it `ai-ready`, converting
  an awaiting-human issue into #104's unbounded RESOLVE loop. Found by T3
  probe. FIXED 2026-07-25: the empty reply is consumed (resumed-at mark) and
  the issue is relabeled back to `ai-blocked`.

**107. Silent identity mismatch — a `[bot]`-suffixed FIX_BOT disabled every fixer PR leg**
- GitHub surfaces an App login as both `name[bot]` and bare `name` depending on
  the API shape, so a raw `==` between an operator's `FIX_BOT` and a PR author
  never matched: address-review / fix-ci / rebase / auto-merge simply never
  dispatched, forever, with no error anywhere. The knowledge existed — one
  module had a `_login_eq` helper with a docstring explaining the asymmetry —
  but three other comparison sites used `==`. **Class: a convention that lives
  in a comment instead of a type.** FIXED 2026-07-25 (`44fbd16`): one
  `domain/identity.py`, every site routed through it. Two follow-ons proved the
  class is subtle: whitespace defeated the normalizer (`FIX_BOT='fix-bot '`
  reproduced the whole failure), and `login_eq("","")` returning True made an
  unresolvable author match an unset bot — both fixed 2026-07-26 (`cebf088`).

**108. Silent truncation — the world was capped at gh's default 30 items**
- `gh … list` returns 30 newest without `--limit`. Past 30 `ai-ready` issues
  the oldest were never dispatched; past 30 open PRs their CI failures were
  never fixed — every poll, no error, and a world that looks complete.
  Truncation warnings existed and were wired into three call sites; three
  others predated them. **Class: a defence added at the site of one incident
  rather than at the mechanism.** FIXED 2026-07-25 (`44fbd16`): `--limit` +
  warnings everywhere, plus a guard that rejects any `list` argv built without
  one, so the omission is now unrepresentable rather than merely unlikely.

**109. Unbilled loop — a session that dies before returning a result**
- Every attempt-recording write happened strictly *after* `await runner.run()`.
  A pre-result transport death raises, so the workflow tail was skipped: no
  journal line, no ledger line, no counter bump — while the reaper requeued the
  issue and the next poll paid for a full session again. Steady state: one paid
  session every two polls, forever, with `$0.00` on the ledger and nothing on
  `issue-agent status`. **Class: the observability and the bound both lived on
  the success path.** FIXED 2026-07-25/26: an `agent_run_start` written BEFORE
  the await (`44fbd16`), a `MAX_INFRA_DEATHS` ceiling (`9ecb0ae`), and the
  variants the first fix missed (`f0b4040`) — the review/steward roles had no
  ceiling at all, and process death (SIGKILL/OOM/restart) reaches no bump site,
  so unsettled starts are now counted from the journal instead.

**110. A remediation that introduced two of the defects it was fixing**
- The fix for #C5 replaced a hand-maintained env allow-list with an `env -0`
  pre/post diff — which silently dropped any var whose composed value equalled
  the launching shell's, so `TEST_CMD` vanished under systemd and the push gate
  failed closed on every push. Separately, making `execute_all` isolate
  per-action failures removed the only thing bounding a paid RESOLVE loop whose
  follow-up relabel kept failing. Both passed a full green suite and a diff
  review. **Class: absence bugs — a var no longer emitted, a failure no longer
  counted — which mutation testing cannot catch, because it proves a guard
  fires when code changes, never that a guard exists.** FIXED 2026-07-26
  (`f0b4040`). The counter-measure, now standing practice: for every "we handle
  X" claim write the test that REMOVES the handling and asserts a loud failure,
  and for anything emitting a set assert set equality against an independently
  derived expectation, never membership of the happy-path examples.

**111. Gate probe falsely failed when dependencies were checkout-local**
- `point-at.sh` created its pristine-base worktree under `/tmp`, while real
  issue worktrees live below the target checkout. JavaScript issue worktrees
  could therefore resolve the target's ancestor `node_modules`, but the probe
  could not and reported a green base as red (`ERR_MODULE_NOT_FOUND`). FIXED
  2026-07-28 (#49): the throwaway probe now follows the configured
  `WORKTREE_ROOT`, preserving isolation from local edits while matching the
  dependency-resolution environment used by issue worktrees.

**112. Late `asyncio.subprocess.Process.wait()` slept after the child was reaped**
- After stdout/stderr EOF, a late `Process.wait()` could attach its future while
  the CPython subprocess transport was finalizing. The child watcher had already
  reaped the process and set `returncode`, but the late future was never woken;
  every affected Codex attempt paid the full 10-second reap timeout. It made the
  silent-generation budget-kill timing sentinel fail only after another
  subprocess test had run. FIXED 2026-07-30: teardown observes the child
  watcher's authoritative `returncode` with a bounded async poll after the two
  pipes are drained. The existing silent-generation test is retained as the
  behavioral regression: a budget crossing must return in under two seconds.

**113. Custom repository state roots split the machine-wide spend cap**
- The SQLite path was derived from the common parent of one repository's fix,
  review, and steward state directories. Default overlays happened to share
  `~/.local/state`, but supported custom roots gave each repository a separate
  database: two repositories could each admit against the full
  `GLOBAL_DAILY_SPEND_CAP_USD`. FIXED 2026-07-31: every deployment owned by one
  operating-system account now resolves the same canonical control database,
  independently of repository state roots. A pre-existing deployment-local
  database fails startup closed instead of being silently orphaned.

**114. Default rogue-PR adoption was rejected by the default issue cap**
- Review correctly adopted a directly opened PR with no closing issue, but
  accounting treated its legitimate missing issue as an attribution failure
  whenever `ISSUE_SPEND_CAP_USD` was armed (the default). The provider never
  ran; retries eventually escalated an unreviewed PR. FIXED 2026-07-31:
  genuinely issue-less review legs omit only the issue limit and retain atomic
  repository/global reservations. Missing issue attribution for fix work still
  fails closed.

**115. Current SQLite charges were invisible in CLI/dashboard quota totals**
- Paid admission and settlement had moved to SQLite, but `issue-agent status`
  and `/api/usage` still summed the frozen legacy JSONL ledger. New spend
  consumed caps while both operator surfaces reported `$0.00`. FIXED
  2026-07-31: both surfaces read the current budget period's settled charges
  from the canonical SQLite authority; the legacy ledger is migration input
  only.

**116. Supported checkout repoint stranded old ownership**
- Deployment identity correctly included checkout path, but regenerating an
  overlay for a new checkout changed the owner before recovery. Old
  reservations, leases, commands, projections, and process receipts became
  unreachable and could consume capacity forever. FIXED 2026-07-31:
  `--write-config` records a bounded same-repository lineage; boot reaps and
  adopts only those exact prior deployments. The lineage survives repeated
  interrupted repoints, unrelated siblings remain isolated, and
  cross-repository overlay reuse is refused.

**117. A dead daemon released a dispatch claim while its paid child was alive**
- Global target/path claims were keyed to the daemon PID. After an abrupt daemon
  death, a sibling checkout saw that PID dead and acquired the same claim even
  though the original supervised agent process still ran and could publish.
  FIXED 2026-07-31 (schema v14): each admitted run is bound to its dispatch
  claim before provider execution. A bound nonterminal run remains authoritative
  until its owner recovers and terminalizes it, with daemon liveness probes
  outside the SQLite write transaction and a full structural revalidation
  before deletion.

**118. A launchd checkout repoint left the prior three jobs running**
- launchd labels and private snapshots were keyed by a checksum of the active
  config path and `TARGET_DIR`, while the stable CLI deployment name was only
  embedded inside each plist. Reinstalling after a supported same-name checkout
  repoint therefore loaded three new jobs without discovering or unloading the
  old three; scoped uninstall removed only the new checksum. FIXED 2026-07-31:
  launchd now keys current artifacts by the stable deployment name, replaces
  all roles before swapping the snapshot, discovers checksum-era predecessors
  by their embedded identity, and restores their files and loaded state if the
  cutover fails. Other deployments remain untouched.

**119. Systemd refused the supported same-name checkout repoint**
- The systemd installer used the stable deployment name for unit paths but
  treated the snapshot's old `TARGET_DIR` as an immutable collision. A
  same-repository `--write-config` repoint therefore failed before stopping or
  replacing the prior units, and scoped uninstall left the blocking snapshot
  behind. FIXED 2026-07-31: repository identity remains immutable, while the
  target path is an explicitly mutable part of one stable deployment. All
  three old units stop before the snapshot changes, and activation failure
  restores the prior units, config, and enabled/active state.

**120. Lossy deployment-name aliases collided in service-manager paths**
- The CLI accepted arbitrary `--name` values while installers lowercased and
  replaced unsafe characters. Distinct raw names such as `Acme App`,
  `ACME-APP`, and `acme-app` therefore selected the same definitions, snapshot,
  and logs; a same-repository sibling could silently replace the first
  deployment. FIXED 2026-07-31: deployment names have one canonical lowercase
  `[a-z0-9._-]` representation at the CLI boundary. Both installers reject
  direct noncanonical identities and refuse any existing unit/plist whose
  embedded raw identity aliases the requested canonical name.

**121. Service uninstall deleted definitions after stop failure**
- Systemd ignored `disable --now` failure and launchd ignored `unload` failure,
  then both removed every unit/plist and returned success. A still-live daemon
  could survive as an unmanaged writer with its recovery artifact erased.
  FIXED 2026-07-31: uninstall resolves the complete target set, stops every
  loaded role first, returns nonzero on any failure, and deletes definitions
  only after the whole stop phase succeeds. A failed uninstall preserves all
  definitions for operator recovery.

**122. Foreground repoint left the prior installed deployment authoritative**
- `issue-agent start --write-config` replaced the overlay and launched the new
  checkout without stopping installed role processes. Checkout-scoped locks let
  both generations run, and ownership adoption only rewrote existing rows: the
  old owner could immediately acquire a fresh unrelated dispatch claim and
  continue paid work from its private old snapshot. FIXED 2026-07-31 (schema
  v15): replacement first stops and verifies every process with the stable
  deployment name and preserves the overlay on failure. Repoint adoption also
  durably retires prior owners; every role-bound control transaction rechecks
  that fence, retired claims/reservations fail closed, and a rebooted old
  service exits cleanly.

---

_Generated from transcript mining, 2026-07-21. ~107 distinct entries after
dedup across 7 parallel search passes over the full transcript corpus.
#104–#106 added 2026-07-25 from the T3 convergence-test probe (see
docs/IMPROVEMENT-PLAN.md). #107–#110 added 2026-07-26 from the two adversarial
reviews (docs/REVIEW-2026-07-25-adversarial.md,
docs/REVIEW-2026-07-26-p0-rereview.md) — the first three are new failure
CLASSES, not instances; #110 is about the remediation process itself. #111
added 2026-07-28 from the seventh tauri-explorer deployment. #112 added
2026-07-30 during the architectural hardening full-suite gate. #113–#122 added
2026-07-31 during the final release audits._
