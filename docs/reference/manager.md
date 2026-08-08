# The manager role — babysitting a deployment

Moved out of CLAUDE.md 2026-07-29 to keep the codex instruction stack under
its 32KB budget (CLAUDE.md rides along in fixer prompts; this manual is for
the operator/manager session, not the fixers).


Whoever babysits a deployment — today an interactive session, eventually the
#11 daemon — owns this lifecycle. Scope boundary: everything here is
**reversible or additive** (comments, labels, restarts, filed issues, reports);
the moment a fix needs harness code, file an issue with journal evidence and
stop — **unless the operator has explicitly directed fixing gaps on the spot**
(2026-08-08 batch ran under that directive and it worked). Mid-batch harness
fixes then follow their own discipline: reproduce the failure before fixing
(two of three filed hypotheses that day were wrong — #177's reruns were all
landing, #178 was a two-dot/three-dot diff bug, and repro-before-fix caught
both); adversarially review the diff in an unbiased subagent; commit only
`make test`-green coherent states (hot reload picks them up between ticks);
and regenerate the composed env snapshot when the fix adds a knob the live
daemons must see.

## Direction: toward an unsupervised harness

The long-term goal is a harness that runs without supervision, where every
remaining operator action genuinely requires human judgment. Two standing
obligations follow (operator, 2026-08-08):

- **Triage every operator action in the batch readout**: for each unblock,
  restart, or hand-fix, say whether it needed a human (product ambiguity,
  platform the box can't reach, spend policy) or only needed knowledge the
  harness could have had (an exit code, a capability probe, a stale install).
  The second kind is a gap — file or fix it; it must not recur as an operator
  action.
- **Apply the surface-area test to every fix**: a good fix is *subtractive* —
  it removes a piece of knowledge or intervention the operator previously had
  to supply. A fix that adds a knob, a flag, or a per-incident special case
  increases the surface and is presumptively wrong; if shipped as a
  transitional measure, name the structural fix that supersedes it and the
  measurement that proves the knob is no longer needed. Gap severity should
  fall across deployments ($126/merge → $5.49/merge over eight); if a class
  recurs, the prior fix treated the instance, not the generator.

Accepted residuals live in [docs/open/residuals.md](../open/residuals.md) with
named triggers — e.g. platform-invisible CI (a Windows-only failure cannot be
reproduced by a Linux leg; the environment manifest keeps legs from burning
spend on impossible local repro, and true convergence is an operator decision
about paid runners, not a harness fix).

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
  silence. **Monitoring is non-verbose by default** (operator, 2026-08-02):
  ONE summarising watcher on the 15-minute cycle (new-event counts per role +
  daemon liveness), no per-event live tail — a line-by-line escalation/verdict
  stream drowns the operator and the session. Arm a live tail only while
  actively debugging an incident, and tear it down when the incident closes.
  Under systemd supervision the role logs are in
  `journalctl --user -u 'issue-agent-*-<name>'`, not the state-dir log files;
  detect and restart dead daemons; investigate wedged legs before
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
  daemon), **write the manager handover and commit it directly to `dev`** —
  `docs/open/<date>-handover-<batch>.md`, the operator-facing batch record,
  together with the ledger verdicts ticked during the batch, in one docs-only
  commit. (Not to be confused with the FIXER's exhausted-leg handover —
  `.harness/handover.md` in worktree scratch, written by
  `adapters/handover.py` for the next leg of the same issue to consume,
  excluded from git and never committed. The two share a name and nothing
  else.) Don't sit on it: a manager session can die at any point and an
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
- **Pre-launch overlay checklist grew two entries (2026-08-08)**:
  `GATE_TEST_FAILURE_EXIT_CODES` must cover every runner in TEST_CMD (cargo
  fails with 101, make with 2 — doctor now errors/warns on gaps, but the
  overlay is where the fix goes) and `EVIDENCE_STACK` should be declared when
  the target's live-evidence toolchain is known (lets the environment manifest
  attest "cannot produce live evidence here" as a verdict, ending
  unproducible-evidence review wedges without a scope note). Entrypoint drift
  no longer needs a manual `make install` — `issue-agent start` self-heals it
  unless another deployment is live.
- **The dashboard unblock endpoint is keyed by ISSUE number, not PR number**
  (`/api/repos/<name>/issues/<issue>/unblock`) — posting the PR number returns
  "issue #N is not open". The optional `guidance` field scopes the next review
  round (absent=keep, ""=clear, text=set); prefer no note now that review
  prompts carry the PR discussion and the capability manifest.
- **Never run `issue-agent dashboard` from a sandboxed assistant shell** — the
  server inherits the sandbox and every `gh` write dies with an h2 "stream
  CANCEL". Spawn it unsandboxed or the unblock lever silently can't write.
- **Terminal GitHub writes are durable.** A comment/relabel/close that exhausts
  transient retries remains a `lifecycle_commands` row in the SQLite control
  store. The daemon drains that authority before its next world read, reaper
  pass, or paid dispatch; ambiguous outcomes are reconciled against GitHub
  before another occurrence is issued. If a role appears idle with GitHub
  healthy, inspect nonterminal lifecycle commands and the typed
  `action_failed`/`action_retry` events before re-queuing work.
