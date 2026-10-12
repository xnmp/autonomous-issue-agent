# Refactor conventions (included in every slice brief)

- **The yardstick** is [the design](2026-10-12-minimal-harness-design.md), read with [the decisions](2026-10-12-refactor-decisions.md). Scope and file:line maps come from [the audit](2026-10-12-refactor-audit.md).
- **DELETE, don't migrate.** No compatibility shims, legacy parsers or "kept for old rows" branches. If old persisted data blocks a deletion, the cutover rule applies: drop or retire those rows in a migration, and the item re-derives from GitHub next tick.
- **No new state.** No new tables, counters, holds, labels, knobs, ledgers or retry loops. Derive instead: attempts and backoff come from the legs/runs table; spend stays in the existing ledger. Constants, not config.
- **One slice per branch,** worktree off `origin/dev`. Verify the merge base first. Commit in green logical steps.
- **Tests die with their code.** Delete tests that only pin deleted behaviour; never port them. Add few behavioural tests, and only for the replacement behaviour the slice brief names.
- **Shared seams are the manager's:**
  - `domain/actions.py` (Action types)
  - `domain/world.py` (WorldState / PRView)
  - `domain/outcome.py` (outcome classification)
  - `app/loop.py` (the daemon loop)
  - `app/config.py`
  - the store API in `control_store.py`

  A slice may *delete* members of these. It may not add or reshape a member without stating so in its report; the manager reviews that line by line.
- **Verification inside a slice:**
  - focused pytest only: `uv run --no-sync pytest -o addopts='' -n 4 …` (at most 4 workers);
  - `make typecheck`;
  - the import-linter and architecture contract tests, run directly.
  - Never run `make test`; the manager runs it once per slice on a quiet host.
- **Process hygiene:**
  - keep child agents blocking;
  - no `pkill -f`/`pgrep -f` self-matching patterns;
  - never touch GitHub, systemd or `~/.local/state`, except read-only DB measurement with `mode=ro`.
- **Report:** what was deleted (by file), what was kept and why, any shared-seam edits, tests deleted and added, net `src/` and `tests/` lines (`git diff --shortstat origin/dev..HEAD -- src` and the same for tests), and anything stopped on.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
