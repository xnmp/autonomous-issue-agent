# CLAUDE.md — autonomous-issue-agent

Headless harness that resolves GitHub issues end-to-end (issue → branch → TDD →
PR → CI → review → merge to `dev`). Three role daemons — **fix**, **review**,
**steward** — poll GitHub and drive Claude Agent SDK sessions.

**Start here:** [docs/reference/architecture.md](docs/reference/architecture.md)
(how it works now) · [HARNESS.md](HARNESS.md) (operator manual) ·
[docs/open/residuals.md](docs/open/residuals.md) (what's still open — read §1
before pointing this at a repo you care about) · [docs/README.md](docs/README.md)
(what every other document is for, and whether to trust it).

The nine `0N-*.md` specs now live in `docs/archive/`: they describe the ORIGINAL
BASH DESIGN and are accurate only about the legacy watchers under `agent/`,
`review/`, `steward/` — not about the default SDK path. Do not implement from
them.

## Direction: token efficiency without losing power

The harness's north star is **driving cost per merged PR down while holding the
quality guardrails flat** — streamline aggressively, but every gap gets plugged,
never papered over. Optimizations are **evidence-triggered, not speculative**
(the P4.1 pattern: build the lever dormant, activate it when journal data shows
the trigger — difficulty routing sat unwired until resolves measured $13–20
each). An efficiency change that cannot say which metric it moves is not ready
to land.

**Efficiency metrics** — all derivable with one `jq` from the global ledger
(`~/.local/state/issue-agent/spend.jsonl`) and the per-role
`<STATE_DIR>/journal.jsonl` (`agent_run` events carry cost, status, exit_reason,
and token usage):

- **$ per merged PR** — the primary number. Baseline 2026-07-27: ≈$17/merge
  claude-heavy (resolves $13–20 each, cache reads dominating); routing targets
  $5–8. On subscription runtimes (codex/glm) the ledgered dollars are synthetic
  — read them as a token meter, same math.
- **Cost mix by leg** — resolve vs review vs repair (fix-ci/rebase/address-
  review). Repair legs are pure overhead; their share should shrink.
- **Overhead ratios** — probe hit rate (`WorldUnchanged` ticks vs full world
  builds), patch-id re-review skips vs paid re-reviews, router runs staying
  ≤$0.10.
