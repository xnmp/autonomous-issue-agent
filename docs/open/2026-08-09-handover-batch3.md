# Handover — batch 3 wrap (2026-08-09 ~10:20Z)

Companion to `2026-08-09-batch3-report.md` (scoreboard, gaps 1–11, operator
actions, costs). This file is the operational state at wrap.

## Live state at wrap

- **Daemons: RUNNING** (fix/review/steward on xnmp/tauri-explorer, poll 60s),
  hot-reloaded onto `5045a44`. Reviewer defaults now sol/$15, triage $2
  (code defaults since `5178298`/`9c0e43e`).
- **Dashboard: RUNNING** on `5045a44` (restarted ~09:52Z; no hot-reload —
  restart after any dashweb change, `make dashboard`).
- **#457/#634 PARKED** at wrap (operator call: approved-on-merits but wedged
  twice by the downgrade-copy livelock; both root causes fixed but the
  operator chose human handoff over more rounds). Unpark = re-arm via the
  dashboard; the diagnosis work is in the PR branch.
- **Queue at wrap: 9 labeled lifecycle issues.** #528 ai-ready (was held
  behind the git-graph chain; should dispatch as remaining cluster PRs
  settle), ~6 ai-triage for the steward, plus in-flight remainders. The
  daemons will keep working these; nobody is watching.
- **Codex dashboard round 4 IN FLIGHT** in `.worktrees/dash-facelift`
  (branch `feat/dashboard-facelift`): 11 feedback items (scrollbar, headers,
  tooltip, false "CI Completed" stepper derivation + test, CI-colored PR
  button, escalated-row highlight, overview compaction). Prompt:
  scratchpad `dash-round4-prompt.md`. When it finishes: adversarial-review
  the diff, `make test`, merge to dev, `make dashboard-ui`, restart
  dashboard. NOT merged as of this handover.
- **dev is ahead of origin, unpushed** (many commits including 79e088a,
  5045a44, 3609a99, 5178298, 9c0e43e, c400657, 23fd840, FIX_CI-history and
  #163 merges, three facelift merges).

## What to verify next deployment (ledger rows already written)

1. `79e088a` — a mechanical rebase over a marker-bearing downgrade runs a
   paid review (grant expected) instead of copying; no ceiling walk.
2. `5045a44` — an unblock with no live reviewer verdict emits
   `operator_rearm_baseline` and a fresh round dispatches next poll.
3. FIX_CI failure history — fenced prior-attempts section appears in a
   FIX_CI prompt; flaky-signature guidance observed.
4. #163 — GraphQL pts/tick drop (71→33 expected) and no world regressions.
5. Sol reviewer cost mix vs. first-pass-approval guardrail (first sol
   re-review was $0.49).

## Sharp edges

- The unblock API needs header `x-issue-agent-dashboard: 1` on writes.
- config/repos overlays are gitignored — deployment-local only.
- `make install`/exact `uv sync` prunes extras; `make dashboard` is the
  supported restart (pins `--extra sdk --extra dash`).
- Env changes: recompose (`bash bin/compose-env.sh <name>`) only — daemons
  re-read the snapshot on hot-reload; do NOT restart (a restart script with
  an inverted drain check killed two live legs this batch).
- Steward GraphQL reduction (#163 item 4), stall-vs-hold composition,
  #184 authenticated grant channel, #182, #177 rerun-confirmation loop
  remain open.
