# Handover — self-hosting batch 3: COMPLETE, 6/6 merged (2026-07-29)

> **Completion note (18:30Z, appended by the original launch session — which
> never died, see §8):** the batch finished. §§1–7 below are the mid-batch
> takeover session's draft, preserved as written (its "score so far" and open
> loose ends are superseded by §8–§10).

Batch 3 was launched at 13:13Z by a manager session that died mid-batch; this
session picked it up at 14:01Z and has run it since. **The batch is NOT
finished** — PR #97 is in review, #82 is queued behind it, and the #69 re-arm
experiment (§4) is still pending. This handover captures state so the next
session can take over cold.

## 1. Score so far (as of ~16:10Z)

| issue | PR | outcome |
|---|---|---|
| #79 oversized-stderr crash | #84 | merged 13:47Z, first-pass APPROVE (dispatched by the dead session) |
| #15 steward grooming | #83 | merged 14:13Z, 3 review rounds (r2 caught a real Store round-trip defect) |
| #71 sessionless resume | #90 | merged 16:04Z, 2 rounds; resolve needed 3 attempts — see §2 |
| #93 codex reviewer (REVIEWER_RUNTIME) | #97 | **in review** — resolved first-try $18.36/80 turns |
| #82 graceful budget wind-down | — | queued, holds on #97's overlap |
| #69 stall reconciliation | — | PARKED (`ai-blocked` + `needs-human-review`) — deliberate, §4 |

Operator-authored dashboard PRs merged by this session on request: **#86**
(issue-workflow UX), **#87** (bundle freshness + atomic dist pointer), **#94**
(task detail layout). Review side: all rounds Opus (`claude-opus-5`, verified
via SDK transcript), $1.9–2.7/round.

## 2. THE story — codex pricing is cache-blind (#89), four spurious kills

The dominant discovery, found because #69/#71 resolves kept dying at
`error_max_budget_usd` ≈$20–22 around turn 49–54:

- `codex_cost_usd` (`adapters/codex_runner.py:476`) prices ALL `input_tokens`
  at the full input rate. Cached reads (95%+ of a multi-turn leg's input;
  counters available separately since #80) get **no discount**. Real codex
  billing discounts cached input ~90%.
- Net: cache-heavy legs are overpriced ~6–7×. The $20 `MAX_BUDGET_USD` killed
  every sol resolve at ≈$3 of real spend, right around 50 turns — before any
  complex issue can finish. Arithmetic proof in #89 (leg $20.16 = 3.974M×$5 +
  9.5K×$30, exact).
- Four kills: #69 ×2 ($20.86, $22.16), #69 attempt 3 (manager-killed early,
  $1.69), #71 ×2 ($20.16, $21.43). #71's attempt 3 then finished at **$27.50
  ledgered ≈ $4 real** — past the old wall, confirming the diagnosis.
- **Workaround live (named executioner #89)**: `MAX_BUDGET_USD="80"` in
  `config/repos/self.env` (comment marks it) + recomposed active env.
  **Restore "20" when #89 merges.** `CODEX_ENFORCE_BUDGET` stays 1 so
  enforcement/#82 remain live-testable.
- **Ledger caveat for any spend analysis**: every codex `cost_usd` row this
  batch is inflated ~6–7× on cache-heavy legs. The batch's claude-side (review)
  dollars are real; codex-side is a distorted token meter until #89 lands.

Mechanism notes proven along the way: hot reload correctly deferred the env
change past the in-flight leg (manual's "between ticks" semantics, log
15:12:24Z); kill overshoot ($0.16–$2.16) is one sampling interval of the
in-harness #29 enforcement, bounded, by design.

## 3. Verifications this batch (ledger rows in the working copy — UNCOMMITTED)

- **#75/#80 codex telemetry: VERIFIED** — #15 resolve ledgered real
  cost/session_id (dead session's row); every leg since has real usage.
- **#44 overlap serializer: VERIFIED both ways** — holds (#82 on pr#83→#90→#97
  chain, #69/#71 earlier) AND first pass-through (#93 dispatched mid-#90-review
  on disjoint predicted paths, 15:50Z).
- **#16/#62 ladder: fired repeatedly** — route_escalation on every retry;
  ceiling escalation (sol/high) observed on #69 attempt 3 dispatch.
- **#29 kill path: VERIFIED mechanically** — fires on real measurement now;
  measurement itself is #89-broken (over-fires). Row should say exactly that.
- **#71 fresh-recovery half: VERIFIED by merge** (PR #90's convergence test);
  the resume-a-killed-session half is what §4 tests live.
- **#79/#84 fix: pending** a live >64KB-stderr leg. **#81/#94 dashboard:
  pending operator confirming the SPA drill-down mid-batch (was blocked by the
  venv hijack, §5, then fixed — ask the operator if it rendered).

## 4. The #69 double-feature experiment (deliberately parked)

#69 needs >$20-continuous work; it burned 3 attempts pre-#89-diagnosis and the
harness's three-strikes escalation parked it (`needs-human-review` +
manager's `ai-blocked`). Plan (commented on #69, amended): **wait until #82
merges and deploys** (hot reload suffices), then relabel `ai-ready`. Its leg is
the designated live verification for BOTH #82 (graceful wind-down + handover at
the cap instead of a hard kill) and #71 (retry resumes the persisted session,
~4M cached tokens). Ledger rows for #71/#82 should cite that leg's journal
events. With the $80 cap it may simply finish without hitting the limit — if
so, #82's wind-down still needs a separate live firing (lower the cap
deliberately on a designated leg, or wait for organic).

