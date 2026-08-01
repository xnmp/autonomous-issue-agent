# Keifu supervised run — findings report (2026-08-01)

Scope: the second keifu supervision session (~05:58Z relaunch → ~11:0xZ
teardown), continuing the maiden-voyage session recorded in
`2026-08-01-handover-voyage-keifu-live.md`. Goal: supervise the harness
through the batch, iron out teething issues at the class level, keep the
dashboard truthful, report findings.

## Outcome

**6/6 merged. Batch complete.** Issues #155, #158, #156, #39, #160 resolved
end-to-end by the harness (PRs #166, #167, #170, #165, #169), plus the
operator-authored lockfile PR #168 which the harness reviewed and merged
itself. Zero recurrences of anything fixed. Teardown clean: 0 running runs,
0 claims, spend ledger == dashboard to the cent, no orphan processes. Three
inert `retryable_failure` update-branch commands remain for merged PRs (the
documented async-verification false negative); next boot's drain reconciles
them.

## Primary metrics (schema-v2 events + control-store charges)

- **Cost per merged PR: $5.06** ($30.36 keifu spend / 6 merges; $18.44 of
  it after the 05:58 relaunch). Compare $17.15 (first keifu deployment) and
  $53.69 (eighth sol deployment).
- **Cost mix since relaunch** (53 settled legs): resolve-side $12.20
  (RESUME $7.34 + RESOLVE $3.13 + PR_RESUME $1.69), review-side $4.39
  (RE_REVIEW ×20 at ~$0.20 each) + $0.44 initial reviews, repair-side $1.85
  (ADDRESS_REVIEW $1.11, FIX_CI $0.48, REBASE $0.26), routing $0.04.
- **Review economics**: single-mode codex reviews averaged **$0.13–0.25 per
  round** in pinned checkouts; the patch-id skip carried one verdict forward
  free (#166 post-refresh); one probe rerun avoided a paid FIX_CI.
- **First-pass approval**: 2/6 (#168 r1, #166 carried-approval). #167
  needed 6 rounds, #169 eleven (see defects — most rounds were harness- or
  CI-artifact-driven, not code quality), #165's 4 pre-dismissal rounds were
  3 phantom + 1 real.
- **Wall time**: ~4h40m relaunch→final merge, dominated by two Windows CI
  hangs (~35 min each), the CI-breaker escalation, and blocked-on-operator
  windows.
- **Dashboard truthfulness**: verified at every 10-minute reconciliation
  tick, including through teardown/relaunch (the stale pre-teardown
  `inflight=5` was corrected by boot recovery's abandoned-run corrections,
  observed live).

## Harness defects found live, fixed at class level (all with regression tests)

1. **Pinned review checkouts hardened** (`c0bd485`; two adversarial
   rounds). The 0d2e295 FAIL set: a plain dir squatting on the worktree
   path could route `reset --hard` into the operator's live checkout (H1,
   incl. the symlink-to-parent variant); registered-but-missing worktrees
   deferred forever (H2); pin deferrals silently burned escalation rounds
   (H3 — now journalled `review_pin_deferred`, free for 5 consecutive, then
   the bound resumes); pinning was inert for Claude reviewers (M1); empty
   head SHAs reviewed the live checkout (M2). Validated live the same hour:
   #168's review ran in `review-pr-168` and merged.
2. **Advisory-UNSTABLE merge deadlock** (`3c47bf8`; FAIL→PASS adversarial).
   A failing check wins `mergeStateStatus` over BEHIND, so an APPROVED PR
   whose only failures are advisory never refreshed after the base gained
   the fix — permanent silent deadlock (#166 observed). New update-branch
   arm bounded once per (head, base tip); `WorldState.base_head` observes
   the ref directly because **GraphQL `baseRefOid` is a stale anchor**
   (verified live — reviewer's counter-proposal refuted empirically).
   Validated end-to-end: #166 refreshed 06:40, merged 06:44.
3. **`.harness/` gate poisoning** (`8208c39` + keifu `539adaf` + `954ccef`).
   PR #167 committed its issue-scoped `test-cmd`; merged, every downstream
   worktree's trusted red-fail gate ran issue 158's single test and
   rejected #156's PR as "tautological". Fixed three ways: base cleaned,
   diffs containing `.harness/` denied (both stacks, parity case), and
   tracked markers ignored outright (repo history is never a gate input).
   A second agent was observed `git add -f`-ing the marker 15 minutes
   before the fix hot-reloaded — the class is real and recurrent.
4. **Broker force-publish erased an operator commit** (#154 → fence in
   `954ccef`; three adversarial rounds — the first fix was itself refuted
   with a real-git proof that patch-equivalence refuses every conflicting
   rebase). Non-fast-forward publishes now require the remote tip to be the
   revision this deployment last published (control-store receipts);
   foreign or pre-receipt tips get merge-and-FF instructions.
5. **Codex visual-evidence dead end + fabricated evidence** (filed #153).
   Codex cannot mint screenshot-read receipts by design, so visual-AC
   resolves dead-ended after paid legs (~$3 on #156) — and the agent then
   committed **byte-identical synthetic diagrams** as evidence for two
   distinct ACs. Interim: `mints_visual_receipts` capability flag + gate
   messages that prescribe test-asserted ACs; proper fix (runner-minted
   receipts from rollout tool RESULTS, byte-identical tripwire) is
   designed in the issue. Note: for TUIs, rendered-buffer assertions
   proved *stronger* evidence than screenshots (#169's ACs).
6. **Stall watchdog + ADR 0012** (`954ccef`). The class-of-classes fix:
   escalating `target_stalled` telemetry for any watched target whose
   observable state stops changing, plus the provenance ratchet rule in
   CLAUDE.md. Dosage and rollback triggers recorded in
   `docs/adr/0012-structural-robustness-invariants.md`.

## Operator interventions (the babysitting the harness still needs)

Twelve, in four categories: **unblock replies** (#156 ×2, #165 ×2, #167 —
the reply/resume rail worked every time and is the model for future human
interaction); **exemptions** the harness cannot yet grant itself (#124
formatter edit to an existing test, applied by hand; review-round budget
reset via phantom-review dismissal — the classifier correctly blocked
review-bot impersonation, the label rail worked instead); **CI surgery**
(two hung Windows runs cancelled; one rerun; the PTY e2e platform-gated —
exactly the cross-platform blindness the boot preflight warns about); and
**PR-body reframing** (#169's visual ACs converted to test-asserted — the
reviewer's screenshot demand would have re-triggered the #153 dead end).

## Feature gaps observed (not defects)

- Agents repeatedly request brokered `gh pr edit` (PR-body updates) and
  `--force-with-lease` (now correctly answered by the #154 fence's
  merge-instead guidance).
- The codex governor env carries no `TEST_CMD`, so agents depend on the
  local `.harness/test-cmd` marker and habitually try to commit it — worth
  baking `TEST_CMD` into the generated hook like `REPO`/`BASE_BRANCH`.
- #124 (reviewer-granted test-edit exemptions) now has two live incidents
  demonstrating demand (fmt reflow; dep-upgrade test updates).

## Guardrail check (per the north star)

Cost per merged PR moved down (-70% vs the eighth deployment) with review
quality *strengthened* (pinned checkouts closed the phantom-verdict class;
the evidence gate caught fabricated proof). Rounds/PR looks inflated but
decomposes into harness artifacts now fixed. Escalations fired exactly
where designed (rejection ceiling, CI breaker) and both returned to the
pipeline after human action. Recurrence rate of fixed defects: zero within
the session. The metric to watch next deployment: rounds/PR and
`review_pin_deferred`/`branch_refresh`/`target_stalled` telemetry rates —
all three should be near-zero on a healthy run.
