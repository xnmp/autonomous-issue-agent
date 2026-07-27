# Handover — optimization session (fix issues #2–#9, then resume deployment)

Written 2026-07-27 ~05:00 UTC at the end of the third deployment day
([session record](2026-07-27-third-deployment-session.md) — read it for the
full narrative; this file is the working brief for the NEXT session).

## Where things stand right now

- **Daemons: RUNNING** against `xnmp/tauri-explorer` under `make point-at
  REPO=xnmp-tauri-explorer` (foreground point-at in a background shell).
  Config: `fixer=codex`, `CODEX_MODEL=gpt-5.6-sol` (verified answering on the
  subscribed account `chonw89@gmail.com`), reviewer/steward = Claude,
  `probe=on/300s`, `arm-on-open ACTIVE`. Check with:
  `grep -E "fixer=|probe=|arm-on-open" ~/.local/state/issue-agent/logs/fix-xnmp-tauri-explorer.log | tail -3`
- **Hot reload is live and commit-gated**: committing to this repo restarts
  all three daemons between ticks (never mid-session) within ~20s; editing
  the composed `config/.active.env` does the same (single-file mtime
  exception). NO manual restarts — just commit. A post-reload boot crash
  slow-loops at 60s and self-heals on the next (fixing) commit.
- **Scoreboard**: 12 lifetime merges; 5 today (#539–#543). No issues
  labelled; the target queue is idle. Daily ledgered spend 2026-07-27 ≈ $86.
- **dev branch protection** (operator-applied): 6 required checks
  (frontend, rust, webkit, "smoke (ubuntu-latest)", launch-smoke, code-maps
  — deliberately NOT the flaky "Performance Regression Check") + 1 required
  review + repo allow-auto-merge. This is what makes arm-at-creation safe;
  don't remove it casually.

## The work: issues #2–#9 on xnmp/autonomous-issue-agent

Suggested order (dependencies/value first). Each issue body carries its own
evidence and acceptance criteria; this is just sequencing rationale.

1. **#8 difficulty routing** — biggest token lever. Grounding data: Claude
   resolves ran $13–20 each with 13–20M cache-read vs ~250 raw-input tokens
   (cost ≈ prefix re-reads × tier price); same profile on sonnet ≈ $3–5.
   `route_model`/`MODEL_TIERS` already exist (`app/planners/tasks.py`,
   runtime-aware). Wire into resolve dispatch only when `FIX_MODEL` unset.
   NOTE: P4.9 cross-leg cache sharing is MOOT (trigger was cache_read≈0;
   reality is the opposite) — recorded in the issue.
2. **#4 synthetic codex pricing** — config + docs mostly; makes the existing
   caps bound codex token volume (today's free-tier exhaustion is the
   motivating incident).
3. **#2 patch-id rebase → skip re-review** — domain change in the re-review
   trigger; pairs naturally with tauri-explorer#544 (below).
4. **#6 journal `hold` events** + **#7 review `status=""`** — small
   observability fixes; do together (both touch journal emission).
5. **#3 conditional fix-daemon probe** — reuse `WorldProbe`; the condition
   ("last world had no open harness PR and no in-flight issue") lives at the
   wiring layer like the review/steward gate.
6. **#5 evidence/review overlap** — AGENT.md prompt-ordering change only;
   measure on the next deployment day before touching anything else.
7. **#9 docs paragraph** (HARNESS.md) — trivial.

Also **tauri-explorer#544** (per-issue lessons/code-map files): a change in
the TARGET repo's docs conventions + a matching AGENT.md tweak (write
`docs/lessons/<issue>.md` instead of appending the shared file). This kills
the every-merge-conflicts-every-PR rebase churn at the source and is worth
doing BEFORE resuming deployment, since it also shortens the post-approval
critical path (each rebase re-triggers ~25 min of webkit).

## Then: resume deployment

1. Pick issues from the open queue (`gh issue list -R xnmp/tauri-explorer`)
   — per #9, batch DISJOINT areas (e.g. one scm + one git-graph + one
   command-palette) so the path-overlap serializer doesn't single-file them.
   Remaining candidates from the old shortlist: #512 (compare two commits),
   #519 (archive/trash untracked). #514 (avatars: network+cache) was
   deliberately avoided as complex.
2. `gh issue edit <n> -R xnmp/tauri-explorer --add-label ai-ready` (or
   `issue-agent ready <n>` from inside the repo).
3. First codex/sol resolve: watch it end-to-end (sol is unexercised as the
   implementer — terra did #542 in 7 min/1 review round; sol should match or
   beat that).

## Monitoring recipe (what worked)

- Typed journal monitor (primary; NEVER grep the human log for state):
  `tail -n 0 -F <state>/journal.jsonl … | grep --line-buffered '^{' | jq --unbuffered -rc '<per-event formatter>'`
  over the three role journals + `~/.local/state/issue-agent/spend.jsonl`.
  Journals: `~/.local/state/{issue,review,steward}-agent-xnmp-tauri-explorer/journal.jsonl`.
- Narrow diagnostics tail (things not in the journal): pattern
  `world build failed|sdk: (WALL|IDLE|transport died|stream ENDED|usage calibration)|sdk-stderr:`.
- Monitor deliveries can arrive MINUTES late and batched — never infer
  "just happened" from arrival time; check the journal `ts`.

## Gotchas that cost time today (don't rediscover)

- `pgrep/pkill -f` with a literal pattern matches the command's OWN shell
  under the rtk hook (the eval'd text contains the pattern) — use a
  char-class breaker: `pgrep -f "issue_agent[.]app[.]main"`.
- gh label SEARCH is stale for closed PRs/issues (a day+); the PR/issue
  OBJECT's own labels are truthful. Never trust `gh pr list --label
  --state all` membership to mean current labels (handled in world builds by
  `1a3d46f`, but applies to any ad-hoc query too).
- `pick_ready_issue` HOLDS ai-ready issues whose declared paths overlap an
  open PR (silent until #6 lands) and SKIPS issues with open blocked-by deps.
- Codex OAuth binds the browser's live ChatGPT session — verify with the
  id_token `email` claim in `~/.codex/auth.json` (memory:
  codex-cli-account-gotchas).
- Stream-vs-result usage skew ranged 3.4–6.4× today (4 calibration pairs
  journaled as `usage_stream`); the estimator learns in stream units now —
  sanity-check the first real ABORT estimate against these pairs.
- The daily spend cap is per UTC DAY (resets 10:00 local); at ~$86/day for a
  5-merge claude-heavy day the $150 cap has headroom, but a mixed day plus
  yesterday's phantom overestimates tripped it once — the estimates are fixed,
  the UTC window is not (accepted design).

## State/config file map (quick orientation)

- `config/repos/xnmp-tauri-explorer.env` — overlay (runtime, model, caps,
  TEST_CMD with vitest+cargo, CARGO_TARGET_DIR shared cache).
- `config/.active.env` — composed live env; editing it hot-reloads daemons.
- `~/.local/state/issue-agent-xnmp-tauri-explorer/` — fixer state: sessions
  (now runtime-tagged `codex:`/`claude:`/bare=claude-legacy), retry counters,
  journal.
- Worktrees: `~/Repos/tauri-explorer/.claude/worktrees/issue-<n>` (session
  transcripts for forensics: `~/.claude/projects/-home-chong-Repos-tauri-explorer--claude-worktrees-issue-<n>/`).