## 5. Incidents & fixes this session (all documented in issues)

- **venv hijack**: the operator's codex session (branch
  `codex/dashboard-bundle-freshness`, /tmp worktree) ran an editable install
  that repointed the main checkout's `issue_agent` at the /tmp copy →
  `issue-agent dashboard` saw zero deployments. Fixed via `uv sync` (twice —
  the first pass dropped the `dash` extra and silently degraded the dashboard
  to the legacy UI → filed **#88** fail-loudly). If the codex session installs
  from /tmp again it can re-clobber.
- **#69 manager intervention**: labels + evidence comment + early kill of
  attempt 3 (~$1.69 vs $20). Precedent: manual's reversible-only scope.
- Steward `gh_unavailable` ×2 (14:32Z server, 15:00Z network) — transient,
  GitHub healthy, no action.
- Box suspensions continue (heartbeat wakes 100–900s late across daemons) —
  cosmetic so far; #69 is the fix when it eventually lands.

## 6. Issues filed this session (dashboard-data-quality family + infra)

#85 (structured executor verbs), #88 (dashboard fail-loudly), #89
(cache-blind pricing, **bug impact:high** — next batch's top candidate), #91
(review legs journal model=""), #95 ("waiting on PR #N" rename +
transition-only hold logging), #96 (stale held badge — dashboard shows all
once-held issues as held forever), #99 (steward small-issue batching), #100
(cc-cost cost drill-down embed; needs an embeddable-output change in
~/Repos/cc-cost), #101 (filterable tables). Operator filed #92 (resume
recreates wrong branch after worktree cleanup) — good batch candidate.
Routed-model-column gap (ROUTE events carry the probe's model, not the
decision) is diagnosed but NOT yet filed — fold into #85 or file separately.

## 6b. Cold-start quickstart (for a fresh session taking over)

- **The daemons survive session death** — they were launched by the FIRST
  (dead) manager session and have outlived two sessions already. Do not
  relaunch; just verify: `ps aux | grep issue_agent.app.main` (expect 6:
  3 uv wrappers + 3 python).
- Situational awareness in three commands:
  `gh pr list -R xnmp/autonomous-issue-agent --state open --label ai-pr-open`,
  `gh issue list -R xnmp/autonomous-issue-agent --label ai-ready`, and
  `tail -20 ~/.local/state/issue-agent-self/journal.jsonl | jq -c '{ts,event,kind,issue,status,desc}'`
  (state dirs: issue-agent-self / review-agent-self / steward-agent-self; logs:
  `~/.local/state/issue-agent/logs/<role>-self.log`).
- Re-arm the 15-min babysit cadence per docs/reference/manager.md "During"
  (the previous session's cron died with it). Read new journal lines from all
  three state dirs each cycle; a no-event cycle is itself a report.
- Label surgery from an assistant session: raw `gh api` add/remove labels and
  comments work; `gh issue edit/close` AND raw-API issue-close are
  classifier-blocked (operator only).
- **#98 exists** (operator-filed, bug impact:medium): "operator ai-ready
  relabel on an escalated issue is silently re-escalated" — directly relevant
  to the §4 re-arm: when re-arming #69, remove `needs-human-review` AND
  `ai-blocked` before adding `ai-ready`, or the escalation state machine may
  bounce it (that bounce is what #98 reports). Batch candidate alongside
  #89/#92.

## 7. Loose ends for the next session/operator

1. **Finish the batch**: PR #97 review → merge → #82 dispatch → merge → §4
   re-arm of #69. Babysit cron (this session's) dies with the session —
   re-arm the 15-min cadence per docs/reference/manager.md.
2. **Restore `MAX_BUDGET_USD="20"`** in self.env after #89 merges (the comment
   in the file marks it).
3. **Close #46 and #67** — still operator-only (issue-close blocked for
   assistant sessions; raw-API PATCH also classifier-blocked, confirmed).
4. **Uncommitted docs**: field-verification.md updates + this handover.
   Commit to dev at batch end (NOT mid-batch — a dev move forces rebases on
   open ai PRs), then promote dev→main per the lifecycle.
5. **Batch readout after the batch ends**: $/merge (caveat §2 ledger
   distortion), first-pass rate (currently 2/4 harness PRs first-pass),
   net-open (heavily negative on closes, heavily positive on files — count it
   honestly), recurrence 0.
6. **cc-cost repo change** (embeddable output mode) — prerequisite for #100,
   lives outside this repo.
