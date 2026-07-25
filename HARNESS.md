# Autonomous Issue Agent — the working harness

This is the runnable implementation of the design in `README.md` and
`01-`…`09-*.md`. Start it, file issues at your leisure, come back to merged PRs.

The original nine docs are the **spec**; this file documents the **code** that
implements it, the decisions made while building, and how to run and test it.

> Design docs: [README.md](README.md) (index) → [01](01-plugin-structure.md) …
> [09](09-bootstrap-and-ops.md). This file is the operator's manual.

---

## What you get

Three independent systems coordinating only through GitHub, plus the enforcement
plugin they all obey:

| Component | Path | Role |
|-----------|------|------|
| **Enforcement plugin** | `plugin/` | tree-sitter governor + gate hooks + `AGENT.md` + `settings.json`. Installed into each target repo's `.claude/`. |
| **Fix agent** | `agent/` | resolves issues → PR, fixes CI, addresses review, auto-merges, cleans up. Single sequential fixer. |
| **Review agent** | `review/` | reviews PRs as a *different* identity; APPROVE / REQUEST_CHANGES / ESCALATE. |
| **Steward** | `steward/` | persistent, memory-in-files owner of conceptual integrity. Proposes; humans dispose. |
| **Master** | `bin/master.sh` | on-demand "what happened / what's stuck?" query. |
| **Ops** | `bin/`, `ops/`, `Makefile` | bootstrap, doctor preflight, launchd/systemd installers. |
| **Demo target** | `examples/demo-target/` | a tiny calculator with seeded issues to watch the loop run. |
| **Tests** | `tests/` | 134 tests: governor (42 pytest), hooks (49 real git), orchestration (43 fakes). |

---

## Quickstart (CLI)

The `issue-agent` CLI is the operator front door — install once, then drive the
harness from **inside any target repo**. It auto-detects the repo + working copy
from your cwd, writes/reuses a per-repo config overlay, and delegates to the same
tested engine `make point-at` uses.

```bash
make install                     # uv sync (editable) + ~/.local/bin/issue-agent shim

cd /path/to/target-repo          # a normal working copy of the target repo
issue-agent start                # DEV / FIRST RUN — foreground, Ctrl-C to stop

issue-agent ready 42             # label issue #42 ai-ready → the fixer picks it up
issue-agent status               # which watchers are running + recent log tails
issue-agent stop                 # stop this repo's foreground watchers

# Once `start` has proven your config works, run it FOR REAL under systemd/
# launchd — see "Running for real" below:
issue-agent install-service      # installs + enables + starts the OS service units
```

**`issue-agent start` is the dev / first-run flow** — it runs in the
**foreground** (Ctrl-C stops all watchers; logs tail live to the terminal, and
to `$LOG_DIR/<role>-<name>.log`, default
`${XDG_STATE_HOME:-$HOME/.local/state}/issue-agent/logs/`, override with
`LOG_DIR`). Foreground has **no process supervisor** — see "Running for real"
below for the documented default for unattended operation:

1. **Auto-`init`** — if `config/shared.env` (global identity/secrets — App key,
   Jira, gateway) doesn't exist yet, it runs the interactive `init` first. So a
   brand-new machine needs nothing but `issue-agent start`.
2. **Auto-detect** — `REPO` + `TARGET_DIR` from the cwd (`--repo`/`--target-dir`
   override). An existing overlay whose `REPO=` matches (e.g. the hand-authored
   `myapp.env`) is reused transparently.
3. **Auto-discover the test command** — when `--test-cmd` is omitted, a cheap
   read-only LLM probe (Haiku, `Read`/`Glob`/`Grep` only) inspects the repo and
   writes its `TEST_CMD`. **If the repo has no tests, it proceeds — but behind a
   loud `THIS REPO HAS NO TESTS` banner** (the PR gate, red-must-fail gate, and
   reviewer test run all lose their teeth without one). Pass `--test-cmd '<cmd>'`
   to skip discovery.
4. **Synthesize/reuse** `config/repos/<name>.env`, then hand off to
   `bin/point-at.sh` (doctor → base branch → labels → run all three watchers).

