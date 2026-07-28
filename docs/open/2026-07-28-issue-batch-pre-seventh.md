# Issue batch — 2026-07-28, before the seventh deployment

Eleven issues fixed and auto-closed on push (`21de6ac`→`e5f5441`): #27 #28 #30
#32 #34 #35 #38 #39 #40 #43 #45. Partials, commented and left open: #29
(honest reporting only; mid-run codex abort postponed), #46 (capability boot
log only; steward-skip postponed). Postponed deliberately: #31 (rewrites
merge-gate semantics on keifu-only evidence), #44 (needs prediction scoring).
New: #47 (Android dashboard companion, blocked by #37), #48 (gh error reads as
"no checks" → auto-merge can arm unverified — found by this batch's #30 pass).

Process: four parallel worktree agents (independent seams), then two on the
`domain/fix.py` cluster, adversarial review on #28. The review earned its cost:
the as-filed #28 fix would NOT have healed the existing backlog (merged PRs had
already lost the PR-side `ai-pr-open`; the `gh pr list --label` hits were stale
search index). Landed instead: widened issue queries PLUS an issue-driven
cleanup arm that is also the transient-failure retry loop.

## Operator notes for the next babysit (tauri-explorer)

1. **Cap semantics changed.** `DAILY_SPEND_CAP_USD` is now per-repo;
   `GLOBAL_DAILY_SPEND_CAP_USD` (shared.env, 0=off) bounds the box. The tauri
   overlay still has `DAILY_SPEND_CAP_USD=0` from the sixth night — set it to a
   real per-repo number (700 was the old pooled value) and decide whether to arm
   the global cap. While capped, daemons now idle silently (tick gate) instead
   of thrashing labels.
2. **First tick self-heal.** On the first full world build the fix daemon
   should repair ~23 stuck closed+`ai-pr-open` issues in one tick: 23×
   relabel→`ai-done`, 23× RemoveWorktree (~2.3 GB reclaimed), 23×
   ClearIssueState (clears leaked `resolve-retries` that were pinning routing
   escalation). Verify labels moved and worktrees are gone; the recurrence
   guardrail (#10) is live for the first time after this.
3. **New behaviors to watch, all default-on:** `DUPLICATE_PR_GUARD` (ai-ready
   issue with a third-party closing PR → comment + `ai-blocked`, no resolve),
   `CI_FREE_RERUN` (one free `gh run rerun --failed` per head SHA before a paid
   FIX_CI — this is the #38 lever; the tauri relay-stub flake is the test case),
   `CI_PENDING_MAX_SECONDS=3600` (stuck-pending PR → `needs-human-review`).
   Escalation-rate is the guardrail for the last one — a healthy long run
   escalating means the default is too tight.
4. **Codex measurement:** populate `CODEX_COST_PER_MTOK` with per-model rates
   (luna/terra/sol) or the ledger keeps pricing everything at the scalar
   fallback; router legs now journal `kind=ROUTE`, so RESOLVE cost mixes are
   clean. `agent_run.model` now records what actually ran.
5. **Running daemons pick this up via hot reload between ticks** — but the
   sixth-night tauri launcher predates the per-name env change; a full restart
   (`point-at`) is cleaner and re-runs the upgraded doctor (new checks: adopt+
   auto-merge vs third-party PRs — dependabot will trip it until
   `REVIEW_ADOPT_UNLABELED=0` is set or the queue is clean — and the CI-matrix
   platform warning).
