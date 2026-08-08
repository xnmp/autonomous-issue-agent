# Session handover — post-batch fix round, 2026-08-08

Continuation of the tauri-explorer 8-issue batch (see
[2026-08-08-handover-tauri-batch.md](2026-08-08-handover-tauri-batch.md) for
the batch record). After the batch drained, the operator directed an immediate
fix round for every gap it surfaced. This documents what landed, what is
unverified, and what the next session owns.

## State at handover

- Harness `dev` is **8 commits ahead of origin, unpushed** (usual practice):
  `56f5a22` start self-heal + doctor exit codes, `85c98ff` reviewer
  discussion + environment manifest, `5d069b4` free-CI-rerun postcondition,
  `6c0bfed` three-dot test-file ceiling, `c4895ab` #176 fix-hold deferral,
  `1c24fea` + `5e7fd33` docs, on top of `17d20a2`/`4f80f3e` from the batch.
- `make test` + `make typecheck` green at HEAD.
- **Daemons LEFT RUNNING** at xnmp/tauri-explorer (fix/review/steward,
  foreground `issue-agent start` shell). They hot-reload from this checkout
  between ticks, so all of the above is live. Queue is drained — no
  `ai-ready` issues, no open `ai-pr-open` PRs.
- Dashboard server + one monitor shell still up in the manager session; both
  die with it, nothing depends on them.
- tauri-explorer `dev` holds the batch's 10 merges, **not promoted to main**.
- Tauri overlay (`config/repos/xnmp-tauri-explorer.env`, gitignored) gained
  `GATE_TEST_FAILURE_EXIT_CODES="1,101"` and `EVIDENCE_STACK="webkitgtk"`;
  composed snapshot regenerated, so live daemons see both.

## What landed (all adversarially reviewed, per-fix rationale)

| Commit | Fix | Class removed |
|---|---|---|
| 56f5a22 | `issue-agent start` self-heals entrypoint drift (one `make install`, refuses when another deployment is live); doctor errors on empty `GATE_TEST_FAILURE_EXIT_CODES` and warns when TEST_CMD's runner (cargo=101, make=2) is uncovered | stale-install launch blocks; Rust-seam red-fail gate deadlocks |
| 85c98ff | review prompts carry the PR discussion (untrusted-fenced, newest-first, capped); environment manifest boot-probes the box and, with `EVIDENCE_STACK` declared, attests "cannot produce live evidence" into fixer+reviewer prompts | reviewer-blind-to-comments; unproducible-evidence rejection wedges (613/614 shape) needing operator scope notes |
| 5d069b4 | free CI rerun verified by acceptance postcondition (GitHub bumps `run_attempt` async ~20s); honest `requested_run_ids` telemetry; one-shot per head; refusal settles PERMANENT | phantom rerun failures + ceiling-less redrive (#177 — premise falsified: all 7 reruns had landed) |
| 6c0bfed | publication test-file gate diffs `base...head` (merge-base net), not `base..head` | false blocks when other PRs merged into base mid-batch (#178 — filed hypothesis was wrong; repro found the real bug) |
| c4895ab | review daemon defers REVIEW/RE_REVIEW while the fix role holds a live dispatch claim on the same PR; ceiling = `agent_max_runtime`+300s; forced-loop bounded by `unproductive_rounds`; journals `fix_hold` per claim episode | submit_review head-race permanent failures (~$2–4 each, 4 in the batch) (#176) |

Issues #177/#178/#176 closed by these commits (Closes trailers). Filed and
**still open**: #179 (receipt self-collision — a dispatch retry re-executed
past its own recorded receipt; analysis posted on the issue), #180
(blocked-PR self-clear), #181 (launch hygiene). Target repo: #611 closed by
the batch.

## Unverified — the next batch adjudicates

Five **pending** rows at the bottom of
[docs/field-verification.md](../field-verification.md) (56f5a22, 85c98ff,
5d069b4, 6c0bfed, c4895ab). The headline measurements for the next
supervised run:

- zero evidence-demand rejection loops reaching the ceiling *without* an
  operator scope note (85c98ff is the structural fix; the 17d20a2 scope-note
  knob is transitional and should go unused);
- zero `submit_review` "head changed before submit" permanent failures;
  `fix_hold` waiting→cleared episodes present, `stopped` rare;
- `ci_rerun` journals `ok:true` with non-empty `requested_run_ids`;
- no test-file publication block on net-compliant diffs during concurrent
  merges;
- doctor exit-code section renders; a stale install self-heals at launch.

## Direction (operator, verbatim intent)

Long-term goal: unsupervised harness; every remaining operator action must
genuinely require human judgment. Every fix must be *subtractive* (remove an
operator-knowledge requirement), never a fragile knob — the whack-a-mole
concern is explicit. Both obligations are now codified in
[docs/reference/manager.md](../reference/manager.md) (Direction section +
amended self-modify rule). Platform-invisible CI is an accepted residual in
[residuals.md](residuals.md) with a named trigger.

## Loose ends for the next session

1. Push harness `dev` when the operator says so; promote tauri `dev` → main
   (10 merges waiting).
2. Next batch: tick the five pending ledger rows; watch the first
   fix/review overlap for `fix_hold` behavior.
3. #179/#180/#181 are harness-backlog candidates for the harness's own
   self-hosting queue.
4. Target checkout GC: stale `fix/482-*` worktree + ~10 dead `fix/*`
   branches.
5. Reviewer runtime is same family as fixer (codex/terra both sides) —
   decorrelation decision still owed.
6. Stop the daemons + dashboard if no batch is planned; they are idle but
   admitted.
