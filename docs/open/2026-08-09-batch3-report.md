# Batch 3 (2026-08-09, steward-selected) — gaps, fixes, operator actions

FINAL (wrapped 2026-08-09 ~10:20Z on operator direction; remaining triage
queue left to the daemons). Supervisor: Fable session (goal: supervise to
drain, fix gaps on the spot subtractively, report every operator action with
whether it genuinely required human judgment).

## Scoreboard

- **11 merges**: #620/#482 (watcher backoff, the 3-blind-FIX_CI saga),
  #627/#493, #629/#512, #630/#546 (P-high), #628/#503, #632/#624, #631/#592,
  #633/#525 (PR thread view), #640/#636 (dependabot grouping), #639/#635
  (user-report, steward re-triaged off the creator's reply), #637/#527
  (git-graph mute base-merges).
- **1 parked**: #634/#457 (Windows smoke-hang diagnosis) — approved on the
  merits every round but wedged twice by the downgrade-copy livelock (gap 7);
  parked at wrap rather than buying more rounds. Both root causes are fixed
  (79e088a, 5045a44); the PR is human-handoff with the diagnosis in-branch.
- **Cost: $77.03 over 80 paid legs → $7.00/merged PR** (configured rate card,
  events-v2 spend records from batch start 2026-08-08T22:30Z). Top costs:
  #482 $25.00 (three blind FIX_CI legs before the flaky-spec diagnosis —
  the incident that produced the FIX_CI failure-history fix), #503 $10.28,
  #457 $8.91 (the livelock), #527 $4.91. First sol-default review leg: $0.49.
- Steward autonomy (`STEWARD_TRIAGE=1`/`STEWARD_PROMOTE=1`) selected the whole
  batch; triage cost ~$0.51/issue; two triage-blocked escalations to humans
  (#526 creator questions, #571 owner questions — `STEWARD_OWNER` unset).
- Zero review/fix collisions (`c4895ab` VERIFIED), zero submit-head-changed
  failures; free-CI-rerun VERIFIED on both branches (real-failure PR #574
  earlier; flake-save PR #639 this batch — green re-run, no paid leg,
  auto-merge same tick).

## Harness gaps found and fixed on the spot (all committed to dev, tested)

1. **#185 — contradictory ai-ready+ai-pr-open never repaired** (`23fd840`).
   Reconciler arm removes the stale ai-ready when an open PR is confirmed
   in-snapshot; liveness folds a sole-label ai-pr-open issue's PR signature
   into its stall clock. Adversarial review found 8 issues pre-commit; the
   three that mattered: linkage had to go through shared `world.issue_for_pr`
   (closing-ref is default-branch-only — the fix would have been a silent
   no-op on the self-hosting repo), repaired issues must join `guarded`
   (same-tick repair+RESOLVE claim race), and the repair must preempt the
   dup-PR guard (else it mints a NEW contradictory pair).
2. **Exemption scope bound 3→7 with two-party per-file grants** (`3793b35`,
   pre-compaction round). Live-verified same night by PR #625 (4 test files,
   4 grants, merged). Cap invariant (per-rule caps sum ≤ total) prevents the
   9-declaration truncation wedge; shell twin got total-cap parity.
3. **#604 class — fixer re-ask loop** (three root causes, three fixes):
   block taxonomy in plugin/AGENT.md (never block for PR-reviewable code
   decisions); `GATE_TEST_FAILURE_EXIT_CODES` baked into gov.sh (`6058332`,
   split-brain between doctor-validated daemon env and the codex sandbox);
   blocked resume forfeits its poisoned session (`53d8f9a`). #604 shipped as
   PR #626 the same night.
