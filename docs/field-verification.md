# Field-verification ledger

One row per behavior change, from the moment it lands to the moment a live
deployment proves it. **A change lands `pending` and stays pending until a
journal event confirms it** — unit tests prove the code, this ledger proves the
deployment. The manager (CLAUDE.md, "The manager role") reads the pending rows
before every batch, ticks verdicts during, and appends new rows in the same PR
as any new behavior change.

Rules:
- **Evidence spec is written when the row is added**, not when it's checked —
  it must name the observable (journal event, label transition, ledger line),
  so verification is a lookup, not a judgment call.
- Verdicts: `VERIFIED` (cite deployment date + evidence), `MISBEHAVED` (file
  an issue, cite it, add a fresh row when the fix lands), `RETIRED` (behavior
  removed before ever firing).
- Never delete rows; this doubles as the harness's institutional memory of
  what has actually been proven vs merely shipped.

| change | landed | evidence spec | status |
|---|---|---|---|
| #16 escalation ladder (`route_escalation`) | be49b1d 2026-07-27 | `route_escalation` journal event on a repair leg after RC/recurrence signals | **pending** — 7 deployments, 3 near-triggers all correctly suppressed (see #41) |
| codex per-leg usage deltas | 27565d9 2026-07-27 | repair legs ledger single-digit deltas, never resolve magnitude | VERIFIED 2026-07-27 (fifth): address legs $6–33 |
| evidence-image commit gate | 2bcb99d 2026-07-27 | blocked commit + self-service downscale in a fixer transcript | **pending** — never fired |
| #20 repair legs skip evidence recapture | PR #26 2026-07-27 | no $100+ ADDRESS_REVIEW; address legs cite existing evidence | VERIFIED 2026-07-28 (sixth): address legs $6–33, no repeat of the $146 leg |
| #23 patch-id re-review skip | PR #26 2026-07-27 | re-review skipped on unchanged patch-id (journal skip event) | VERIFIED 2026-07-28 (keifu 2nd): `review_skip` on PRs #150 and #152 (09:04Z) after empty-commit CI retriggers — patch-id unchanged, no paid re-review. (Eighth's PR #574 rebase crossed conflicting merges → patch changed → paid re-review also correct.) |
| commit author `Harness - <model>` | 7098f47 2026-07-28 | PR commits authored `Harness - <model>` | VERIFIED 2026-07-28 (sixth): PR #561 `Harness - gpt-5.6-luna` |
| per-name composed env | 8b39404 2026-07-28 | two concurrent deployments, no clobber/cross-reload | VERIFIED 2026-07-28 (seventh): tauri+keifu concurrent, per-name AGENT_ENV confirmed in /proc |
| #21 repair legs inherit routed model | 665f80e 2026-07-28 | repair `agent_run.model` == its resolve's; same session_id | VERIFIED 2026-07-28 (seventh): #518 terra, #557 sol |
| #28 stuck-issue self-heal | e5f5441 2026-07-28 | closed+`ai-pr-open` issues relabel `ai-done`, worktrees removed, state cleared | VERIFIED 2026-07-28 (seventh): ~23 repaired in one tick, #426 clean |
| #40 router legs journal `kind=ROUTE` | 21de6ac..e5f5441 2026-07-28 | `agent_run.kind == "ROUTE"` for classify probes | VERIFIED 2026-07-28 (seventh): 5/5 batch probes |
| `CODEX_COST_PER_MTOK` per-model rates | 21de6ac..e5f5441 2026-07-28 | one night's ledger prices tiers distinctly | VERIFIED 2026-07-28 (seventh): luna $0.39 / terra $27.41 / sol $84.46 |
| #39 `DUPLICATE_PR_GUARD` | 2f092ff 2026-07-28 | ai-ready issue with third-party closing PR → comment + `ai-blocked`, no resolve | **pending** |
| #38 `CI_FREE_RERUN` | d63d704 2026-07-28 | `ci_rerun` journal event, then `ci_flake` (green without a FIX_CI leg) | **pending** — eighth 2026-07-28: `ci_rerun` fired (PR #574 09:41:46Z), failure was real so FIX_CI correctly followed; the flake path (green on rerun) still unobserved |
| `CI_PENDING_MAX_SECONDS` stall escalation | 21de6ac..e5f5441 2026-07-28 | stuck-pending PR → `needs-human-review` + journal stall | VERIFIED 2026-07-28 (keifu 2nd): PR #150 windows job queued 3609s → `ci_pending_deadline` journal event + comment + relabel — but MISBEHAVED on reversal: re-fires every tick while the check runs, operator cannot resume → #54 |
| per-repo daily cap + `GLOBAL_DAILY_SPEND_CAP_USD` | 21de6ac..e5f5441 2026-07-28 | cap block journals against the right scope; capped daemon idles silently | **pending** — caps armed (700/1500) but never hit |
| startup preflight for unconfigured repos | 2b87a2e 2026-07-28 | doctor catches stale/missing config before daemons start | VERIFIED 2026-07-28 (seventh): caught 2 stale snapshots after a shared.env edit |
| pristine-base gate probe | 2b87a2e-era 2026-07-28 | red base blocks launch with the failing TEST_CMD output | MISBEHAVED 2026-07-28 (seventh): false red on green base, /tmp worktree loses node_modules → #49 |
| #48 failed `gh pr checks` reads UNKNOWN | 37055ac+0acb3da 2026-07-28 | log `gh pr checks failed … checks UNKNOWN this tick` on a fetch failure, with merge/fix-ci gates held (no spurious FIX_CI leg or merge that tick) | **pending** |
| #1 webhook wake (`WEBHOOK_WAKE=1`) | 40fedbb+0acb3da 2026-07-28 | `webhook-<name>.log` streams deliveries; a push wakes a daemon before its poll interval (wake-file touch) while polling stays as slow reconciler | **pending** — knob unset on both live deployments; needs gh-webhook ext + `admin:repo_hook` |
| codex instruction-budget: truncation pointer + doctor size check | 2026-07-28 | a codex leg on an over-budget repo (tauri, 37KB) shows the `[Repository instructions truncated…]` pointer in its worktree `AGENTS.override.md`; doctor warns pre-launch with the measured sizes | **pending** — doctor half verified same-day against live tauri config (warned ≈37086B); the in-prompt pointer needs the next codex leg on tauri |
| #37 operator dashboard | 2f7a552 2026-07-28 | `issue-agent dashboard` renders every repo's live state from journals alone, mid-batch | VERIFIED 2026-07-28 (keifu 2nd): both repos, cost mix, in-flight — caveat: healthy-idle daemons read `STALE` (journal-only view can't see a quiet poll loop) |
| #54 CI-stall escalation one-shot per head | 1258eab 2026-07-28 | one `ci_pending_deadline` + one relabel/comment per (PR, head SHA) even across ticks (`ci_pending_escalated` state); operator's `+ai-pr-open` reversal sticks while the check still runs | **pending** |
| #55 fixer-identity unblock | 1258eab 2026-07-28 | `ai-blocked` issue + reply from FIX_BOT's own login (postdating the block comment, not itself a block comment) → RESUME leg dispatched, no resume loop on the fixer's own posts | **pending** |
| #56 repair legs dispatch before fresh resolves | 1258eab 2026-07-28 | a tick with both queued: ADDRESS_REVIEW/FIX_CI/REBASE dispatches before a new `ai-ready` RESOLVE (journal order), no repeat of the 88-min starvation | **pending** |
| #31 `continue-on-error` checks read advisory | 1258eab 2026-07-28 | a failed advisory job on an otherwise-successful run does NOT flip `ci_state` to FAILURE (no FIX_CI leg; log names the advisory checks) — retires keifu's `CI_IGNORE_CHECKS='^Security Audit$'` workaround | **pending** |
| #52 timestamped role/webhook logs | 1258eab 2026-07-28 | every line of `<role>-<name>.log` (and `webhook-<name>.log`) carries a UTC ISO prefix via `bin/lib/logging.sh:timestamp_lines` | **pending** — trivially checkable on next launch |
| #57 no-PR terminal retires worktree+branch | 1258eab 2026-07-28 | an already-implemented close (pushed/no-PR) removes its `issue-<n>` worktree and only harness-owned local branch in the same action batch — no leftovers at batch end | **pending** |
| #33 doctor.sh toolchain-pin drift check | cfb165a 2026-07-29 | a real `doctor.sh` preflight run against a target repo prints a `rustc`/`node`/`python3` drift or missing-pin warning under `== toolchain pin (drift vs CI) ==` before the first PR exposes the same gap in CI | **pending** — never run against a real target repo yet |
