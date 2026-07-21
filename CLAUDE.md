# CLAUDE.md — autonomous-issue-agent

Headless harness that resolves GitHub issues end-to-end (issue → branch → TDD →
PR → CI → review → merge to `dev`). Three role daemons — **fix**, **review**,
**steward** — poll GitHub and drive Claude Agent SDK sessions. Design spec is the
nine `0N-*.md` docs; operator manual is [HARNESS.md](HARNESS.md).

## Pointing the harness at a repo

Two equivalent entry points:

**CLI (operator front door)** — install once, run from inside any target repo:

```bash
make install                     # uv sync (editable) + ~/.local/bin/issue-agent shim
cd /path/to/target-repo && issue-agent start   # does everything (see below)
issue-agent ready <n>            # label issue ai-ready (triggers the fixer)
```

`issue-agent start` (foreground, Ctrl-C stops all) is the whole first-run flow:
**auto-`init`** (creates `config/shared.env` interactively if absent) →
**auto-detect** `REPO`/`TARGET_DIR` from cwd (reuse a matching overlay) →
**auto-discover `TEST_CMD`** via a cheap read-only Haiku probe, proceeding behind
a loud `THIS REPO HAS NO TESTS` banner if none exists → synthesize/reuse
`config/repos/<name>.env` → **delegate to `bin/point-at.sh`** (no launch-logic
reimplementation). `src/issue_agent/app/cli.py` installs editable (`bin/install.sh`)
because the daemon walks `__file__` to this checkout for `plugin/` + `config/mcp/`
— a plain wheel omits those. Subcommands: `init`, `start`, `stop`, `status`,
`doctor`, `ready`, `triage`, `install-service`.

**make (lower level)** — one command aims all three watchers at a target repo:

```bash
make point-at REPO=<name>        # reads config/repos/<name>.env over config/shared.env
```

e.g. `make point-at REPO=myapp`. `bin/point-at.sh` does, idempotently:

1. **Compose env** — sources `config/shared.env` (repo-independent) then
   `config/repos/<name>.env` (per-repo, wins), writes the resolved values to a
   gitignored `config/.active.env`, and exports `AGENT_ENV` to it. Both the bash
   tools (via `AGENT_ENV`) and the Python daemons (via `os.environ`) read one
   source of truth.
2. **Preflight** — `bin/doctor.sh` (tooling, identities, repo access).
3. **Ensure base branch** — creates `BASE_BRANCH` (default `dev`) off the target's
   default branch and pushes it, if absent.
4. **Point-and-fire prep (NO tracked-tree mutation)** — ensures the GitHub
   lifecycle **labels**, and appends worktree-scratch ignores to the target's
   local **`.git/info/exclude`** (`.claude/worktrees/`, `.harness/`, `.codex/`,
   `AGENTS.md`, non-image `evidence/`). Nothing is committed into the target.
