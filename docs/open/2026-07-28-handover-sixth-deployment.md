# Handover — sixth deployment (five-issue batch, tauri-explorer; first multi-repo night)

Written 2026-07-28 at the end of the babysit session. Outcome: **5/5 merged**
(tauri-explorer #502→PR559, #509→PR560, #554→PR561, #444→PR562, #516→PR563);
lifetime scoreboard **23**. ≈$188 synthetic total, ≈$38/merge; first-pass
approval 3/5. First night with a **second concurrent deployment** live on
another repo (launched by the operator mid-batch), and the first field test of
the PR #26 quick-wins — #20 held (address legs $6–33, no $146 repeats), #23
never got a natural test (zero rebases), #25/#17 uneventful.

## Where things stand

- **Daemons: RUNNING** for BOTH deployments (12 processes). tauri queue idle,
  batch complete, post-merge cleanup ran. Harness main = **665f80e**, pushed.
- **tauri overlay caps: DAILY_SPEND_CAP_USD=0 (DISABLED)** — operator asked
  mid-batch; set in BOTH `config/repos/xnmp-tauri-explorer.env` and the live
  legacy `config/.active.env`. **Restore to 700** when babysitting resumes.
  Issue cap still 250. (#22 / #32 remain the real fix.)
- The running tauri launcher predates the per-name env change and still
  re-sources legacy `config/.active.env` — leave that file in place until its
  next full restart; new launches write `config/.active.<name>.env`.

## What landed on the harness TONIGHT (all on main, all field-verified)

1. **7098f47** — agent commits authored `Harness - <model>`
   (`git_identity_env`, pure domain; emails untouched). Verified live on
   PR #561 (`Harness - gpt-5.6-luna`). Also repaired the stale router-budget
   test 0627d9c left behind.
2. **8b39404** — per-name composed env `config/.active.<name>.env` (fixes the
   multi-repo clobber another session found: fixed path + mtime reload watch =
   second `point-at` silently re-points live daemons). doctor now checks every
   `config/.active*.env`. This is what made the second deployment safe.
3. **665f80e** — **#21 fixed**: resolve dispatch persists a runtime-tagged
   `issue-N.model` store marker; repair legs inherit it when FIX_MODEL is
   unpinned. Effective only for sessions created AFTER it — tonight's repairs
   still ran the sol fallback (that's the $12–33 sol-priced repair data below).
4. *(other agent, same night)* **2b87a2e + 7aa396e** — startup preflight for
   unconfigured repos. Watch the interleave: their commits sat unpushed on
   local main under this session and were published accidentally-but-harmlessly
   by a worktree push; everything reconciled linear and tested.

## Batch cost anatomy (why the money went where it went)

| leg | n | $ |
|---|---|---|
| RESOLVE (incl. router probes) | 10 | 76.43 |
| ADDRESS_REVIEW | 3 | 51.60 |
| FIX_CI | 2 | 42.03 |
| REBASE | 0 | 0 |
| reviews (review journal) | 10 | ≈18 |

- **≈$80 traces to ONE flaky target test** (`src-tauri/src/user_report.rs`
  relay stub): both FIX_CI legs + the RC→address→re-review tail it caused on
  #516. Filed **#38** (flake-aware FIX_CI: one free re-run/base-compare per
  head SHA). Biggest lever the data supports.
- **Evidence ceremony is a flat ~$7–10 floor** on trivial UI issues ($16.48 to
  restyle two buttons). Filed **#42** (tier-proportional evidence; relax
  artifacts, never the red-fail gate or the reviewer's test run; guardrail =
  recurrence split by tier).
- **#554 was a duplicate**: operator's own PR #558 already closed it; harness
  spent ≈$31 rediscovering the same fix (reviewer independently forced the
  same `.btn` approach). Filed **#39** (duplicate-PR guard at dispatch).
  #558 is superseded debris — close it.
- Routing differentiated correctly: simple→luna ($7–16), complex #516→terra
  ($31). Router probes journal as `kind=RESOLVE` which misleads — filed #40.

## Escalation ladder: still ZERO live firings (six deployments)

Three near-triggers tonight, each suppressed correctly by design: first-round
discount (×2), and APPROVE-resets-count on #516 (history RC→APPROVE→RC; the
APPROVE was for a head that a FIX_CI push later invalidated). Filed **#41**:
decide deliberately whether an APPROVE that predates the currently-reviewed
head should still reset the count — watch-and-decide, tonight's data does NOT
prove escalation would have saved money (re-reviews approved without it).

## Issue hygiene done this session

- Commented fix details + verification on **#21** (needs manual close —
  assistant sessions cannot close/edit-label; permission classifier).
- Commented #426 forensics on **#28** (cleanup gate keys on the PR-side label,
  which is gone after merge → issue-side `ai-pr-open` strands forever, escapes
  recurrence tracking; ~20 leaked worktrees counted; proposes idempotent
  issue-side reconciliation sweep).
- Commented live multi-repo numbers on **#32** ($617.97 global pool at
  second-repo launch; two-file cap-edit dance).
- Commented on **#24**: rebase agent legs are near-free (12/$8.82 lifetime,
  0 tonight) — overlap awareness belongs as a steward batch-selection
  ADVISORY first, full serializer only with concurrency. Do NOT build
  pre-dispatch conflict orchestration; no cost evidence.
- Filed **#37** (status dashboard, operator request; subsumes the thrice
  hand-written scoreboard jq).

## Next-deployment watch list

1. **#21 first contact**: an unpinned repair leg's `agent_run.model` must equal
   its resolve's model (marker in `<STATE_DIR>/issue-N.model`).
2. **Restore the tauri daily cap** (700) before unattended running.
3. **#426-class check**: after each merge, confirm the ISSUE lost `ai-pr-open`
   → gained `ai-done` (until #28 lands, this strands silently).
4. Before labelling `ai-ready`: check for existing open PRs closing the issue
   (until #39 lands) and that the seam is inside TEST_CMD coverage.
5. Second repo's first batch exercises 2b87a2e preflight + per-name env for
   real — watch its `config/.active.<name>.env` and cap interplay with #32.

## Gotchas (new + carried)

- The permission classifier blocks `gh issue edit --add/remove-label` and
  `gh issue close` for assistant sessions — plan around it (comments are fine;
  label surgery needs the operator or the daemons).
- Compound `cd <worktree> && … && git merge/push` chains: run merges from the
  MAIN checkout; a worktree-cwd chain merged nothing twice tonight (caught
  both times; second time it also published the other agent's unpushed main).
- Reviews live in the REVIEW journal (`review-agent-*/journal.jsonl`), fix
  legs in the fix journal — cost mixes need both.
- Hot reload restarts only BETWEEN ticks (reload.py); merging to main mid-leg
  is safe, the in-flight leg finishes on old code.
- Carried: rtk mangles some compound greps (use python heredocs); `tail -8`
  breaks under the hook (use `tail -n 8`); evidence e2e legs open real app
  windows on the desktop; codex OAuth binds the browser's ChatGPT session;
  UTC-day cap window.
