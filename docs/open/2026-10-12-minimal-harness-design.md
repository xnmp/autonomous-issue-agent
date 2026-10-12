# Minimal harness: target design

Draft, 2026-10-12. This is the yardstick for the subtractive refactor. Anything this page doesn't need is deleted, not migrated.

## Rules
1. **GitHub owns workflow state.** Each tick reads it fresh: labels, PR head, mergeability, required checks, bot reviews, comments.
2. **SQLite owns only what GitHub can't hold:** money (reserve, then settle), running legs (leases, sessions), idempotency keys. Attempt counts are derived from the legs.
3. **A machine failure never counts as an attempt.** Retry it with backoff; only money caps bound it.
4. **Agent output is a claim, never authority.**
5. **Only judgment stops work:** spend, access, policy, or a dispute.

## Tick
`observe` (one GraphQL query plus SQLite) → `derive` (a pure function, state never stored) → `act` (idempotent: label, comment, merge, dispatch a leg).

| Derived state | Action |
|---|---|
| issue `ai-ready`, no PR, no lease | RESOLVE |
| required check red on head | FIX_CI |
| conflicted | REBASE |
| CI green, no bot review on head | REVIEW |
| bot requested changes on head | ADDRESS_REVIEW |
| bot approved head + CI green | merge |
| `needs-human` | wait |

Attempts are counted per `(item, action)`: agent-attempt legs since the last reset event on GitHub (a non-harness comment, review or push). The cap is 3. Review rounds are the bot's CHANGES_REQUESTED reviews on the PR, also capped at 3. Hitting either cap means `needs-human` plus a comment saying why. Decisions: [refactor decisions](2026-10-12-refactor-decisions.md).

## Labels
- `ai-ready`: a human admits the issue.
- `needs-human`: the only stop, with one writer. A human reply or removing the label releases it.

Harness PRs are identified by branch prefix, not by a label.

## Quality
Quality comes from the repo's required CI checks plus one model review on the exact head. PRs touching `.github/workflows/**` get `needs-human`.

## Agents
Agents push and return; they never wait on CI. They ask a question only when they need judgment. A comment counts as a reply only if it is newer than the leg's end. A wall-cap hit is a machine failure: resume once.

## Defaults (owner may veto)
- The TDD gate becomes prompt guidance plus reviewer judgment.
- The steward is deleted.
- Cutover: drop the local workflow tables and re-derive everything from GitHub.

## Target

| | Now | Target |
|---|---|---|
| core src lines (excludes dashboard) | 104,285 | **≤ 15,000** |
| test lines | 148,404 | ≤ 20,000 |
| stop states | 34 | **1** |
| labels | 16 | **2** |
| SQLite tables | many workflow tables | **3**: legs+money, leases, idempotency |
| roles | fix, review, steward | fix, review |
| ADRs | 17 | this page + money/supervision |

Kept, slimmed: money accounting, process supervision, the Claude/Codex runtime adapters, the domain/adapter split.

**The dashboard is a separate component, outside the core budget** (12,249 src lines now, across `domain/dashboard.py`, `app/dashboard.py` and `app/dashweb/`). It is read-only: it consumes the derived state and the telemetry, and holds no authority. The core must not import it. It may only trigger the operations the design keeps, which means label changes on GitHub. Slice 8 trims it to match the core.

**Target layout** (a guide, not a straitjacket):

```text
domain/   state.py (derive) · policy.py (state → actions) · money.py · outcome.py (classify a leg result) · labels.py
adapters/ github.py · store.py (3 tables) · claude.py · codex.py · process.py (supervise, kill) · worktree.py
app/      tick.py · fix.py · review.py · cli.py · config.py
dashboard (separate, read-only consumer)
```

## Deletion order
Each slice keeps `make test` green and must shrink the line count.
1. Review-time operator tests.
2. Self-clear and machine blocks.
3. Machine-failure-as-debt.
4. Labels down to two.
5. The local workflow model: fences, re-arms, releases, gates, continuation and imports.
6. Fixer publication gates: the TDD red/green gate and the governor checks, replaced by prompt guidance.
7. The steward.
8. Dashboard and telemetry, trimmed to the derived state.
9. Adapter trim (Codex, CLI, store, money path, config, prompts) to reach ≤15k.

The execution order is 2, 3, 7, 6, 8a, 5a–5d (with the two-label contract), the slice 4 residue, 8b, then 9 (from the [audit](2026-10-12-refactor-audit.md)).
