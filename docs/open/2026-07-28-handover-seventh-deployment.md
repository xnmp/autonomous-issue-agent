# Handover — seventh deployment (and rules for two concurrent babysitters)

Written 2026-07-28 after the seventh tauri-explorer batch. Deliberately thin:
the watch-list lives in [docs/field-verification.md](../field-verification.md)
(pending rows = the eighth deployment's checklist), loose ends live under the
`operator-action` label, metrics come from `issue-agent dashboard`, and stable
gotchas moved into CLAUDE.md. This file carries only what has no better home.

## State of the world

- **Batch: 5/5 merged in ~1h50m** (#400 #480 #510 #518 #557 → PRs 565–569),
  scoreboard 28, $125.96 total, repair share 18%, first-pass 3/5, zero
  FIX_CI/REBASE/spend-blocks. CI wall clock (~15m webkit) was the critical
  path → tauri#570 + harness #51.
- **Daemons RUNNING for both deployments** (tauri + keifu, 3 python procs
  each), all on per-name `.active.<name>.env`; legacy `.active.env` deleted.
- **Harness main = 368080f, pushed.** Both this session and the keifu-side
  agent pushed to main tonight; histories stayed linear but see the rules
  below.
- **Caps**: tauri DAILY=700; `GLOBAL_DAILY_SPEND_CAP_USD=1500` armed in
  shared.env. **keifu overlay still has DAILY_SPEND_CAP_USD=0** — its
  babysitter should set a real number (the global cap is its only bound).
- **Rates**: `CODEX_COST_PER_MTOK` set (luna .25/1.5, terra 2/12, sol 5/30 —
  sol pinned at the historic scalar so old ledger data keeps its unit).
- tauri launch currently needs `GATE_PROBE=0` (false red, #49).

## Rules for two concurrent babysitting sessions

The deployments are isolated (per-name env, per-repo state dirs, per-repo
daily caps) but FOUR seams are shared. Tonight's interleaves were benign;
these keep them that way:

1. **The harness main checkout is shared.** A commit to main by either
   session hot-restarts BOTH deployments' daemons between ticks (reload
   watches one HEAD). Safe by design — but pull before every commit, merge
   from the main checkout (never a worktree cwd), and expect the other
   session's unpushed commits on local main (one accidental-but-harmless
   publish already happened on the sixth night).
2. **`config/shared.env` edits stale EVERY deployment's composed snapshot.**
   After editing it, regenerate all of them (`bash bin/compose-env.sh <name>`
   per repo) or the other session's next launch fails doctor. Tell the other
   session you did it — their daemons will hot-reload the new values.
3. **The global daily cap is one pool.** A heavy day on one repo can idle the
   other's daemons (silent tick gate). If a queue mysteriously stalls, check
   `spend_block` events for scope `global:` before debugging anything else.
4. **Harness-code changes mid-batch: one owner.** Whoever files the issue
   fixes it; the other session pulls. Two sessions editing harness code
   concurrently re-creates the consistency debt CLAUDE.md's subagent brief
   warns about. Coordinate via issue assignment, not assumption.

## Only-here notes for the eighth batch

- tauri candidates left with seams in TEST_CMD coverage: #520, #521, #512,
  #513, #511 (one git-graph-core at a time), #497, #505/#503 (perf — hard to
  verify), #429 (gitattributes — NO test seam, red-fail deadlock risk, skip
  or pair). #546/#498/#457/#493 remain hardware/platform-dependent.
- #50 (PR-side ai-blocked unresumable) is the sharpest known edge: a
  PR-stage block before approval strands the PR until a human relabels.
  If a leg blocks on a PR, reply AND watch — the reply alone does nothing
  until #50 lands.
- The dashboard reads healthy-idle daemons as STALE (journal-only view);
  don't restart on that signal alone — check `pgrep` first.
