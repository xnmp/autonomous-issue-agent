# Docs

Four directories, one rule each. The rule is the point: before this structure
existed, a new session could not tell which documents described the system as it
*is* from ones describing a design it had already replaced — and
[CLAUDE.md](../CLAUDE.md) pointed at the latter as "the design spec".

| Directory | Rule | Trust it? |
|---|---|---|
| **`reference/`** | Describes the system as it IS. Kept current. Undated. | **Yes** — if it's wrong, fix it |
| **`open/`** | The ONLY place with actionable items. | **Yes** — this is the to-do list |
| **`history/`** | Dated records of what happened: reviews, plans, handovers. Immutable once written. | As history only |
| **`archive/`** | Superseded designs. Kept for provenance. | **No** — do not implement from these |

Naming: dated files are `YYYY-MM-DD-slug.md`, undated ones `lower-kebab-case.md`.
A **date means the file is a snapshot of a moment** — a handover, a review, a
plan. `reference/` therefore never carries dates (it describes the present).
`history/` is all dates, and its files are not edited after their work closes (a
status banner at the top is the exception).

`open/` holds both: `residuals.md` is undated because it is continuously true,
while a dated handover lives here **while its work is still ahead of it** and
graduates to `history/` when that work closes. So a dated file in `open/` is a
job someone still has to do; the same file in `history/` is a record.

## Start here

- **Operating the harness** → [../HARNESS.md](../HARNESS.md)
- **Pointing it at a repo** → [../CLAUDE.md](../CLAUDE.md)
- **How it works** → [reference/architecture.md](reference/architecture.md)
- **What's left to do** → [open/residuals.md](open/residuals.md)

## reference/

- [architecture.md](reference/architecture.md) — the current design, and the
  five properties a refactor must preserve
- [codex-runner.md](reference/codex-runner.md) — the Codex runtime
- [failure-modes.md](reference/failure-modes.md) — 110 recorded failure modes.
  Load-bearing: tests and comments cite these by number (`FAILURE-MODES #66`)

## open/

- [2026-07-27-handover-fourth-deployment.md](open/2026-07-27-handover-fourth-deployment.md)
  — **start here.** The working brief for the next deployment session: current
  daemon state, everything that changed in the optimization session and what to
  watch on its first real dispatch (routing, synthetic codex pricing, patch-id
  skips, holds, recurrences), candidate issue batch, monitoring recipe, gotchas
- [residuals.md](open/residuals.md) — everything deliberately still open, why,
  and what triggers the work. **Read §1 before pointing this at a real repo.**
- [issues/](open/issues/) — three unresolved incident write-ups, not
  re-validated against the current tree

## history/

Read in this order to understand how the harness got here:

1. [2026-07-25-improvement-plan.md](history/2026-07-25-improvement-plan.md) —
   the original audit + reference design. Its lean core shipped; its §4 backlog
   is trigger-gated and now summarised in `open/residuals.md`
2. [2026-07-25-lean-core-handover.md](history/2026-07-25-lean-core-handover.md) —
   what the lean core actually built, and the decisions made during it
3. [2026-07-25-adversarial-review.md](history/2026-07-25-adversarial-review.md) —
   five review passes. Verdict: architecture sound, but a P0 list that silently
   disabled the harness or its safety layer
4. [2026-07-25-remediation-plan.md](history/2026-07-25-remediation-plan.md) —
   the fix plan, plus §6b's record of what executing it actually taught
5. [2026-07-26-p0-rereview.md](history/2026-07-26-p0-rereview.md) — an
   independent re-review of that remediation, which found five more P0s, **two
   of them introduced by the remediation itself**. Its §4 residuals are live and
   mirrored into `open/residuals.md`
6. Deployment era, in order:
   [2026-07-26-first-run-handover.md](history/2026-07-26-first-run-handover.md)
   (the recipe) →
   [2026-07-26-first-real-deployment.md](history/2026-07-26-first-real-deployment.md)
   (first unsupervised merge + four blocker fixes) →
   [2026-07-26-second-deployment-session.md](history/2026-07-26-second-deployment-session.md) →
   [2026-07-27-third-deployment-session.md](history/2026-07-27-third-deployment-session.md)
   (5 merges/day, codex runtime, spend telemetry) →
   [2026-07-27-handover-optimization-session.md](history/2026-07-27-handover-optimization-session.md)
   (the brief that became harness #2–#10 — all closed 2026-07-27)

The through-line, and the reason §5 of that last document is worth reading even
if you skip the rest: the defects that survived longest were **absence** bugs —
a variable no longer emitted, a failure no longer counted, a key no longer
cleared. Mutation testing cannot catch those. It proves a guard fires when code
changes; it never proves a guard exists for a path nobody wrote a test for.

## archive/

The original bash design (`v1-original-spec.md` and the numbered `01-`…`09-`
specs), plus the SDK-port planning docs (`refactor-sdk.md`,
`refactor-roadmap.md`, `architecture-status.md`) that describe a migration since
completed.

These remain accurate about the **legacy bash watchers** under `agent/`,
`review/`, `steward/`, which still exist and still run under
`FIXER_RUNTIME=codex` / `make run-fix-sh`. They are **not** accurate about the
default SDK path. `plugin/hooks/checks/` in particular is live for codex mode
and is pinned against the Python gate by `tests/test_gate_parity.py`.