- **Issue→merge wall clock** — the critical path (post-approval rebases that
  re-trigger CI have dominated it; see #2/#544).

**Power guardrails** — an efficiency change must leave these flat, measured on
the next deployment day before stacking further changes:

- **First-pass approval rate** (review `agent_run.status` APPROVE vs
  REQUEST_CHANGES since #7) and rounds-per-PR.
- **Escalation rate** — `needs-human-review` relabels, resolve/CI/rebase retry
  counters, infra deaths. A cheaper model that escalates more is a net loss.
- **Merges per deployment day** at a given issue difficulty mix.
- **Recurrence rate** (#10) — harness-resolved issues that come back: a
  literal reopen (steward journals `recurrence` kind=reopen on an open
  `ai-done` issue whose stateReason is "reopened"), or a new issue
  re-reporting a fixed bug / a shipped feature not working (triage classifies
  against the recent-merges list → `recurrence` label + comment naming the
  original issue/PR + journal kind=refiled). Review cannot see these — they
  surface later, from reality — which makes them the ground-truth guardrail.
  Advisory only: nothing is auto-reopened or auto-dispatched. Split by routed
  model tier before trusting a cheaper tier with more issues.

State the pair explicitly in any efficiency PR: the metric it moves, the
guardrail it must not move, and the measurement that will decide it.

## The manager role (babysitting a deployment)

Whoever babysits a deployment — today an interactive session, eventually the
#11 daemon — owns this lifecycle. Scope boundary: everything here is
**reversible or additive** (comments, labels, restarts, filed issues, reports);
the moment a fix needs harness code, file an issue with journal evidence and
stop. Never self-modify mid-batch.

- **Before**: read the pending rows of
  [docs/field-verification.md](docs/field-verification.md) (the append-only
  ledger of what has shipped vs what a live deployment has proven) and add
  rows for anything `git log` shows landed without one — each row names the
  journal evidence that will prove the change works; triage doctor output
  with judgment (a red preflight can be a false positive — verify
  independently before overriding, and file the gap); restore caps and
  regenerate stale env snapshots; populate newly-landed knobs; pick the batch
  (seam inside TEST_CMD coverage, no existing closing PRs, overlap-disjoint —
  the steward proposes under #15, the manager approves).
- **During**: detect and restart dead daemons; investigate wedged legs before
  killing; answer `status=blocked` clarification questions (evidence
  unproducible from a worktree/CI → scope it to operator post-verification;
  genuine product ambiguity → escalate to the human, don't guess); tick off
  the watch-list as its evidence arrives in the journals; flag anomalies with
  restraint — an expensive-but-in-cap leg is data, not an incident.
- **After**: file surfaced issues with evidence; produce the batch readout
  ($/merge, first-pass rate, cost mix by leg, guardrail deltas — the numbers
  the Direction section requires before stacking changes); give every
  watch-list entry a verdict in the ledger — **VERIFIED** (cite the journal
  event), **MISBEHAVED** (file it), or leave it **pending** (a change nothing
  fired is untested, not fine); hand the operator a
  single loose-ends list (post-deploy verifications, label surgery the
  classifier reserves, superseded PRs).

### Operational gotchas (stable across 3+ deployments — no longer handover news)

- **Hot reload fires only BETWEEN ticks** (`adapters/reload.py`, watching the
  main checkout's HEAD + the AGENT_ENV file's mtime). Merging to main or
  editing the composed env mid-leg is safe; the in-flight leg finishes on old
  code. Corollary: editing `shared.env` makes every deployment's composed
  snapshot stale — regenerate each with `bin/compose-env.sh <name>` or doctor
  blocks the next launch.
- **Run git merges from the MAIN checkout, never with a worktree as cwd** — a
  `cd <worktree> && … && git merge && git worktree remove` chain merges
  nothing and then deletes your own cwd (bitten twice on 2026-07-28).
- **The daily spend window is per UTC day**; ledger sums and "why did the cap
  reset" confusion follow UTC, not local midnight.
- **Codex OAuth binds whatever ChatGPT session the browser has live** — after
  any login, verify the id_token email claim before trusting the account.
- **Evidence e2e legs open real app windows on the operator's desktop** —
  cosmetic, but warn whoever is at the keyboard.
- **Assistant sessions cannot `gh issue edit --add/remove-label` or
  `gh issue close`** (permission classifier). Comments and issue/label
  *creation* work; label surgery belongs to the daemons or the human — file
  it under the `operator-action` label (exists on the harness and target
  repos) rather than retrying.
- **`gh` label/state searches are stale for recently closed objects** —
  verify with a direct `gh issue view` before acting on a search result.

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
`doctor`, `ready`, `triage`, `dashboard` (all repos, journal-only; `--html`/
`--serve` for the drill-down snapshot), `install-service`.

**make (lower level)** — one command aims all three watchers at a target repo:

```bash
make point-at REPO=<name>        # reads config/repos/<name>.env over config/shared.env
```

e.g. `make point-at REPO=myapp`. `bin/point-at.sh` does, idempotently:

1. **Compose env** — sources `config/shared.env` (repo-independent) then
   `config/repos/<name>.env` (per-repo, wins), writes the resolved values to a
   gitignored `config/.active.<name>.env`, and exports `AGENT_ENV` to it. Both the bash
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
   all). Logs → `${XDG_STATE_HOME:-~/.local/state}/issue-agent/logs/<role>-<name>.log`
   (override with `LOG_DIR`; systemd units log to journald). `uv` runs from the harness
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
- `config/.active.<name>.env` — the composed run file `point-at` regenerates
  each run (per-name, so concurrent deployments don't clobber each other or
  trip each other's hot-reload watch).
  *gitignored.*
- `config/agent.env.example` — full knob reference (legacy single-repo layout).

`TEST_CMD` must be **non-watch** (each sub-command must exit) — it arms the PR
gate, the red-fail (anti-tautological-test) gate, and the reviewer's test run. For
a monorepo, compose the tiers with `set -e` (e.g.
`set -e; (cd backend && uv run pytest) && (cd frontend && pnpm exec tsc -b)`).

On codex, `MAX_BUDGET_USD` is enforced in the harness rather than by a CLI
flag: the runner samples the rollout's cumulative token counters, subtracts the
resumed session baseline, and kills the process group when the current leg
crosses the threshold. This requires `CODEX_ENFORCE_BUDGET=1` and positive input
and output prices for every fallback/per-model rate; startup logs explicitly
report any configuration where the dollar bound cannot be enforced.

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
