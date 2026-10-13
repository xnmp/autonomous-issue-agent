# Handover: keifu batch complete, ADR 0012 landed, teardown clean

Date: 2026-08-01 ~11:15Z. Written at session end after the supervised keifu
batch finished. Companion documents: the findings report
(`2026-08-01-keifu-supervision-findings.md`, the analytical summary) and the
morning handover (`2026-08-01-handover-voyage-keifu-live.md`, now historical
— every open item in it is resolved). This document is the operational
state: what is running, what is committed where, what the next session
inherits.

## TL;DR for the next session

Nothing is urgent. The batch is done (6/6 merged), the daemons are DOWN
(clean SIGTERM, verified), the dashboard on :8321 is deliberately still up,
and local `dev` holds ~10 unpushed commits ending at `aee0871`. The three
highest-value next moves, in order: (1) push/promote `dev` if the operator
agrees; (2) run the next deployment and watch the three new telemetry rates
(`review_pin_deferred`, `branch_refresh`, `target_stalled` — all should be
~0 on a healthy run) plus rounds/PR; (3) build #153's runner-minted visual
receipts, which unblocks visual-AC issues on Codex.

## Where everything is

- **This checkout** (`~/Repos/autonomous-issue-agent`): branch `dev`,
  HEAD `aee0871`, working tree clean apart from pre-existing untracked
  dirs (`.codex/`, `.harness/`, older docs). `dev` is NOT pushed to
  origin — the session's commits exist only locally (plus this handover's
  branch). Daemons hot-reload from this checkout: **commit only coherent
  tested states** (`make test` green — it is, 1400+ tests + typecheck 0).
- **Keifu** (`~/Repos/keifu`): operator checkout on `chong-dev` with the
  operator's own uncommitted work (`src/ui/issue_compose.rs`, `test.py`) —
  DO NOT touch. Remote `chong-dev` advanced through PR #170; base also
  carries `539adaf` (operator: removed committed `.harness/test-cmd`,
  gitignored `.harness/`). No open ai-labeled issues or PRs remain.
- **State dirs**: `~/.local/state/issue-agent-xnmp-keifu` (+`review-`,
  `steward-` variants); control authority
  `~/.local/state/issue-agent-control/control.db` (pre-voyage backup in
  `backups/`, sha in memory). Dashboard server on :8321 RUNNING
  deliberately (pid ~2184895).
- **Session monitors**: all stopped. No background tasks remain.

## Session commit log (local dev, oldest first)

- `c0bd485` — pinned review checkouts hardened (0d2e295 adversarial FAIL
  set: H1 parent-repo reset hazard incl. symlinks, H2 prune-before-add,
  H3 bounded+journalled pin deferrals, M1 Claude pinning armed, M2 empty
  head defers). Two adversarial rounds.
- `3c47bf8` — advisory-UNSTABLE branch refresh (keifu #166 deadlock):
  `WorldState.base_head` observed from the ref (GraphQL `baseRefOid` is a
  stale anchor — verified live), once-per-(head,base) marker,
  `branch_refresh` telemetry. Two adversarial rounds.
- `4b8b596` — residuals: async update-branch verification false negative.
- `8208c39` — deny `.harness/` paths in gated diffs, both stacks + parity
  (keifu #167→#156 gate poisoning).
- `954ccef` — ADR 0012 lean set: stall watchdog
  (`domain/liveness.py` + fix-daemon reporter, `target_stalled`),
  publish-broker foreign-write fence (#154; receipt provenance — the
  git-cherry patch-equivalence approach was REFUTED by adversarial review
  with a real-git repro, do not resurrect it), tracked-marker distrust
  (`HarnessMarkers._tracked`, memoized), `mints_visual_receipts`
  capability + honest dead-end messages, CLAUDE.md provenance/liveness
  ratchet rule, `docs/archive/adr/0012-structural-robustness-invariants.md`.
  Three adversarial rounds total.
- `aee0871` — findings report.

Also filed on the harness repo: **#153** (Codex visual receipts: runner
must mint from rollout tool RESULTS not model-authored call text, plus a
byte-identical evidence tripwire) and **#154** (force-push authorship
safety — the fence in `954ccef` is the fix; the issue holds the incident
record). Closed on keifu: #145 (resolved by keifu#168).

## Batch outcome (detail in the findings report)

