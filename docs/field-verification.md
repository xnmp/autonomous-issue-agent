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
| #23 patch-id re-review skip | PR #26 2026-07-27 | re-review skipped on unchanged patch-id (journal skip event) | **pending** — zero natural rebases since |
| commit author `Harness - <model>` | 7098f47 2026-07-28 | PR commits authored `Harness - <model>` | VERIFIED 2026-07-28 (sixth): PR #561 `Harness - gpt-5.6-luna` |
| per-name composed env | 8b39404 2026-07-28 | two concurrent deployments, no clobber/cross-reload | VERIFIED 2026-07-28 (seventh): tauri+keifu concurrent, per-name AGENT_ENV confirmed in /proc |
| #21 repair legs inherit routed model | 665f80e 2026-07-28 | repair `agent_run.model` == its resolve's; same session_id | VERIFIED 2026-07-28 (seventh): #518 terra, #557 sol |
| #28 stuck-issue self-heal | e5f5441 2026-07-28 | closed+`ai-pr-open` issues relabel `ai-done`, worktrees removed, state cleared | VERIFIED 2026-07-28 (seventh): ~23 repaired in one tick, #426 clean |
| #40 router legs journal `kind=ROUTE` | 21de6ac..e5f5441 2026-07-28 | `agent_run.kind == "ROUTE"` for classify probes | VERIFIED 2026-07-28 (seventh): 5/5 batch probes |
| `CODEX_COST_PER_MTOK` per-model rates | 21de6ac..e5f5441 2026-07-28 | one night's ledger prices tiers distinctly | VERIFIED 2026-07-28 (seventh): luna $0.39 / terra $27.41 / sol $84.46 |
| #39 `DUPLICATE_PR_GUARD` | 2f092ff 2026-07-28 | ai-ready issue with third-party closing PR → comment + `ai-blocked`, no resolve | **pending** |
| #38 `CI_FREE_RERUN` | d63d704 2026-07-28 | `ci_rerun` journal event, then `ci_flake` (green without a FIX_CI leg) | **pending** — no CI failure since landing |
| `CI_PENDING_MAX_SECONDS` stall escalation | 21de6ac..e5f5441 2026-07-28 | stuck-pending PR → `needs-human-review` + journal stall | **pending** |
| per-repo daily cap + `GLOBAL_DAILY_SPEND_CAP_USD` | 21de6ac..e5f5441 2026-07-28 | cap block journals against the right scope; capped daemon idles silently | **pending** — caps armed (700/1500) but never hit |
| startup preflight for unconfigured repos | 2b87a2e 2026-07-28 | doctor catches stale/missing config before daemons start | VERIFIED 2026-07-28 (seventh): caught 2 stale snapshots after a shared.env edit |
| pristine-base gate probe | 2b87a2e-era 2026-07-28 | red base blocks launch with the failing TEST_CMD output | MISBEHAVED 2026-07-28 (seventh): false red on green base, /tmp worktree loses node_modules → #49 |
| #48 failed `gh pr checks` reads UNKNOWN | 37055ac+0acb3da 2026-07-28 | log `gh pr checks failed … checks UNKNOWN this tick` on a fetch failure, with merge/fix-ci gates held (no spurious FIX_CI leg or merge that tick) | **pending** |
| #1 webhook wake (`WEBHOOK_WAKE=1`) | 40fedbb+0acb3da 2026-07-28 | `webhook-<name>.log` streams deliveries; a push wakes a daemon before its poll interval (wake-file touch) while polling stays as slow reconciler | **pending** — knob unset on both live deployments; needs gh-webhook ext + `admin:repo_hook` |
| #37 operator dashboard | 2f7a552 2026-07-28 | `issue-agent dashboard` renders every repo's live state from journals alone, mid-batch | **pending** |