The overlay is written once and **reused** thereafter (edit by hand, or
`--write-config` to regenerate). Flags: `--roles fix review steward` (subset),
`--base-branch`, `--test-cmd`, `--max-budget`, `--max-ticks` (bound the loop),
`--no-init` (error instead of auto-init on first run).

> **Why install from the checkout, not `pip install`.** The daemon loads
> `plugin/AGENT.md`, `plugin/hooks/bash_governor.py`, and `config/mcp/*.json` by
> walking up from its own file to this checkout — none of those ship in a wheel.
> `make install` does an **editable** `uv sync` into the checkout's `.venv` (the
> same env `point-at.sh`'s `uv run` uses) and drops a launcher shim on PATH, so
> that resolution stays intact. Keep the checkout in place.

The lower-level `make point-at REPO=<name>` entry (below) still works unchanged —
the CLI is a thin wrapper over it.

### Running for real: systemd/launchd is the documented default

Foreground `issue-agent start` / `make point-at` is the **dev / first-run
mode** — good for proving a repo's config (identities, `TEST_CMD`, GitHub
access) actually works, with logs streaming live where mistakes are visible
immediately. It has **no process supervisor**: a laptop suspend, an OOM/jetsam
kill, or just closing the terminal kills the daemons and nothing brings them
back. That's exactly the class of incident in FAILURE-MODES #58–#62 — Codex
SIGKILL'd under memory pressure (#58), a machine suspend severing the SDK
control stream (#59), a review daemon "limping" for 58 minutes after a Mac
sleep (#60), a reaper that (partially) couldn't reset `ai-working` after an
external kill (#61), and a transient bad-fd after suspend (#62). Once you've
confirmed `issue-agent start` works, run it for real instead:

```bash
issue-agent install-service      # launchd (macOS) / systemd (Linux) user units
```

This is exactly `bin/install-launchd.sh` / `bin/install-systemd.sh` under the
hood (`src/issue_agent/app/cli.py:cmd_install_service`), invoked against the
config `issue-agent start` already composed for this repo. On Linux it fills
in the `ops/systemd/issue-agent-{fix-watcher,review-watcher,steward}.service`
templates and installs them to `~/.config/systemd/user/` as
`issue-agent-<role>-<name>.service`, where `<name>` is derived from
`TARGET_DIR` — so pointing the harness at several repos and running
`install-service` in each gives every repo its own independent, non-colliding
set of units (each with its own config snapshot under
`~/.config/issue-agent/<name>.env`, `chmod 600`). Every unit ships
`Restart=always` + `RestartSec=30`: systemd restarts a killed *process* on its
own, closing the whole suspend/jetsam/process-death class above. It's not a
full recovery story — an in-flight session's actual work can still be lost —
but the daemon's own reaper (`reap_actions`) self-heals a stranded
`ai-working` label on its next tick once the process is back up, instead of an
issue staying silently stuck until a human notices and relaunches by hand.

Logs go to the **journal**, not a file — tail with:
```bash
journalctl --user -u issue-agent-fix-watcher-<name> -f
systemctl --user status 'issue-agent-*-<name>'
```

Uninstall: `issue-agent uninstall-service` (equivalently
`bash bin/install-systemd.sh uninstall [<name>]` — omit `<name>` to remove
every repo's units at once).

Lower-level equivalent, without the CLI (per `config/repos/<name>.env`):
```bash
bash bin/compose-env.sh myapp                              # writes config/.active.env
AGENT_ENV=config/.active.env bash bin/install-systemd.sh    # Linux
# or: AGENT_ENV=config/.active.env bash bin/install-launchd.sh   # macOS
```

## Quickstart (make / lower level)

### 0. Prerequisites
`gh` (authenticated), `jq`, `git`, `uv` (for the governor), and `claude`. Check:
```bash
make doctor          # validates tooling, config, identities, repo access
```

### 1. Configure
```bash
cp config/agent.env.example config/agent.env
$EDITOR config/agent.env          # set REPO, identities, TEST_CMD, etc.
```
Key knobs (all in `config/agent.env`):
- `REPO` / `TARGET_DIR` — the GitHub repo (`owner/name`) and the local path to its
  working copy. **`TARGET_DIR` is required**: the watchers run git/worktree ops
  relative to cwd, so `lib.sh` `cd`s there (and the launchd/systemd installers set
  `WorkingDirectory` from it). Without it, watchers run in the harness dir and
  create worktrees in the wrong repo.
- `IDENTITY_MODE` — `three` (fix-bot ≠ review-bot ≠ steward-bot) or `solo` (one
  account; review is human, auto-merge stays human).
- `PERMISSION_MODE` — default `auto`: Claude self-approves routine tool calls and
  only escalates genuinely risky ones. In a headless daemon an escalation becomes
  a `status=blocked` → GitHub comment → human reply → resume cycle.
- `AUTO_MERGE` — `1` (default): on APPROVE + green CI, enable GitHub auto-merge
  into `dev`. Promotion `dev → prod` is always human.
- `TEST_CMD` / `LINT_CMD` / `TYPECHECK_CMD` — the project's commands; the gate
  and reviewer use them. **`TEST_CMD` must be non-watch** (e.g. `vitest run`, not
  `vitest`) — a watch-mode command never exits and hangs the PR/push gate hook.
- Difficulty router: `ROUTE_TRIVIAL_MODEL` / `ROUTE_STANDARD_MODEL` /
  `ROUTE_COMPLEX_MODEL` / `ROUTE_DEFAULT_MODEL` / `ROUTER_MODEL` /
  `FIX_SUBAGENT_MODEL` — a cheap Haiku classify picks the MAIN fix model per issue
  (blank = env `ANTHROPIC_DEFAULT_*` ids / bare aliases). `FIX_MODEL` set →
  router bypassed.

### 2. Install the plugin into your target repo
```bash
make bootstrap TARGET=/path/to/your/repo     # installs .claude/, labels, gitignore, priors
cd /path/to/your/repo && git add .claude INVARIANTS.md docs/adr && git commit -m "chore: install agent harness"
```

### 3. Run the watchers

**For real (documented default):** install as systemd (Linux) / launchd
(macOS) user services — supervised, auto-restarting, survives a suspend/
OOM-kill/reboot (see "Running for real" above; closes FAILURE-MODES #58–#62,
which foreground mode below is exposed to):
```bash
make install-systemd    # Linux — reads AGENT_ENV (default config/agent.env)
make install-launchd    # macOS
```
On the multi-repo layout, compose the per-repo config first (this just writes
the file — it does not launch anything), then point `AGENT_ENV` at it:
```bash
bash bin/compose-env.sh myapp                         # writes config/.active.env
AGENT_ENV=config/.active.env make install-systemd
```
Uninstall: `make uninstall-systemd` / `make uninstall-launchd`.

**Dev / first run (foreground, no supervisor):**
```bash
make run-fix            # fix watcher
make run-review         # review watcher (three-bot mode)
make run-steward        # steward (slow cadence)
```
Ctrl-C stops each. Use this to prove config (`TEST_CMD`, identities, GitHub
access) works before installing the service units.

Watchers must run with cwd = the target repo (git/worktree ops are cwd-relative).
Set `TARGET_DIR` in the config and `lib.sh` `cd`s there automatically; the
launchd/systemd installers read `TARGET_DIR` and set the unit's `WorkingDirectory`.
If you invoke `watcher.sh` by hand, `cd` into the target repo first.

### 4. File issues, label them `ai-ready`, walk away
The fixer picks the highest-priority `ai-ready` issue, opens a PR against `dev`,
the reviewer reviews, CI runs, and (with `AUTO_MERGE=1`) it merges itself. You
get a GitHub notification only when an agent is genuinely blocked.

### Watch the whole loop on a throwaway repo
```bash
DEMO_REPO=you/calc-demo bash examples/demo-target/init-demo.sh
```
Creates the repo, pushes a tiny calculator, installs the plugin, and seeds two
issues (a `fix/` and a `feat/`). Then run the watchers and watch them resolve.

---

## Constraint → enforcement traceability

The user's explicit requirement: *every* stated constraint should have a hook,
not just the obvious three. This is the full map. "Hook" = mechanically enforced
and non-circumventable; "Prompt" = honor-system instruction in `AGENT.md`;
"Ground truth" = the orchestrator verifies against GitHub, not model prose.

| # | Constraint | Enforcement | Where |
|---|-----------|-------------|-------|
| 1 | Branch is `feat\|fix\|chore\|refactor/<n>-…` | **Hook** | `checks/check-branch-name.sh` |
| 2 | Commit message uses an allowed prefix | **Hook** | `checks/check-commit.sh` |
| 3 | **`green:` only after a `red:` exists on the branch** | **Hook** (NEW) | `checks/check-commit-order.sh` (commit-time) + `check-pr.sh` §6 (log re-check) |
| 4 | No `--no-verify` / `-n` / `core.hooksPath` / `HUSKY=0` | **Hook** | `bash_governor.py` `bypass_reason()` — fail-closed, any nesting |
| 5 | Lint + typecheck pass before commit | **Hook** | `checks/check-commit.sh` (`LINT_CMD`/`TYPECHECK_CMD`) |
| 6 | Tests pass once locally before the PR | **Hook** | `checks/check-pr.sh` §1 (`TEST_CMD`) |
| 7 | PR body has `## Acceptance Criteria` | **Hook** | `check-pr.sh` §2 |
| 8 | PR links the issue (`Fixes #n`) | **Hook** (NEW) | `check-pr.sh` §3 |
| 9 | A `## Test Plan` was posted on the issue | **Hook** | `check-pr.sh` §4 (marker `.harness/test-plan-posted`) |
| 10 | `feat/`→e2e test, `fix/`→regression test in the diff (`chore/`/`refactor/` exempt) | **Hook** | `check-pr.sh` §5 (derived from git, not prefixes) |
| 11 | `evidence/` is never committed | **Hook** + gitignore | `check-pr.sh` §7 + bootstrap gitignore |
| 12 | Adversarial pre-PR verdict = PASS | **Hook (soft)** | `check-pr.sh` §8 reads `.harness/adversarial-verdict`, which the agent writes — see note below |
| 13 | ADR referenced when a governed path changes | **Hook** | `check-pr.sh` §9 (`Governs:` line in `docs/adr/*`, matched with `grep -F`) |
| 14 | AC generated/posted **before** code | **Hook (marker)** + reviewer | `check-pr.sh` §4 requires `.harness/ac-posted`; reviewer dim 3 checks evidence vs AC |
| 15 | Don't modify/delete existing tests | **Hook** + reviewer | `check-pr.sh` §7b blocks edits to test files present on base; reviewer dim 1/2 |
| 16 | Every commit subject uses an allowed prefix | **Hook** | `check-commit.sh` (commit-time) + `check-pr.sh` §6 re-checks the whole log (closes the `-F` bypass) |
| 17 | Don't violate `INVARIANTS.md` | **Prompt** + reviewer + steward | `AGENT.md`; reviewer dim 5; steward owns the file (no marker — accepted) |
| 18 | Don't refactor unrelated code | **Prompt** + reviewer | `AGENT.md` hard rule; reviewer flags scope creep — no structural diff-scope bound |
| 19 | Browser PR creation (`--web`) can't evade the gate | **Hook** | `check-pr.sh` §0 rejects `--web` |
| 20 | Tests run in CI, not the commit hook | **By construction** | commit hook runs lint/typecheck only; CI is authoritative |
| 21 | Never strand an issue/PR in `ai-working` | **Ground truth** | every fix script resets the label on the error path; `watcher-lib.sh` reaper handles issues AND PRs |
| 22 | Outcome from envelope + GitHub, never prose | **By construction** | `claude_outcome()` + `pr_for_issue()` (discovery by issue-number, any valid prefix) in `lib.sh` |
| 23 | Single fixer, no concurrent builds | **Lock** | `watcher.sh` run-lock (live PID, single-writer) |
| 24 | Reviewer/steward can't modify code | **Mechanical** | `--disallowed-tools Edit,Write` + restricted `--tools` |
| 25 | No self-approval (GitHub 422) | **By construction** | distinct `review-bot`; solo/unknown `IDENTITY_MODE` skips auto-merge |
| 26 | Auto-merge only on approve+green by another identity | **Ground truth** | `watcher-lib.sh` `maybe_automerge()` |
| 27 | Bounded retries (CI, review) → escalate, don't loop | **Counter** | `fix-ci.sh` (`MAX_CI_RETRIES`), `address-review.sh` (`MAX_REVIEW_RETRIES`), reviewer rejections (`REVIEW_MAX_REJECTIONS` — counts REQUEST_CHANGES, not pushes) |
| 28 | Promotion `dev → prod` stays human | **Out of scope** | nothing here merges past `dev` |
| 29 | Pushes to an open-PR branch re-verify state (tests, commit log, evidence, test-immutability) | **Hook** (NEW) | `checks/check-push.sh` (governor routes `git-push`); no-op until the branch has an open PR; shares `_pr_common.sh` with `check-pr.sh`. The `gh pr list` lookup that decides "does this branch have an open PR" fails **CLOSED**: one retry (`GATE_GH_RETRY_SLEEP`, default 2s) then DENY the push if `gh` still errors — a transient `gh`/network blip must never be silently read as "no PR" (that would skip the whole gate on a branch that *does* have one open) |
| 30 | **New tests must ACTUALLY fail without the implementation** (anti-tautology / anti-wrong-layer) | **Hook** (NEW) | `check-pr.sh` §7c → `prc_check_red_fails`: reverts the impl in the worktree, keeps the new tests, re-runs `TEST_CMD`, requires red. Create-only; `RED_MUST_FAIL=0` disables; fail-open on infra error |
| 31 | **PR must target `BASE_BRANCH`** (not the repo default branch) | **Hook** (NEW) + prompt | `check-pr.sh` §0b requires an explicit `--base`==`BASE_BRANCH`; `AGENT.md` step 9 tells the agent to pass it. Without `--base`, `gh pr create` defaults to the repo default (often `main`), routing the PR through the wrong branch's CI and breaking auto-merge |

Constraints 3, 8, 14, 15, 16, 19 had **no hook in the original spec** — they were
added here to honour the "every constraint gets a hook" requirement
(`check-commit-order.sh`; `check-pr.sh` §3 issue-link, §4 AC marker, §6 full-log
prefix re-check, §7b test-immutability, §0 `--web`). Constraint 29 (push gate) was
added after a real run showed address-review pushes added commits the create-time
gate never re-checked. Constraint 30 (red-must-fail) was added after issue #39
merged with a tautological test (`expect('My App').toBe('My App')`) that the
ordering rule (row 3, label-only) could not catch — the only durable defence is to
mechanically prove the test goes red without the change, independent of model tier.
Constraint 31 (PR base) was added after PR #49 (issue #48) opened against `main`
instead of `dev`: the agent ran `gh pr create` with no `--base`, so it inherited
the repo's default branch, whose CI still ran the (since-removed-on-`dev`) test job
on starved runners → permanent auto-merge deadlock. The base is now a hard gate.

> **Note on row 12 (adversarial verdict).** This is enforced as a **soft** hook:
> `check-pr.sh` confirms a `PASS` verdict file exists, but the agent writes that
> file itself, so a determined agent could fake it (the spec acknowledges this).
> It is a fast self-check; the **review agent is the real gate**. For a truly
> non-circumventable variant, move the adversarial `claude -p` call *into*
> `check-pr.sh` (slower, paid) — see `03-fix-agent-spec.md`. Rows 1–11, 13–16,
> 19 enforce against immutable git/file state and are not fakeable this way.

### Governor bypass coverage (the non-circumventable gate)

The tree-sitter governor (`bash_governor.py`) blocks hook-disabling no matter how
it is nested or disguised. An adversarial review hardened it against:
`--no-verify`; `-n` and clustered short flags (`-nm`, `-an`); ANSI-C quoting
(`$'--no-verify'`); backslash escapes (`--no\-verify`); `git -c core.hooksPath=…`;
**`git config core.hooksPath`** (two-step attack); path/variable-invoked git
(`/usr/bin/git`); **`env HUSKY=0 git …`** wrapping; `git push --no-verify`;
`GIT_CONFIG*`/`HOME` env redirection; `HUSKY=false`/`OVERCOMMIT_DISABLE` and other
hook-manager kill-switches; `git switch -C`/`checkout -B` force-create; **nested
and passthrough wrappers** (`env env HUSKY=0 git …`, `exec`/`command`/`builtin`/
`nohup`/`nice`/`time git …`, unwrapped to a fixed point). A **missing checker now
fails closed** (a deleted gate blocks rather than silently allows). `git push -n`
(`--dry-run`) is correctly *not* treated as a bypass — only `git commit -n` is.
Each has a regression test in `tests/test_governor.py` (42 cases).

Beyond bypass detection, the governor now also **routes `git push` to a checker**
(`check-push.sh`, constraint #29) — previously push was bypass-guard-only. The
push checker is a no-op unless the branch has an open PR, then re-runs the
state-derived gate (tests, commit log, evidence, test-immutability).

> **Accepted residual.** If the OS denies `fork()` (resource exhaustion) in the
> exact window between the watcher writing an issue's `resumed-at` marker and the
> resume actually starting, that one reply won't re-trigger until the next
> distinct reply. This needs OS-level process exhaustion and the launchd/systemd
> supervisor restarts the watcher on any crash, so it's left as-is.

---

## Decisions made while building (deltas from the spec)

These are deliberate, tested choices — see commit messages and code comments.

1. **`plugin/` staging dir, not the harness's own `.claude/`.** The installable
   payload lives in `plugin/`; `bootstrap.sh` copies it into a target repo's
   `.claude/`. This keeps the harness's own session config separate from the
   artifact it ships, and matches how Claude Code plugins are structured.
2. **`PERMISSION_MODE=auto`, not blanket `bypassPermissions`.** Per the operator's
   choice. `plugin/settings.json` ships a broad `allow` list and a `deny` list
   (secrets, auth, network) so only genuinely unanticipated actions surface; in a
   headless daemon those map onto the existing blocked→resume path.
3. **Auto-merge implemented** (`maybe_automerge`), flag default ON for `dev`,
   idempotent via a per-PR marker, and skipped in solo mode (no second approver).
4. **`check-commit-order.sh` is a new hook** enforcing red-before-green at commit
   time; `check-pr.sh` re-checks from the git log as a backstop.
5. **`watcher-lib.sh`** factors the watcher's helpers into a sourceable file —
   bash 3.2 (macOS default) cannot `source <(…)`, and this makes the loop logic
   unit-testable against a real file.
6. **jq boolean trap fixed.** `.is_error // true` / `// false` are both WRONG in
   jq (it treats `false` as empty). The spec's review scripts would have *never
   submitted a review*. Fixed to `(has("is_error") and .is_error == false)`.
7. **bash 3.2 `local` split.** `local a=$1 b=x-$a` is unbound under `set -u` on
   macOS bash; declarations that self-reference are split.
8. **Identity wrappers** (`gh_fix`/`gh_review`/`gh_steward`) switch tokens per
   identity via `*_TOKEN` env vars, falling back to ambient `gh auth`. A GitHub
   **App** can serve as the review+steward identity (its `[bot]` actor is distinct
   from the fixer, so it satisfies the no-self-approval rule with no extra user
   account); the wrappers mint short-lived App installation tokens via
   `bin/gh-app-token.sh` when `GH_APP_ID` is set.
9. **Gate armed via `--settings` (absolute path), not the worktree's `.claude/`.**
   `common_claude_args()` builds a merged `--settings` JSON at runtime — the full
   `plugin/settings.json` permission allow/deny lists **plus** a PreToolUse hook
   pointing at the harness's *absolute* `dispatch.sh` — and passes it on every
   `claude` call. Why: the agent runs in a `git worktree`, and `git worktree add`
   does **not** copy untracked files, so a target's untracked `.claude/` is absent
   in the worktree and a `$CLAUDE_PROJECT_DIR`-relative hook silently never fires
   (a real run shipped a PR with the gate completely unarmed this way). `--settings`
   *replaces* project settings, so the permissions MUST travel in the same blob —
   omitting them left the agent with no allow-list and it stalled asking for
   permission on every Bash call. The merged blob fixes both.
10. **Gate markers live in `.harness/`, not `.claude/`.** The AC / test-plan /
    adversarial markers moved from `.claude/.*` to `.harness/*`: Claude Code treats
    `.claude/` paths as sensitive and refuses Edit/Write there, so the agent could
    never create them. The agent now writes them via Bash.
11. **Difficulty router.** `resolve-issue.sh` runs a cheap Haiku classify before the
    main session and selects the MAIN model by tier (trivial→haiku, standard→sonnet,
    complex→opus) via `ROUTE_*_MODEL`. `FIX_MODEL` bypasses it. Use **bare aliases**
    (`haiku`/`sonnet`/`opus`) for `--model`, not the full `ANTHROPIC_DEFAULT_*`
    ids — some proxies reject the full ids as a `--model` value while the aliases
    resolve reliably.
12. **explore-local subagent.** The fixer is given `agent/agents/explore-local.json`
    via `--agents`, a read-only Haiku codebase-search subagent; `FIX_SUBAGENT_MODEL`
    is exported as `CLAUDE_CODE_SUBAGENT_MODEL` (which outranks the subagent's own
    `model` field) to keep exploration on Haiku and the main context cheap.
13. **Push gate (`check-push.sh`).** The governor routes `git push` to a checker
    that re-runs the state-derived gate when the branch has an open PR (closes the
    "address-review pushes were never re-checked" hole). Shares `_pr_common.sh` with
    `check-pr.sh` so the two can't drift. Its own precondition check — "does this
    branch have an open PR" via `gh pr list` — fails **CLOSED**, not open: a `gh`
    error is retried once (`GATE_GH_RETRY_SLEEP`, default 2s) and then DENIES the
    push. Earlier this treated a `gh` error the same as "no open PR" (allow) —
    one flaky `gh` call (proxy hiccup, expired token, rate limit) silently
    bypassed the entire push gate on a branch that *did* have an open PR.
14. **`TARGET_DIR` cwd.** Watchers do cwd-relative git/worktree ops, so they must run
    *inside* the target repo. `lib.sh` `cd`s to `TARGET_DIR`; the launchd/systemd
    installers also set the unit `WorkingDirectory` from it. Without it, worktrees
    land in the harness repo and the agent can't find the target's code.
15. **Red-must-fail gate.** `check-pr.sh` §7c reverts the implementation in the
    worktree, keeps the new test files, and re-runs `TEST_CMD`, requiring it to go
    RED. Proves the tests exercise the change — catches tautological/wrong-layer
    tests (the #39 class) that the label-only red-before-green rule cannot. The
    reviewer (now defaulting to **opus** — strong + decorrelated) re-checks the same
    property by reading the test on disk.
16. **PR base gate.** `check-pr.sh` §0b requires an explicit `--base`==`BASE_BRANCH`.
    Without `--base`, `gh pr create` targets the repo's *default* branch (main),
    routing the PR through the wrong branch's CI and deadlocking auto-merge (live
    on PR #49). `AGENT.md` tells the agent to pass it.
17. **`ci_state` waits for CI to settle.** `pending` takes precedence over
    `failure`: a failed check while others are still queued/running is NOT a final
    verdict. Acting on it makes the fixer push a commit that re-triggers the
    in-flight jobs → loop + budget burn (observed on PR #49). The fixer dispatches
    `fix-ci` only once every check has completed and at least one genuinely failed.
18. **`CI_IGNORE_CHECKS` for starved runners.** An ext-regex of check names to
    drop from `ci_state` decisions. When a legitimate job (e.g. `Format Backend`)
    queues forever on a starved Actions fleet, it pins `ci_state` at `pending` and
    deadlocks BOTH fix-CI dispatch and auto-merge — even though it's not redundant
    and shouldn't be deleted. Excluded checks are ignored; every other check
    (Sonar/Wiz/Snyk/build) still counts, so a real failure is never masked.
    (Subtle wiring bug found in test: `gh --jq` must be *immediately* followed by
    its program; `--arg name value` goes after — else `--jq` swallows `--arg`.)
19. **`FIX_MCP_CONFIG` — conditional CI-fix MCP servers.** The CI-fixer diagnoses
    from `gh pr checks` names + `gh run view --log-failed` + tools it runs in the
    worktree (black/npm). That covers format/build/IaC failures, but a **SonarQube
    quality-gate** failure's exact conditions live on the Sonar server, not always
    the Actions log. `FIX_MCP_CONFIG` (JSON map keyed by a check-name regex)
    attaches an MCP server — and allow-lists its tools in the `--settings` blob —
    **only when the PR's CI actually contains that check** (`pr_ci_has` /
    `ci_mcp_for_pr` in `lib.sh`), so a repo without Sonar never loads it. The gate
    hook is Bash-only, so `mcp__*` calls are never gated; they just need allow-
    listing under `PERMISSION_MODE=auto`. Config files (with secrets) live in the
    gitignored `config/mcp/`. `fix-ci.sh`'s prompt maps each failure class to its
    fix (format→run formatter; sonar→query the gate via the MCP; build→reproduce).

---

## Testing

```bash
make test            # all three suites (134 total)
make test-governor   # 42 pytest cases: parsing, classification, bypass detection, push routing
make test-hooks      # 39 cases: check-*.sh (incl. check-push.sh) against real temporary git repos
make test-orchestration  # 29 cases: full loop with FAKE gh + claude (no network/spend)
```

The orchestration suite uses `tests/fakes/gh` (a stateful JSON-backed `gh`
simulator that pipes through real `jq`) and `tests/fakes/claude` (a scriptable
envelope queue that can mutate the fake GitHub state to simulate "the agent
opened a PR / pushed"). This proves the scripts read **ground truth**, not prose,
without touching GitHub or spending a cent.

---

## Layout

```
plugin/                 # the installable enforcement payload (→ target .claude/)
  settings.json         #   verified PreToolUse hook schema + permission allow/deny
  AGENT.md              #   appended to the fix agent's system prompt
  hooks/
    dispatch.sh         #   shim: stdin JSON → governor
    bash_governor.py    #   tree-sitter parser + router + bypass detection
    checks/
      check-branch-name.sh check-commit.sh check-commit-order.sh
      check-pr.sh check-push.sh _pr_common.sh   # _pr_common: shared create/push checks
agent/                  # fix agent orchestration (operator machine)
  lib.sh watcher.sh watcher-lib.sh resolve-issue.sh resume-issue.sh
  fix-ci.sh address-review.sh cleanup.sh
  agents/explore-local.json   # Haiku read-only search subagent (--agents)
review/                 # review agent (separate identity)
  lib.sh review-pr.sh re-review-pr.sh review-watcher.sh
steward/steward.sh      # persistent architecture steward
bin/                    # bootstrap.sh doctor.sh master.sh install-launchd.sh install-systemd.sh
ops/                    # launchd plists + systemd units
config/agent.env.example
examples/demo-target/   # tiny calculator + seed issues to watch the loop
tests/                  # test_governor.py test_hooks.sh test_orchestration.sh + fakes/
```

---

## Operating notes

- **Blocked work** surfaces as a GitHub comment + an `ai-blocked` label (issues)
  or `needs-human-review` (PRs escalated at the review round cap). That is your
  only inbox.
- **`make doctor`** before first run, and any time you change identities or
  tokens — it catches the misconfigurations that would otherwise fail silently.
- **State** lives in `~/.local/state/{issue,review,steward}-agent/` and in
  GitHub. Killing and restarting any watcher is safe; the steward re-reads its
  journal from disk.
- **Cost** is bounded by `--max-budget-usd` on every `claude` call and by the
  single-fixer model (one fix + one review in flight at a time). The difficulty
  router keeps trivial issues on Haiku and exploration delegated to a Haiku
  subagent, so routine fixes cost a fraction of an all-Opus run.
- **Always start watchers through the harness** (`make run-*` / the launcher /
  service units), and ensure `TARGET_DIR` is set: the enforcement gate is armed via
  `--settings` (an absolute path), not the worktree's `.claude/` (git worktrees
  don't copy untracked files), and the watchers must run with cwd = target repo.
  A hand-run `claude -p` in a worktree without the harness args has **no gate**.
- **CI vs. the local gate.** Tests are enforced by the harness (the PR-creation
  gate and the push gate run `TEST_CMD`), so duplicating them as a CI job is
  redundant and a stuck CI runner can deadlock auto-merge. Keep security/build
  checks in CI; let the harness own the test gate. `TEST_CMD` must be non-watch.