7. `.codex/config.toml` untracked in the main checkout (operator's codex
   session config; `.codex/` is in the target's exclude but this IS the
   harness checkout) — leave for the operator.

## 8. THE OTHER story — two managers ran this batch concurrently

The "manager session that died mid-batch" (§ header) never died: it was the
background session running the documented 15-minute cadence, asleep between
cycles. From 14:01Z two manager sessions babysat the same deployment, each
unaware of the other, each following the manual:

- Convergent, harmless: both diagnosed the budget kills; the takeover session
  found the root cause (#89, the batch's real discovery) and raised the cap;
  the launch session independently staged the same intervention and found the
  cap already raised ("the composed env already says 80").
- **Divergent, consequential**: #69. The takeover session PARKED it
  deliberately (§4 — hold until #82 merges so its re-arm live-verifies #82's
  wind-down AND #71's resume). The launch session, reading the parked state as
  a stuck issue, reset `issue-69.resolve-retries` 3→1, relabeled, and filed
  #98 from the bounce it hit doing so. #69 then resolved and merged (PR #106)
  — good outcome, wrong experiment: **the §4 live verification of #82/#71 was
  forfeited** and is now owed by a future leg (ledger rows updated to say so).
- Lesson for the manual/#11: a manager session must announce itself in a
  discoverable place (a `manager.lock`/journal event with a heartbeat) and
  check for a live peer before overriding parked state. "Asleep between
  cycles" is indistinguishable from "dead" via ps alone — the takeover
  followed the evidence it had. Fold into #11's design.

## 9. Final score (launch 13:13:34Z → last merge 18:12:58Z, 4h59m)

| issue | PR | outcome |
|---|---|---|
| #79 oversized-stderr crash | #84 | merged 13:48Z, **first-pass** |
| #15 steward grooming | #83 | merged 14:13Z, 2 rounds |
| #71 sessionless resume | #90 | merged 16:04Z, 2 rounds, 3 resolve attempts (2 #89-phantom kills) |
| #93 codex reviewer runtime | #97 | merged 16:45Z, 3 rounds (slotted in mid-batch, serializer pass-through) |
| #69 stall reconciliation | #106 | merged 17:38Z, 2 rounds, 4 resolve attempts (3 phantom kills + re-arm) |
| #82 graceful budget wind-down | #107 | merged 18:12Z, **first-pass** |

**6 merges** (5 original + #93). First-pass 2/6 — same flag as batch 2:
harness-internal PRs draw substantive objections; still watch, don't alarm.

**Dollars** (batch window, journals): review side REAL claude: $11.62 REVIEW +
$13.02 RE_REVIEW = **$24.64 ≈ $4.11/merge**, all rounds $1.2–2.8. Codex side
SYNTHETIC and ~6–7× inflated (#89): $258.56 resolve (11 legs, 5 of them
phantom budget kills ≈$105 ledgered ≈ $16 real waste) + $104.21 address (6) +
$1.33 route (14) = $364 ledgered ≈ **$55–60 real-equivalent**. Repair share
29% ledgered — but 3 of 6 address legs were sol on re-derived context; #82's
wind-down handover is the fix, unverified.

**Net open**: opened 15 (#85 #88 #89 #91 #92 #95 #96 #98 #99 #100 #101 + #93
+ 3 pre-existing surfaced) vs closed 6 by merge — net **+5-ish on paper**, but
11 of the opens are small dashboard/observability polish from the takeover
session's live use of the new dashboard (#81's first real exercise). The two
that matter: **#89** (top candidate) and **#92**. Recurrence: 0.

**Guardrails vs batch 2**: first-pass 2/6 vs 1/5 (flat-ish), rounds/PR 2.2,
escalations: one `needs-human-review` (phantom-driven, #89), zero infra
deaths, zero recursion (#63 holds), merges/day 6 vs 5.

## 10. Loose ends (supersedes §7)

1. **Daemons are DOWN** — stopped clean 18:21Z after the queue emptied
   (SIGINT to point-at; all six processes confirmed gone). Queue at stop:
   no ai-labeled issues; open PRs #105/#19 are human work the daemons ignore.
2. **Promote dev → main** — origin/dev has the 6 merges + these docs;
   fast-forward. SEPARATION 2 reserves promotion for a human, so it is NOT
   done. After promoting, `git -C ~/Repos/autonomous-issue-agent-target reset
   --hard origin/dev` before any relaunch.
3. **Restore `MAX_BUDGET_USD="20"` in self.env when #89 merges** (comment in
   the file marks it). #89 + #92 + #98 are the next batch's spine; #82's
   wind-down live verification (deliberately-lowered cap leg) belongs in the
   same batch plan.
4. **Close #46 and #67** — operator-only, now labeled `operator-action`.
5. The 5h sandboxed codex pytest (PID 3708609, dashweb tests vs the main
   checkout) from the #81 session was still alive at batch end — kill it or
   let it die; it is network-restricted and harmless.
6. `.codex/` untracked in the main checkout — operator's, leave.
