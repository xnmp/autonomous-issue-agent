# Handover — fourth deployment session (new issue batch on tauri-explorer)

Written 2026-07-27 at the end of the optimization session. The previous
handover's work is DONE: harness issues #2–#9 all closed (commits
f1b3e2d…c904db7), plus three follow-ons — `FIXER_RUNTIME=glm` (755dd41),
the CLAUDE.md direction/metrics section, and recurrence recording #10.
tauri-explorer#544 landed as per-issue lessons files via PR #548, which went
through the harness's own full loop (reviewer-bot APPROVE at $0.80, six checks,
auto-merge) — closing the loop on the very machinery it changed. Session
records live in `docs/history/2026-07-2*`.

## Where things stand right now

- **Daemons: RUNNING** against `xnmp/tauri-explorer` (`make point-at
  REPO=xnmp-tauri-explorer`, foreground in a background shell). Hot reload is
  live: committing to this repo restarts all three between ticks; NO manual
  restarts. Config: `fixer=codex`, `CODEX_MODEL=gpt-5.6-sol`, reviewer/steward
  = Claude, probe on, arm-on-open ACTIVE, caps issue=$50 / daily=$150.
- **dev branch protection** (operator-applied): 6 required checks + 1 required
  review + allow-auto-merge. This is what makes arm-at-creation safe.
- **Scoreboard**: 13 lifetime merges (12 + PR #548). Target queue is idle —
  nothing labelled.

## NEW since the last deployment day — what to watch on first contact

Every item below is code that has never seen a real dispatch. First-tick
behaviors to verify, in the order they will fire:

1. **Difficulty routing is ACTIVE** (#8). With `FIX_MODEL` unset, every
   resolve now runs a ≤$0.10 classifier first, then resolves on the tier's
   model — codex family: luna/terra/sol. Watch the fix log for
   `agent #N: route — tier=… → model=…`. NOTE the interaction: a routed
   resolve's model now OVERRIDES `CODEX_MODEL=sol` (task model wins in
   `build_codex_argv`), so sol is no longer the guaranteed resolve model —
   it is the `complex`-tier answer and the default for FOLLOW-UP legs
   (resume/fix-ci/rebase, which stay unrouted). A garbage classifier answer
   degrades to terra; a router raise degrades to sol-by-default. If routing
   misbehaves, pin `FIX_MODEL` in the overlay to bypass it entirely.
2. **Codex runs now ledger synthetic dollars** (#4): $5/$30 per MTok (sol list
   price). The caps can therefore TRIP on codex-only load for the first time.
   Sanity-check the first codex resolve's ledgered cost; if a normal day of
   fictional dollars approaches the $150 daily cap, raise
   `DAILY_SPEND_CAP_USD` deliberately rather than letting it bite mid-queue.
3. **Patch-id re-review skip** (#2): after a mechanical rebase, expect the
   review log's carry-forward line instead of a paid RE_REVIEW; the round
   counter must not advance. A rebase that actually changes the diff must
   still re-review (both paths are tested, neither is field-proven).
4. **Hold visibility** (#6): a held ai-ready issue now logs
   `HOLD: issue #N … (pr#M)` every fix poll and journals `hold` once. If the
   queue looks stuck, this line is the first thing to look for.
5. **Fix-daemon probe** (#3): idle fixer ticks are 1 REST point. Expect
   `world build` lines only after repo activity; in-flight work (any open
   harness PR / ai-working issue) forces full builds every tick.
6. **Evidence/review overlap** (#5, prompt-only): fixer transcripts should
   show the adversarial subagent and an evidence-capture subagent spawned in
   ONE message. Measure wall-clock per issue against the old 6–9 min serial
   wait — this was the change we said we'd measure before touching more.
7. **Recurrence recording** (#10): steward journals `recurrence`
   (kind=reopen|refiled), labels/comments refiled ones. Zero steady-state
   cost; nothing to do unless an event appears — then a human judges it.
8. **Review verdicts in the journal** (#7, verified live already): review
   `agent_run` events carry `status: APPROVE|REQUEST_CHANGES` — verdict
   analytics are one jq now.
9. **glm runtime exists but is NOT active** (755dd41): to try it, set
   `FIXER_RUNTIME=glm` + `GLM_SETTINGS_FILE=~/.cc-glm/settings.json` in the
   overlay. Routing then uses glm-4.7/glm-5.2/glm-5.2[1m]. Known gap:
   `issue-agent init` doesn't offer glm interactively yet.

## The batch: picking issues

Per the (now documented in HARNESS.md, #9) rule: label DISJOINT areas so the
path-overlap serializer doesn't single-file them. Current queue candidates by
area (verify bodies before labelling — none are triaged):

- **app shell / trivial**: #545 (window title "<cwd> - Tauri Explorer") — a
  perfect first probe of routing: it should classify trivial → luna.
- **scm**: #519 (archive/trash untracked) or #518 (per-file history) or #516
  (hunk-level stage/unstage).
- **git-graph**: #529 (filter by path) or #521 (inline CI log viewer) — avoid
  labelling two git-graph issues at once.
- **dialogs/app UI**: #547 (in-app bug report dialog).
- Previously avoided as complex: #514 (avatars, network+cache). #526/#525
  (PR actions/threads) look API-heavy — triage before considering.

Label with `issue-agent ready <n>` from inside the target repo, or
`gh issue edit <n> -R xnmp/tauri-explorer --add-label ai-ready`.

## Monitoring recipe (unchanged, still what worked)

- Typed journal monitor (primary; NEVER grep the human log for state):
  `tail -n 0 -F <state>/journal.jsonl | grep --line-buffered '^{' | jq --unbuffered -rc '…'`
  over `~/.local/state/{issue,review,steward}-agent-xnmp-tauri-explorer/journal.jsonl`
  and `~/.local/state/issue-agent/spend.jsonl`. New event types to include in
  the formatter: `hold`, `recurrence`.
- Diagnostics tail pattern:
  `world build failed|sdk: (WALL|IDLE|transport died|stream ENDED|usage calibration)|sdk-stderr:`.
- Monitor deliveries can arrive minutes late and batched — trust journal `ts`,
  not arrival time.

## Gotchas that still apply (carried forward)

- `pgrep/pkill -f` with a literal pattern matches its own shell under the rtk
  hook — use a char-class breaker: `pgrep -f "issue_agent[.]app[.]main"`.
- gh label SEARCH is stale for closed PRs/issues (a day+); the object's own
  labels are truthful.
- Codex OAuth binds the browser's live ChatGPT session — verify via the
  id_token `email` claim in `~/.codex/auth.json`.
- Stream-vs-result usage skew is 3.4–6.4× (calibration pairs journaled as
  `usage_stream`); the abort estimator learns in stream units.
- The daily spend window is per UTC day (resets 10:00 local) — accepted design.
- Worktree/session forensics:
  `~/.claude/projects/-home-chong-Repos-tauri-explorer--claude-worktrees-issue-<n>/`.

## Still open on the harness

- **#1 webhook push** (`gh webhook forward`; demote polling to a slow
  reconciler) — the only remaining harness issue.
- `issue-agent init` doesn't offer glm (noted in 755dd41's commit message).
- `docs/open/issues/` — three incident write-ups, not re-validated.
- Evaluation: the CLAUDE.md Direction section defines the metric/guardrail
  pairs; today's baseline is ≈$17/merge claude-heavy. A `bin/metrics.sh` that
  prints the per-day scoreboard (cost/merge, cost mix by leg, probe hit rate,
  re-review skips, first-pass approval, escalations, recurrences) is unfiled —
  file it if the ad-hoc jq gets old.