5. **Run all three watchers** in the foreground under one `trap` (Ctrl-C stops
   all). Logs → `/tmp/issue-agent-<role>-<name>.log`. `uv` runs from the harness
   root (that's where the `issue_agent` package lives); each daemon self-chdirs
   into `TARGET_DIR` for its git/worktree ops.

Then label an issue `ai-ready` to trigger the fixer: `issue-agent ready <n>` (from
inside the repo) or `gh issue edit <n> -R <owner/repo> --add-label ai-ready`.

Knobs: `ROLES="fix review steward"` (subset to start), `ISSUE_AGENT_MAX_TICKS=N`
(bound each daemon's loop — dry run / CI; unset = forever).

**Network / proxy.** The daemons shell out to `gh` and call the LLM gateway; they
inherit the environment of the shell that launched `point-at` (nothing proxy-related
is hardcoded — the harness stays general). On a network that requires an outbound
proxy (e.g. a corporate one), export the proxy vars in that shell **before**
launching, and include the git host + LLM gateway in `NO_PROXY` as needed, e.g.:

```bash
export HTTPS_PROXY=… HTTP_PROXY=… NO_PROXY=localhost,127.0.0.1,.github.com,…
make point-at REPO=myapp
```

If the daemons log `error connecting to api.github.com` while your interactive
`gh` works, the launching shell was missing the proxy env — relaunch with it set.
(To confirm a running daemon's env: `ps eww <pid> | tr ' ' '\n' | grep -i proxy`.)

### Why nothing is installed into the target

The SDK daemons inject everything **in-process** — the target's tracked tree is
never touched:

- **Governor hook** (`src/issue_agent/adapters/governor.py`) — built as a
  `HookMatcher` and passed to the SDK as `extra_hooks`; its bypass core is loaded
  by path from the harness's `plugin/hooks/bash_governor.py`, not the target's.
- **AGENT.md** — `app/main.py:_agent_md` reads the harness's `plugin/AGENT.md`,
  renders it, and injects it as `append_system_prompt`.
- The fixer runs `permission_mode=bypassPermissions`, so a target `settings.json`
  allow-list is irrelevant.

So the old `make bootstrap` (which copies `.claude/` plugin + `settings.json` +
`AGENT.md` into the target) is **redundant for the SDK path** and `point-at` does
not call it. `bin/bootstrap.sh` remains only for the legacy bash-watcher mode.

### The target's CLAUDE.md IS used

Each agent runs with `cwd = the per-issue worktree` (inside the target repo) under
the `claude_code` system-prompt preset with `setting_sources=["project"]`. The SDK
therefore auto-loads the target's `CLAUDE.md` (and nested ones, e.g. `backend/`,
`agents/…/CLAUDE.md`) plus its `.claude/settings.json`. `INVARIANTS.md` / ADRs are
read **if present** (`domain/tasks.py`) — absent ones are simply skipped; the
harness never creates them.

## Config layout

- `config/shared.env` — repo-independent: GitHub App identity (`GH_APP_*`),
  `FIX_BOT`/`REVIEW_BOT`/`STEWARD_BOT`, Jira (`JIRA_*`), models, budgets.
  *gitignored.*
- `config/repos/<name>.env` — per-repo: `REPO`, `TARGET_DIR`, `BASE_BRANCH`,
  isolated `*_STATE_DIR`s, `TEST_CMD`/`EDIT_CHECK_CMD`, and any per-repo overrides
  (e.g. reviewer prompt/label). *gitignored.*
- `config/.active.env` — the composed run file `point-at` regenerates each run.
  *gitignored.*
- `config/agent.env.example` — full knob reference (legacy single-repo layout).

`TEST_CMD` must be **non-watch** (each sub-command must exit) — it arms the PR
gate, the red-fail (anti-tautological-test) gate, and the reviewer's test run. For
a monorepo, compose the tiers with `set -e` (e.g.
`set -e; (cd backend && uv run pytest) && (cd frontend && pnpm exec tsc -b)`).

## Identities

- **fix** = ambient `gh auth` login. **review + steward** = the GitHub App
  (`my-reviewer-bot[bot]`). `IDENTITY_MODE=three` requires `FIX_BOT` distinct
  from review/steward (GitHub 422 self-approval); review and steward may coincide
  (the steward never approves PRs).

## Package management

`uv` for Python (`pyproject.toml`; the daemons need the `sdk` extra +
`tree-sitter`/`tree-sitter-bash`). `make test` runs the full self-test suite
(governor + hooks + orchestration + domain + adapters + arch import-linter).

## Layout

- `src/issue_agent/` — the SDK port (hexagonal: `domain/` pure, `adapters/` I/O,
  `app/` wiring). `app/main.py` is the daemon entrypoint (`fix|review|steward`).
- `plugin/` — enforcement payload: tree-sitter governor, gate hooks, `AGENT.md`.
  The SDK reads `plugin/` directly; only the legacy bash mode copies it into a
  target.
- `agent/` `review/` `steward/` — legacy bash watchers (`make run-fix` etc.).
- `bin/` — `point-at.sh`, `bootstrap.sh` (legacy), `doctor.sh`, service installers.
- `config/` — see above.