4. **Reply framing widens no authority** (`01c5e78`): the resume prompt tells
   the fixer the operator reply answers its question and to re-evaluate — as
   information, not as a grant channel (the unauthenticated-comment surface
   stays unauthenticated; the authenticated mint is #184).
5. **Dashboard extras pruned by any exact sync** (`c400657`): install.sh syncs
   `--extra dash` and `make dashboard` pins extras at launch. Two silent
   degradations to the v1 snapshot tonight motivated it.
6. **Dashboard rounds 1+2** (merges `293bf1c`, round-2 merge): per-leg detail
   (model/effort/session/exit_reason), review-verdict excerpts (escaped,
   untrusted-chipped, 500-char server bound), durable unproductive-rounds
   counter (the inferred numerator was WRONG against the real breaker — an
   operator could read 0/5 one tick before escalation), single-flight gh
   cache, force-push attribution fix, and the operator-requested lifecycle
   stepper with circle-back retry counts.

7. **#634 downgrade-copy livelock** (`79e088a`). The reviewer APPROVEd on the
   merits but omitted the `required-test *` grant line; the harness downgrade
   to REQUEST_CHANGES (c637501, first live firing — ledger row now VERIFIED)
   worked as designed, but the patch-id re-review shortcut then copied that
   downgrade onto every mechanical rebase. The downgrade's remedy is
   reviewer-side only (emit the grant), so the copy denied the one act that
   could clear it: unchanged patch → rejection ceiling → needs-human-review,
   twice, each needing an operator unblock. Class fix: the downgrade body
   carries a round-trip-stable marker and the copy shortcut refuses to copy a
   marker-bearing CHANGES_REQUESTED, reviewing normally instead. Ordinary
   rejections still copy free.
8. **#457 VOID exemption self-diagnosis** (`3609a99`). A branch accumulated
   `required-test *` AND `required-test instrumentation-only` (2 subjects >
   cap 1 → declaration VOID), but the denial said "add the trailer" it
   already had — the fixer blocked blaming the harness. Both gate twins now
   name the void (subject count, subjects, "exactly one") and the remedy
   (reword/drop the offending commit message).
9. **FIX_CI failure history** (merge `68c6c16`+`b8650cb`): pure
   `domain/ci_history.py`, last-5-attempt store, fenced planner section with
   flaky-test-signature guidance, recorded post-leg, 4 reset sites. Closes
   the top "not yet fixed" candidate below.
10. **#163 GraphQL diet** (merge `03f1d2a`): terminal PRViews from the list
    payload, `fix_probe_safe` REST head-signals probe (CI+mergeability, ≤4
    heads), 71→33 pts/tick; fixer holds TTL floor during CI waits.
11. **Reviewer defaults raised to the fixer tier** (`5178298`, `9c0e43e`):
    review model gpt-5.6-sol, review budget $15, triage budget $2 — now code
    defaults, not per-repo overlay knowledge.

## Gaps observed, not yet fixed (candidates, post-batch)

- **Stall watchdog vs. hold reporter don't compose**: a reported overlap hold
  (git-graph cluster serialization) still climbs stall levels. Fold hold
  state into the stall signature (same family as the #185 liveness fix).
- **GitHub secondary rate limits** (#163): the fixer-side diet shipped
  (`03f1d2a`, item 10 above); the steward's GraphQL reduction (item 4 of
  #163) remains open. Verify the live pts/tick drop at drain.
- **Steward triage label-removal 404s log as errors** (benign but noisy —
  add-before-remove landed, DELETE hit the already-absent label).
- **`STEWARD_OWNER` unset** → owner-level triage questions tagged generically
  and #571 blocked. Either set it in the overlay or default it to the repo
  owner.

## Operator actions this session (each triaged)

| Action | Human judgment needed? | Automation path |
|---|---|---|
| #620 flaky-spec diagnosis + direction comment | Judgment call tonight, but MECHANICAL in principle | failure-history in FIX_CI prompt (above) |
| #620 unblock ceremony: reply + relabel + hand-reset `issue-482.ci-retries` | No | operator reply on an escalated PR should re-arm one bounded round |
| #482 stale ai-ready hand-removal | No | fixed tonight (#185, `23fd840`) |
| STEWARD_TRIAGE/PROMOTE enable + env recompose | Yes (posture change, user-directed) | n/a — but see next row |
| Daemon restart for env change | NO — and it was WRONG | env hot-applies on reload; my restart script's inverted drain check killed two live legs mid-flight (bounded loss, boot recovery reconciled). Document: never restart for env; fix compose docs |
| Dashboard publish (build + extras + server restart ×3) | No | `make dashboard` (`c400657`) + publish gap noted; bundle still pins at boot |
| Facelift worktree launcher clobber (`make install` from worktree) + venv extras restore ×2 | No | `c400657` removes the recurring half; worktree agents already instructed not to run install |
| Closing stale harness-backlog issues (#591, #166, #156, #131) | No | steward grooming now on for self-repo? (recursive grooming gap) |
| Codex facelift question round-trip (posture answer) | No | pre-answer postures in briefs; non-interactive directive |
| #634 unblock ×2 (dashboard API, second with reviewer-directed guidance) | No | root cause was the downgrade-copy livelock, fixed `79e088a`; with the fix the first downgrade triggers a fresh reviewer run that can grant |
| #457/#634 park at wrap (dashboard API) | Yes (operator chose to stop buying rounds) | the park lever worked as designed; nothing to automate |
| Dashboard restart to pick up 5045a44 | No | dashboard has no hot-reload; publish-pipeline gap already on the residual list |

## Verification ledger

Updated in `docs/field-verification.md` (`69d1c34`, `8b855c0`, `1414f11`):
5d069b4 and c4895ab VERIFIED; c637501 upgraded PARTIAL→VERIFIED (downgrade
branch fired live, PR #634 r8); #38 CI_FREE_RERUN upgraded to VERIFIED (flake
branch, PR #639); 01c5e78 PARTIAL; new UNVERIFIED rows for 79e088a and the
batch commits. Verify next deployment: 79e088a (no-copy carve-out on a real
downgraded rebase), 5045a44 (`operator_rearm_baseline` event on an unblock),
FIX_CI failure-history fenced section in a FIX_CI prompt, #163 pts/tick drop.

## Cost

Covered in the scoreboard: $77.03 / 80 legs / 11 merges = $7.00 per merged PR.
The efficiency story of the batch is concentrated in two numbers: #482's $25
(three blind FIX_CI legs before a human diagnosis — now automated as FIX_CI
failure history) and PR #639's $0 flake save. Reviewer tier moved luna→sol
mid-batch with first observed sol re-review at $0.49; watch the review cost
mix next deployment for the sol-tier delta against the first-pass-approval
guardrail.
