# Codex fixer SIGKILL'd (exit -9) during heavy e2e verification, loses work before commit/push

**Labels:** bug, fixer, codex-runtime, reliability

## Symptom

On a frontend PR whose `.harness/test-cmd` runs a full Playwright e2e suite (stub
backend + Chromium + vite), the codex fixer repeatedly dies with **`exit -9`
(SIGKILL)** — 6 times across 8 dispatches on Myapp PR #354 (2026-07-21). The fix
itself is correct and the scoped test passes green; codex dies on its *verification*
step (full-suite run, mutation check) or even mid-`git commit`, so the work is never
pushed. Each death burns a fixer leg and increments the address-review circuit
breaker → 354 hit `needs-human-review` twice and needed manual breaker resets.

It landed on the 2nd manual reset only because one attempt happened to get
`git commit && git push` out during a gap between kills.

## What it is NOT (ruled out)

- **NOT the harness idle watchdog.** `grep -c "IDLE — no stderr"` = 0 across all 6
  kills. The runner's per-line idle watchdog never fired; `hung=False` on every
  exit -9 (no `status=timeout`/"hung" envelope). The teardown logic
  (`codex_runner.py:530-570`) is correct — `_pump_stderr` self-bounds, no outer
  runtime cap (the comment at :537 documents a *previous* bug where a single outer
  timeout killed every run >30s with exit -9 and no IDLE line — that is fixed; this
  is a different cause).
- **NOT the harness killpg.** `_kill_quietly` only fires on the post-hang stdout
  drain timeout, which didn't trigger (hung=False).
- **NOT steady-state OOM.** `memory_pressure` showed 57–61% free when sampled.

So codex is being SIGKILL'd **externally** (by the OS), asynchronously to what it's
doing — the mid-`git commit` kill is the tell: git commit is not heavy, so the kill
came from elsewhere (a still-winding-down e2e child from the prior step, or a
process-group-wide signal).

## Suspected cause (hedged — not proven)

Most likely **macOS jetsam / memory-pressure killer** killing codex or its process
group during a transient spike when the full Playwright stack spawns (Chromium +
multiple node workers + stub uvicorn + vite dev server, all at once). Steady-state
free memory looks fine, but the e2e launch is a burst allocator; a short spike can
trip jetsam even at 60% average-free. The `exit -9` + no-IDLE + heavy-e2e correlation
fits this. **Unconfirmed** — needs a `log stream --predicate 'sender == "kernel"'`
capture during a kill, or `sudo dmesg`/Console "jetsam"/"lowpri" events, to prove.

An alternative (also unproven): the e2e subprocess tree spawned under codex's session
(`start_new_session=True`, so codex is the pgid leader) leaves orphans holding
ports/memory when a step is interrupted; a subsequent `pnpm run kill-ports` /
cleanup, or the OS reaping the group, catches codex too.

## Evidence (Myapp #354, 2026-07-21)

6× `exit -9`, all clustered on heavy steps:

| Kill # | codex was running |
|---|---|
| 1, 2 | `bash .harness/test-cmd` (full user-flows.spec.ts suite) |
| 3, 4 | mutation check (Playwright stub-backed flow) |
| 5 | `git commit` (children from prior e2e step still winding down) |
| … | scoped test itself: **passed green** (`✓ …#347 (3.1s)`) before the full-suite kill |

`IDLE — no stderr` count: **0**. `memory_pressure`: 57–61% free.

## Impact

- Frontend PRs that verify via full Playwright e2e are effectively un-landable by the
  codex fixer without manual breaker resets — codex fixes correctly but can't survive
  its own verification to commit.
- Each SIGKILL increments the review breaker → false `needs-human-review` escalations
  on PRs that are actually fixed.
- Orphaned e2e stacks (ports 8080/3001) leak when codex dies mid-run and its EXIT
  trap never fires (SIGKILL is uncatchable) — a later run's e2e then can't bind →
  more thrash. (Observed; had to manually `pkill` orphans.)

## Proposed fixes (candidates, pick after confirming cause)

1. **Confirm the killer first.** Capture `log stream --predicate 'eventMessage
   CONTAINS "jetsam"'` (or Console.app → "jetsam"/memorystatus) during a repro to
   prove memory-pressure vs. something else. Don't fix blind.
2. **Cap e2e concurrency / memory** in `.harness/test-cmd` for frontend PRs — run
   Playwright with `--workers=1`, headless-shell only, and tear down the stub backend
   between phases so peak RSS stays under jetsam's threshold.
3. **Commit-before-verify ordering.** Have the fixer `git commit` the fix as soon as
   the *scoped* test passes, BEFORE the expensive full-suite/mutation verification —
   so a SIGKILL during verify doesn't lose a correct, already-green fix. (The scoped
   test passed green every time; only the full-suite verify killed it.)
4. **Daemon-side orphan reaping on codex death.** When codex exits (esp. -9), the
   daemon should killpg any surviving e2e children (ports 8080/3001) so the next
   dispatch starts clean — instead of relying on codex's own EXIT trap, which SIGKILL
   bypasses. (Today this fell to a human running `pkill`.)
5. **Distinguish external-SIGKILL from agent failure** in `CodexRunner`: an `exit -9`
   with no captured envelope + no IDLE is an infra kill, not an address-review
   failure — it should NOT increment the review breaker (which caused the false
   `needs-human-review`). Retry it like a transport drop instead.

## Related

- `docs/issues/persistent-review-session.md` — the reviewer's fresh-process-per-round
  cost; #5 above (don't count infra kills against the breaker) is the fixer analogue.
