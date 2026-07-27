# Session record — third deployment day (xnmp/tauri-explorer), 2026-07-27

Follows [2026-07-26-second-deployment-session.md](2026-07-26-second-deployment-session.md).
Goal: **merge 5 more issues, fix harness defects along the way, then judge
whether the red/green protocol is too strict.** Goal met — 12 lifetime merges.

## Outcomes

| Issue | PR | Runtime | Result |
|---|---|---|---|
| #517 fuzzy filter in SCM sidebar | #539 | claude | **MERGED** 00:43 |
| #530 Ctrl+Up/Down branch-line jump | #540 | claude | **MERGED** 01:35 |
| #515 bulk-hide remote-only branches | #541 | claude (+codex rebase/fix-ci) | **MERGED** 02:41 |
| #522 pin checked-out branch lane | #542 | **codex/terra** (7.0 min resolve) | **MERGED** 03:29 |
| #524 detached-HEAD indicator | #543 | claude (codex blocked on quota) | **MERGED** 04:39 |

Ledgered spend ≈ $86 (4 claude resolves $13–20 each dominate; reviews $1.5–3;
codex runs bill $0 — subscription blindspot as designed/warned). Post-#542,
merges happen SERVER-SIDE via arm-at-creation + branch protection.

## Harness changes landed (all committed, suite green, verified live)

| Commit | Change |
|---|---|
| `43d8df2` | Estimator units fix: rate learned in stream units; calibration pairs journaled (`usage_stream`). Live pairs today: 3.4–6.4× skew — a constant would have been wrong. |
| `bcd7104` | World probe: review/steward quiet ticks cost 1 REST point, not ~14 GraphQL (the 23:37 rate-limit exhaustion). Backlog #1 filed for webhook push. |
| `688451d` | Arm-on-open behind a boot-time protection probe; operator enabled `dev` protection (6 required checks + 1 review). Verified: #542/#543 armed at creation, merged by GitHub with no daemon on the path. |
| `01204e6` | Codex OAuth mode (blank base_url = `codex login` credentials; no `-m ""`). |
| `2a65d1c` | Runtime-aware router tiers (sol/terra/luna beside opus/sonnet/haiku); router task runs on the active runtime's cheapest tier. |
| `663b416` | Runtime-tagged sessions — the cross-runtime resume bug (codex handed a Claude uuid, 0.3s "no rollout found", burned the rebase cap, false-escalated #541). Codex stderr tail now logged on nonzero exit. |
| `5be684a` | Scoped fix-verifier replaces full re-review after adversarial FAIL; PR-body gate denial teaches the full skeleton (both #517/#530 bounced twice on it). |
| `1a3d46f` | Cheap-fetch non-OPEN PRs: gh's label search serves stale results for closed PRs (9 merged PRs still listed a day later), each paying hang-class mergeable + checks calls every tick. |
| `502d2f0`+`8482130` | Hot reload: daemons restart BETWEEN ticks (never mid-session) on a **HEAD commit** change (commit-gated after the mtime version live-demonstrated the partial-load hazard by crashing two daemons on a half-edited tree) + the composed env file (runtime flips now land without manual restarts — used twice in anger today). |
| `8a77806` | point-at crash-loops slowly after a post-reload boot crash (self-heals on the fixing commit) instead of leaving dead roles inside a live point-at. |

## Codex account saga (operator-side, resolved)

The first `codex login` OAuth captured the browser's live ChatGPT session —
a free-tier account (pooky472@hotmail). Symptoms, in order: `gpt-5.6-sol`
400 "not supported with a ChatGPT account", catalog missing sol, then the
free monthly usage limit exhausted mid-resolve (reset Aug 26) which killed
RESOLVE #524 twice ($0, clean 429 window-aligned backoff → infra death).
Diagnosis: decode the id_token email claim in `~/.codex/auth.json`.
Re-login on the subscribed account (chonw89@gmail): sol listed, sol answers.
Fixer now runs codex + `CODEX_MODEL=gpt-5.6-sol`.

## Is the red/green protocol too strict? (the session's question)

**No — measured, the TDD ceremony costs ~5 min of a ~40-min session** (test
runs 2–3 min across 13–17 invocations; markers/hooks/screenshots ~1–2 min),
and it is what lets the reviewer approve in one round and auto-merge act on
that approval. **The real cost was the verification loop** (~20 min/issue:
two full adversarial rounds + fix cycles) — and it caught real defects in
BOTH transcript-analyzed sessions (incl. a regression introduced by the
round-1 fixes). Response shipped: scoped fix-verifier (verify the fixes, not
re-review the world) + the PR-body-skeleton denial. First data point under
the new protocol (#524): 40 min vs 47 for the comparable #515. The codex
comparison (#522: 7 min, one clean review round) suggests implementer speed,
not protocol weight, dominates wall time.

## Still open / carried forward

- Review runs journal `status=""` (empty structured status) on clean APPROVEs
  — cosmetic but makes verdicts grep-hostile; not yet chased.
- `pick_ready_issue` holds are silent — journal a deduped `hold` event
  (path-overlap / blocker) so a deliberate hold is distinguishable from an
  idle bug without code archaeology (cost ~30 min of it today).
- Steward hit intermittent GitHub GraphQL 502s (~5 over an hour) — clean
  fail-closed skips each time; no action needed unless chronic.
- Backlog #1 (webhook push) trigger unchanged: rate limiting recurring
  despite the probe.
- Estimator: 4 calibration pairs collected (3.4–6.4×); abort estimates now
  learned in matching units — watch the first real abort estimate.
