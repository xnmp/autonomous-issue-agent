# Session record — second deployment day (xnmp/tauri-explorer), 2026-07-26 evening

Follows [2026-07-26-first-real-deployment.md](2026-07-26-first-real-deployment.md).
Goal for the session: **merge 5 issues, fix harness defects along the way, add
logging wherever a failure cause was unclear.** Goal met.

## Outcomes

| Issue | PR | Result |
|---|---|---|
| #481 debounce tab-state writes | #535 | **MERGED** |
| #499 git-graph font mismatch | #536 | **MERGED** |
| #501 Alt+L/P pane focus (salvaged from last session's abandonment) | #537 | **MERGED** |
| #506 git-repo tab icon (salvaged; root-caused to unshipped half of #471) | #534 | **MERGED** |
| #500 long file names in git graph | #538 | **MERGED** |

7 lifetime harness merges (with #504/#494). Ledgered spend for the day:
~$98 across 18 runs, of which ~$19 is a known over-estimate (see "estimator
units bug"); realistic total ≈ $80 against the $150 daily cap.

**§3.6 from the first-deployment handover is now VERIFIED, two ways:** the
#506 resolve captured screenshots with zero governor denials (took the
Playwright `webServer` path AGENT.md now recommends), and a different leg that
did hit the `run_in_background` denial received the corrected message and
adapted on the next call (foreground until-loop) instead of flailing.

## Harness fixes landed this session (all committed, suite green)

| Commit | Fix | Verified live? |
|---|---|---|
| `2ccda41` | TEST_CMD discovery prompt: revert-gate purpose, all-language coverage, exclude perf/e2e flake tiers | prompt-only; overlay composed vitest+cargo runs in gates ✓ |
| `5f08893` | Abort forensics: last_trace was debug-gated (`last: none` always); limping-wall path ledgered $0.00; msg-count on aborts; usage-calibration line | ✓ (`aborting after 293 stream messages (last: TaskStartedMessage)`) |
| `1a89e97` | Auto-merge marker only latches when GitHub verifiably armed/merged (the #537 forever-wedge) | ✓ (#537 unwedged and merged; #534 refusals retried instead of latching) |
| (same batch) | Blocked rebase/fix-ci posts the agent's `needs` to the PR | not yet exercised |
| `ddf4150` | Tick executes cheap terminal actions before agent dispatches (finished work no longer queues behind hour-long sessions) | live after this restart |
| `1a575fc` | Stale-interrupt auto-reject guidance (AGENT.md + resume reminder); docs/issues/rebase-stale-interrupt-auto-reject.md | live after this restart |
| `6ab9cbb`+`8e037f4` | Don't arm CONFLICTING PRs (kills per-poll gh churn); contract test updated | live after this restart |
| `31e2e90` | Rebase prompt: push and RETURN — do not babysit remote CI (observed ~20 min of `gh pr checks` loops per rebase) | live after this restart |

Config (gitignored overlay): TEST_CMD composed with `cargo test --quiet` +
shared `CARGO_TARGET_DIR` primed (~/.cache/issue-agent/xnmp-tauri-explorer);
wall cap 2700→3600s.

## New findings, still open

1. **Estimator units bug (top priority).** The calibration line showed a
   completed run's stream-accumulated usage is ~3.5× its ResultMessage total
   (43.2M vs 12.1M tokens; output_tokens agree). `observed_cost_per_token`
   learns $/token from RESULT totals but aborted runs are priced on STREAM
   totals → aborted-run estimates ~3.5× high (the "$27" #506 abort was likely
   ~$8), silently eating issue/daily caps. Fix: journal both totals on
   completed runs and learn the rate against stream totals (same units as the
   abort path). One calibration pair so far — collect more before trusting a
   constant.
2. **$0.00 completed run.** REBASE #538 (first one) journaled
   `status=success, cost_usd=0.0, duration 238s` — a completed run with no
   cost. Same family as the drop diagnostics; check the envelope next time.
3. **Stale-interrupt CLI bug** — mitigated in prompts, root cause in the CLI
   resume path unproven; see docs/issues/rebase-stale-interrupt-auto-reject.md.
   `TaskStartedMessage` as the last message before #538's wall abort suggests
   SDK background-task machinery is active in these sessions — possibly
   related; unexplored.
4. **Heartbeat cries wolf** (§3.2 of first handover) — untouched; still fires
   after every long dispatch.
5. **Target repo: append-only shared docs files** (`docs/lessons_learnt.md`,
   `docs/code-map/map-folder.md`). Every branch appends to the same files, so
   EVERY merge conflicts EVERY open PR → a rebase + re-review cycle (~$3–10,
   plus 2–3 review rounds on #538) per landing. Recommend filing an issue on
   tauri-explorer to move to per-issue files (`docs/lessons/<issue>.md`).
   `merge=union` gitattributes will NOT help: GitHub's mergeability check
   ignores merge drivers.
6. **Arm-at-creation knob** (user question): on repos whose branch protection
   encodes required-review + required-checks, arming auto-merge at PR creation
   would shave latency and survive daemon downtime. Needs a doctor probe of
   the protection rules before enabling. Moot on this repo (no protection).
7. **Rebase retry counter vs docs-conflict churn:** #538 consumed 2 of 3
   rebase retries on pure docs conflicts. On a busy day the cap can trip on a
   PR with nothing wrong. Consider not consuming a retry when the conflict
   set is docs-only, or fixing (5) and forgetting this.

## Operational notes

- The daemon log's ACTION lines are unprefixed (`dispatch …`, `relabel …`);
  only config/sdk/heartbeat lines carry `fix:`/`review:`. Monitors/greps keyed
  on the prefix miss everything that matters.
- `pytest … | tail` in zsh reports tail's exit status — one commit (6ab9cbb)
  briefly landed with a red test that way (amended in 8e037f4). Check
  `PYTEST_EXIT` explicitly.
- zsh: `echo === X ===` fails (`=cmd` expansion); avoid leading `=` words.
- Session transcripts under
  `~/.claude/projects/<worktree-slug>/<session-id>.jsonl` are the forensic
  gold mine — the #534 "gave up in 18.7s" mystery was solved entirely from
  timestamps there (53ms tool_use→rejection = mechanical, not model).