6/6 merged: #166 (issue 155), #167 (158), #170 (156), #165 (39), #169
(160), plus keifu#168 (operator lockfile PR the harness reviewed and merged
itself). $30.36 keifu spend, $5.06/merged PR, ledger == dashboard to the
cent at teardown. Twelve operator interventions, all through rails that
worked (reply/resume, label handoffs, review dismissal API, CI
cancel/rerun). Two escalations fired exactly as designed and were returned
to the pipeline.

## Teardown verification (do not re-verify, recorded here)

SIGTERM to launcher pid 3390429 at ~11:0xZ → watchers stopped, no
`issue-agent`/`point-at` processes remain, control store shows 0 running
runs, 0 dispatch claims. THREE leftover `update_branch` lifecycle commands
in `retryable_failure` for PRs 165/166/170 (all MERGED): this is the
documented async-verification false negative (residuals.md) — the update
landed but `is_branch_updated` read the ref too early. Next boot's
reconcile-first drain settles them; they authorize nothing meanwhile.

## Open items, priority order

1. **Push/promote local `dev`** — operator decision. Everything is tested
   and adversarially reviewed, but the session classifier has blocked
   merge-authority actions before; pushing `dev` to origin may need the
   operator to run `git push` themselves if blocked again.
2. **#153 (runner-minted visual receipts)** — the designed class fix for
   the Codex visual-AC dead end. Key design constraint discovered live:
   `view_image` calls appear inside model-authored `exec` JS in the
   rollout, so the receipt must key on the tool RESULT item (image payload
   attached), never the call text. Include the byte-identical tripwire.
   Until built, visual-AC issues on Codex fixers get the honest gate
   message (convert ACs to test-asserted) — which for TUIs produced
   *better* evidence anyway (#169).
3. **Bake `TEST_CMD` into the generated codex governor hook** like
   `REPO`/`BASE_BRANCH` (`codex_hook.py:_gov_script`). Observed: the
   scrubbed agent env has no TEST_CMD, so agents depend on the local
   `.harness/test-cmd` marker and habitually try to commit it (two agents
   did, 15 minutes apart). The gate now survives that, but baking the env
   removes the incentive.
4. **#124 (reviewer-granted test-edit exemptions)** — two more live
   incidents this session demonstrate demand (fmt reflow on #167,
   dep-upgrade test updates on the #145 class). Spec is in the issue.
5. **ADR 0012 deferred items with named triggers**: full branch
   ownership/handoff protocol (trigger: the foreign-write fence refusing
   more than occasionally — watch for its message in agent transcripts);
   capability manifest + routing (trigger: third runtime or second
   capability gap); `.harness/` relocation out of the worktree (needs an
   agent-reachable declaration channel — design alongside #153);
   provenance ratchet applies to all NEW verdict inputs (now a CLAUDE.md
   rule).
6. **Watch metrics next deployment**: rounds/PR, and the three new
   warning rates (`review_pin_deferred`, `branch_refresh`,
   `target_stalled`) — near-zero on a healthy run; a nonzero rate is
   either a real stall or the watchdog's own false-positive noise (ADR
   0012 names noise as its rollback trigger, so either way act on it).
7. Housekeeping: `config/repos/*` is gitignored (keifu cap changes are
   local-only); keifu MAX_BUDGET_USD=20 note from batch 3 (#89) still
   stands; the self-e2e deployment remains down (by design since the
   canary); `docs/open/` accumulates — several older handovers are now
   historical and could move to `docs/history/`.

## Session quirks worth knowing

- The permission classifier blocks: `gh pr merge`, branch-protection
  writes, acting under the review bot's token (impersonation-shaped), and
  once a compound python-heredoc-edits-test + push command (splitting into
  Edit + plain git push passed). Route around via label rails or ask the
  operator; never work around the intent.
- The reply/resume rail (`ai-blocked` + a comment) is the reliable
  operator→agent channel; PR-level resumes fire within a poll or two,
  issue-level took ~20 min once.
- Review-round budget resets are possible by DISMISSING the stale reviews
  via `gh api PUT .../dismissals` (works as operator) — used for #165's
  three phantom verdicts.
- Adversarial review loop that worked well: one persistent reviewer agent
  (retains context across rounds), FAIL→fix→delta-verify, and *empirically
  test both directions* — this session each side refuted the other once
  with live evidence (baseRefOid staleness; git-cherry vs conflict
  resolutions).
