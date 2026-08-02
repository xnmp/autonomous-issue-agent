# Handover: keifu batch 2 MID-BATCH stop — daemons will die with the session

Date: 2026-08-02 (~01:30Z / 11:30 local). Predecessor:
`2026-08-02-handover-nixos-bootstrap-wizard.md`. Operator requested stop
mid-batch. **The daemons run as the assistant session's background task and
receive SIGTERM at session end** (clean drain by design; boot recovery
reclaims). The web dashboard (127.0.0.1:8321) and all monitors die too.

## TL;DR for the next session

1. RESOLVED before session end: the two subagent fixes (#158 dashboard
   mapping, #156 installer shims + drain ownership) came back green on the
   full suite and were committed as `fb7f326`, explicitly FLAGGED PENDING
   ADVERSARIAL REVIEW — run that review pass before building on them.
2. Restart: `cd ~/Repos/keifu && issue-agent start` (foreground, session-tied)
   — or better, `issue-agent install-service` to decouple daemons from
   assistant sessions (the handbook's recommendation; tonight proved why).
3. **Daily spend cap is the binding constraint**: $28.34 settled today (UTC)
   of $50, and every fix-role leg reserves MAX_BUDGET_USD=$20 — with one leg
   in flight, a second admission is rejected (observed on #18, 01:24-01:25Z).
   Either raise DAILY_SPEND_CAP_USD in `config/repos/keifu.env` (+
   `bash bin/compose-env.sh keifu`) or accept the batch pausing until UTC
   midnight (10:00 local).
4. #18/#174 likely need the #155-class unstick after the rejections above
   saturate its breaker again:
   `printf '<review-ts> 0' > ~/.local/state/issue-agent/xnmp__keifu/issue-18.review-retries`
   + `gh api -X DELETE repos/xnmp/keifu/issues/174/labels/needs-human-review`
   (read the file first for the current review timestamp). Same procedure
   was applied to #16/#171 tonight (label removed; **the issue-16
   counter reset is STILL PENDING** — operator was asked to run
   `printf '2026-08-02T00:17:05Z 0' > ~/.../issue-16.review-retries`).

## Batch state (xnmp/keifu, base chong-dev)

- Open ai PRs: **#171** (issue 16, geometry refresh; r1 REQUEST_CHANGES;
  needs-human-review label REMOVED tonight, counter reset pending → once
  reset, address-review re-dispatches), **#173** (issue 25, interactive
  rebase TUI, $11.94 resolve; r1 REQUEST_CHANGES; address-review leg was IN
  FLIGHT at handover — escalated medium→high effort on sol at 01:26Z; will
  be killed by shutdown, recovery re-dispatches), **#174** (issue 18,
  startup-picker timeout, via the RESUME path; r1 REQUEST_CHANGES;
  address-review admission-rejected on the daily cap, breaker likely
  saturating).
- ai-ready queue (no PR yet): #143, #159, #161, #162 (+ #18/#25 carry a
  spurious extra ai-ready — see relabel race below; harmless to leave, the
  serializer defers on their open PRs).
- needs-clarification (blocked on OPERATOR ANSWERS — they're addressed to
  @xnmp as issue author): #34, #124, #125, #126, #130, #132, #136, #142,
  #157 (+ new #172 filed by operator tonight, also questioned). Recommended
  defaults per issue: `docs/open/2026-08-02-keifu-decision-sheet.md`
  (committed alongside this handover). Steward re-triages on any issue edit.
- #163/#164 were swept ~00:48Z — verify their verdicts landed (labels/
  comments) before assuming triaged.
- Spend: $35.85 total across both UTC days; today $28.34. Cost mix:
  RESOLVE $19.63/3, TRIAGE $9.43/38, ADDRESS_REVIEW $5.07/6, REVIEW $0.62/3,
  ROUTE $0.58/3, RESUME $0.52/1. First-pass approval 0/3 (watch this
  guardrail — codex reviewer gpt-5.6-terra is strict, rounds/PR may climb).

## Landed tonight (all on dev, tested + adversarially reviewed)

