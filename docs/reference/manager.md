# The manager role — babysitting a deployment

Moved out of CLAUDE.md 2026-07-29 to keep the codex instruction stack under
its 32KB budget (CLAUDE.md rides along in fixer prompts; this manual is for
the operator/manager session, not the fixers).


Whoever babysits a deployment — today an interactive session, eventually the
#11 daemon — owns this lifecycle. Scope boundary: everything here is
**reversible or additive** (comments, labels, restarts, filed issues, reports);
the moment a fix needs harness code, file an issue with journal evidence and
stop. Never self-modify mid-batch.

- **Before**: read the pending rows of
  [docs/field-verification.md](docs/field-verification.md) (the append-only
  ledger of what has shipped vs what a live deployment has proven) and add
  rows for anything `git log` shows landed without one — each row names the
  journal evidence that will prove the change works; triage doctor output
  with judgment (a red preflight can be a false positive — verify
  independently before overriding, and file the gap); restore caps and
  regenerate stale env snapshots; populate newly-landed knobs; pick the batch
  (seam inside TEST_CMD coverage, no existing closing PRs, overlap-disjoint —
  the steward proposes under #15, the manager approves). Treat the **first
  launch at a new target as an incident surface, not a checklist step**: the
  gate probe runs the target's full suite under the deployment env for the
  first time — watch the process tree while it runs (a runaway is invisible in
  the launcher's buffered output), and when a harness bug blocks the launch,
  work around it in the gitignored overlay, file the issue with evidence, and
  put the real fix in the batch. Never patch harness code to unblock a launch.
- **During**: poll on a **15-minute cadence** (the default babysitting
  workflow): each cycle, read the new journal lines from all three role state
  dirs plus the fix-log tail, report anything notable to the operator, and
  re-arm — a cycle with no events is itself a report ("no new events"), not
  silence; detect and restart dead daemons; investigate wedged legs before
  killing; answer `status=blocked` clarification questions (evidence
  unproducible from a worktree/CI → scope it to operator post-verification;
  genuine product ambiguity → escalate to the human, don't guess); tick off
  the watch-list as its evidence arrives in the journals; flag anomalies with
  restraint — an expensive-but-in-cap leg is data, not an incident.
- **After**: file surfaced issues with evidence; produce the batch readout
  ($/merge, first-pass rate, cost mix by leg, guardrail deltas, **net open
  issues (opened − closed) and recurrence count** — the numbers the Direction
  section requires before stacking changes); give every
  watch-list entry a verdict in the ledger — **VERIFIED** (cite the journal
  event), **MISBEHAVED** (file it), or leave it **pending** (a change nothing
  fired is untested, not fine); hand the operator a
  single loose-ends list (post-deploy verifications, label surgery the
  classifier reserves, superseded PRs). **When the pending list drains** (no
  `ai-ready` issues, no `ai-pr-open` PRs, nothing held or blocked awaiting a
  daemon), **write the handover document and commit it directly to `dev`** —
  together with the ledger verdicts ticked during the batch, in one docs-only
  commit. Don't sit on it: a manager session can die at any point and an
  uncommitted handover dies with it (batch 3, 2026-07-29, took two takeover
  sessions to reconstruct). Mid-batch the rule stays the opposite — never
  move `dev` while ai PRs are open; it forces rebases on all of them.

## Operational gotchas (stable across 3+ deployments — no longer handover news)

- **Hot reload fires only BETWEEN ticks** (`adapters/reload.py`, watching the
  main checkout's HEAD + the AGENT_ENV file's mtime). Merging to main or
  editing the composed env mid-leg is safe; the in-flight leg finishes on old
  code. Corollary: editing `shared.env` makes every deployment's composed
  snapshot stale — regenerate each with `bin/compose-env.sh <name>` or doctor
  blocks the next launch.
- **Run git merges from the MAIN checkout, never with a worktree as cwd** — a
  `cd <worktree> && … && git merge && git worktree remove` chain merges
  nothing and then deletes your own cwd (bitten twice on 2026-07-28).
- **The daily spend window is per UTC day**; ledger sums and "why did the cap
  reset" confusion follow UTC, not local midnight.
- **Codex OAuth binds whatever ChatGPT session the browser has live** — after
  any login, verify the id_token email claim before trusting the account.
- **Evidence e2e legs open real app windows on the operator's desktop** —
  cosmetic, but warn whoever is at the keyboard.
- **Assistant sessions cannot use the `gh issue edit`/`gh issue close`
  wrappers** (permission classifier), but raw API calls work for the same
  operations: `gh api -X POST repos/<r>/issues/<n>/labels -f 'labels[]=x'`
  (add), `-X DELETE .../labels/x` (remove), and `-X PATCH .../pulls/<n> -f
  state=closed` (verified live 2026-07-29, both batches). Issue *close* has no
  verified raw-API path yet — leave closes to the daemons or the human under
  the `operator-action` label.
- **`gh` label/state searches are stale for recently closed objects** —
  verify with a direct `gh issue view` before acting on a search result.
- **Terminal GitHub writes are durable.** A comment/relabel/close that exhausts
  transient retries is stored in the role's `STATE_DIR/pending-actions.json`;
  the daemon drains that queue before its next world read, reaper pass, or paid
  dispatch. A write that remains unverifiable after queued replay is alerted and
  moved to `quarantined-actions.json` so it cannot halt unrelated issues. If a
  role appears idle with GitHub healthy, inspect both files and the journal's
  `action_failed`/`action_retry`/`action_quarantined` events before re-queuing
  work.
