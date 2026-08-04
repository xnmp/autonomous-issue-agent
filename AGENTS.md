# AGENTS.md — autonomous-issue-agent

**Read `CLAUDE.md` in this directory first — it is the authoritative project
document** (architecture pointers, direction/metrics, config layout, which
docs to trust). This file exists because some agent runtimes (codex) read
`AGENTS.md` and not `CLAUDE.md`; it deliberately duplicates only the hard
operational rules, so the two cannot drift far.

Non-negotiables when working on this repo:

- Python via **uv** only (no pip/poetry). Type-check with **pyrefly** (not
  mypy/pyright). Run the full self-test suite with `make test` — it must be
  green before any commit.
- Hexagonal boundaries are enforced by import-linter: `domain/` stays pure
  (no I/O, no adapters imports), `adapters/` does I/O, `app/` wires. Put
  classification/decision logic in domain, side effects in adapters.
- Make broken lifecycle states unrepresentable where practical: prefer
  explicit typed variants/discriminated unions over independent booleans,
  status strings, and contradictory optional fields. Validate external input
  before constructing a trusted variant, with runtime checks at the boundary.
- Live daemons hot-reload from this checkout's HEAD **between ticks**: a
  commit to the checked-out branch restarts them. Commit only coherent,
  tested states; never commit a half-edit "to save it".
- Tests assert behavior and contracts, not implementation details. New
  business logic gets unit tests, including malformed/absent input edges.
- Comments state constraints and reasons the code can't show — never
  narration of what the next line does.

NOTE: inside harness-managed per-issue worktrees (`.claude/worktrees/`),
this file may be OVERWRITTEN by the generated agent contract
(`adapters/codex_hook.py:write_hook_config`) — do not commit that generated
content back to the repo.