- `7c86859` STEWARD_RUNTIME (codex steward; role-keyed read-only governor
  profiles; strict-schema nullable widening; journal_paths read-side parity).
- `e47a3fe` ADR-0012 ceilings: EFFECT_RETRY_CEILING (refusals-counted,
  control store v18), STEWARD_ERROR_CEILING, doctor Issues:Write probe,
  harness_signal revision-guard exemption for escalation writes.
- `08219fb` + `1bf7e78` doctor REST identity read, rate-limit-honest
  message, classic-PAT-for-cross-account advice.
- `ac7e8b1` field-verification ledger rows (several VERIFIED live same day).
- Config: STEWARD_RUNTIME=codex (shared.env), classic PAT (REVIEW_BOT_TOKEN),
  ISSUE_SPEND_CAP_USD=35 + ROUTER_BUDGET_USD=0.50 (keifu overlay), operator
  twin-shim `~/.local/bin/issue-agent-publish`.

## Open defects with root cause IN HAND (fix next session)

1. **Relabel race (ai-ready + ai-pr-open coexist; observed #18, #25)** —
   fully diagnosed, evidence in journal. `reap_actions`
   (domain/fix.py:726-733) re-queues a just-finished issue because
   `decide()` samples `reservations.snapshot()` AFTER the non-atomic
   `build_world()` (roles.py ~2197-2230): the leg's reservation releases
   mid-fetch, the stale world still shows ai-working/no-PR → orphan shape.
   The revision guard can't catch it because `_command` MINTS
   expected_revision from a live read for None-revision relabels
   (self-fulfilling CAS, effects.py:823-829/876-892). Fix direction (both):
   (a) snapshot reservations BEFORE build_world — surgical root-cause fix,
   fail-toward-busy; (b) make Relabel verification check label membership
   (remove-label still present), not updated_at equality. Add a behavior
   test reproducing the interleaving.
2. **#155 admission-rejection → wrong escalation surface** (issue updated
   with correction: the 3-strike breaker DOES bound it; remaining work =
   route deterministic admission BUDGET_EXHAUSTED to budget_actions/
   needs-budget-review naming the cap arithmetic, and auto-re-arm after a
   cap change — the #98 class on the PR path). Tonight's daily-cap
   rejections on #18 are the same class.
3. **#158 dashboard run-failed mapping** and **#156 installer shim + drain
   publish-kind noise** — implementations possibly already in tree/stash
   (see TL;DR item 1).
4. **#157 legacy dashboard retirement + seamless build** — operator-requested
   cleanup, not a defect.

## Operator environment notes

- Autocompact mystery SOLVED: settings.json `env` block only reaches tool
  subprocesses, never the CLI's own process (anthropics/claude-code#63186;
  confirmed by decompiling v2.1.220 — override caps at min(pct×window,
  window−13k)). Fix: `export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=40` in zshrc
  before launch. ~/.claude/settings.json was hand-repaired tonight (a
  permissions paste had made it invalid JSON since ~01:10Z; backup at
  settings.json.bak-20260802). Permission rules likely need a fresh session.
- The auto-mode classifier blocks daemon-state-file writes and some gh
  label ops even with allow rules present pre-repair; re-test after restart.
- Report notes for the FINAL report (structural-vs-incidental analysis, per
  the session goal) accumulated at
  `/tmp/claude-1000/-home-chong-Repos-autonomous-issue-agent/e5a2b01d-3170-4c21-b409-f6f52fef32ac/scratchpad/report-notes.md`
  — /tmp survives session end but not reboot; the essential findings are
  mirrored in this handover, the field-verification ledger, and issues
  #155-#158. **The final report is still owed** when the backlog drains.

## Ledger verdicts to tick when evidence lands

- "#71 sessionless blocked-reply recovery" → **VERIFIED 2026-08-02**: #18
  ai-blocked + fixer-identity reply 01:04Z → RESUME leg $0.52 → PR #174.
  (Row lives in docs/field-verification.md from batch 3; tick it.)
- EFFECT_RETRY_CEILING / STEWARD_ERROR_CEILING / harness_signal rows:
  still pending live triggers.
