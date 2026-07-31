# Handover: dispatch identity remediation and live canary

Date: 2026-08-01

## Objective

Continue the same user goal to completion:

1. finish the architectural and remediation work without adding unnecessary
   manager/authority abstractions;
2. keep recovery policy centralized while making recovery triggers explicit;
3. document architectural decisions in ADRs and reference them from code;
4. commit only a coherent, fully tested state;
5. prove the dashboard and the refactored harness end-to-end by resolving the
   disposable self-canary issue/PR while watching its live progress;
6. after the self-canary is clean, supervise at least five real Keifu issues
   under bounded stress and capture the evidence;
7. run a fresh, unbiased adversarial review before each final commit/hand-off.

The user stopped the current agent deliberately and requested this handover.
Do not infer that the implementation is abandoned.

## Repository state at handover

- Checkout: `/home/chong/Repos/autonomous-issue-agent`
- Branch: `dev`
- Parent HEAD: `5d6d1759c15c01411aba03ea28e15f8e43ba0251`
- `origin/dev` was also at `5d6d175` when checked.
- The commit containing this document is an explicitly authorized **WIP
  checkpoint** on top of that parent. Use `git log -1 --oneline` for its exact
  hash.
- No harness daemon was started after the latest edits.
- The final fresh adversarial reviewer was cancelled when the user asked to
  stop; it produced no verdict. A new final review remains mandatory before
  treating the checkpoint as canary-ready.

Recent coherent commits already on `dev`:

- `51579f2 refactor: isolate paid run authority from target commands`
- `5d6d175 refactor: make recovery triggers explicit`

The recovery-trigger work is complete and should not be replaced with a larger
manager. `RecoveryCoordinator._sweep()` remains the shared reconciliation core;
the explicit trigger policy maps boot, wake, and periodic triggers to the
correct arms and grace. ADR 0006 records that decision.

## WIP checkpoint contents

The checkpoint contains the implementation/test changes in these 14 files:

- `docs/adr/0008-control-authority-scope-and-deployment-fencing.md`
- `docs/reference/architecture.md`
- `src/issue_agent/adapters/accounting_runtime.py`
- `src/issue_agent/adapters/journal.py`
- `src/issue_agent/adapters/runtime_plan.py`
- `src/issue_agent/app/dispatch.py`
- `src/issue_agent/app/roles.py`
- `src/issue_agent/app/workflows/fix.py`
- `src/issue_agent/app/workflows/review.py`
- `tests/adapters/test_accounting_runtime.py`
- `tests/app/test_dispatch.py`
- `tests/app/test_journal.py`
- `tests/app/test_review_workflow.py`
- `tests/app/test_roles.py`

This handover document is included in the same checkpoint.

Do **not** stage these pre-existing user-owned untracked paths:

- `.codex/`
- `.harness/`
- `docs/open/2026-07-30-handover-124-test-immutability-escalation.md`
- `docs/open/2026-07-30-handover-batch-4-codex-reviewer.md`
- `docs/open/2026-07-31-codex-remediation-workorder.md`
- `docs/open/2026-07-31-implementation-review-for-fable.md`
- `docs/open/2026-07-31-maiden-voyage-babysitter-runbook.md`

These paths were deliberately excluded from the checkpoint. Continue using
explicit staging; do not use `git add -A` here.

## What the checkpoint implementation does

The live canary exposed `dispatch_claim_unavailable` on review PR #152. The
decision side reserved the issue inferred from branch `fix/150-...`, while a
later path independently derived another target from mutable/partial GitHub
linkage. Component tests were green, but the composed authorities disagreed.

The patch reuses the existing dispatch reservation instead of introducing a
new authority object:

- the observed-world reservation target is carried through dispatcher routing,
  journaling, and paid accounting admission;
- a fresh PR read validates linkage but cannot replace the admitted target;
  disagreement defers without spend;
- claim identity and spend identity remain distinct for issue-less PRs: `-PR`
  can be the claim target while per-issue spend remains absent;
- review retry/death identity is separately and stably `-PR`, so a later issue
  relink cannot erase a paid failed review round;
- fix and steward retry ceilings remain issue-owned;
- recovered control-store abandonments and journal-only deaths use the same
  stable review retry key;
- `JournalingRuntime.leg_kind()` preserves the claim target through multi-leg
  review fan-out;
- fix PR workflows and review workflows reject fresh linkage that disagrees
  with the observed reservation before touching a worktree/session or buying a
  leg.

ADR 0008 and the architecture reference describe the claim/spend/retry
separation. ADR 0008's `Governs` list now includes accounting, journal,
runtime-plan, dispatcher, roles, and both affected workflows. Code references
ADR 0008 in those seams. The repository currently has ADRs 0001 through 0011.

## Verification already completed

Focused regression suites passed, including:

- decision-side claim survives a failed live `issue_for_pr` lookup;
- fix PR execution defers when fresh linkage changes;
- issue-less review claims `-PR` without creating a bogus issue budget scope;
- multi-review leg context preserves the claim target;
- linked review uses issue claim/spend but `-PR` retry identity;
- raised review deaths and journal-recovered deaths use `-PR`;
- a paid failed review remains counted after a later issue relink;
- productive review clears the stable key and legacy issue-key migration key.

The full repository gate was rerun after the last functional/test correction
and passed:

- 90 hook checks;
- 8 point-at checks;
- 676 domain tests;
- 857 adapter tests;
- 1,375 app tests;
- gate parity green;
- all four import-linter contracts kept;
- pyrefly: 0 errors;
- `git diff --check`: green.

The exact checkpoint snapshot, including the final ADR wording and code
references, was rerun immediately before the WIP commit with:

```bash
UV_CACHE_DIR=/tmp/issue-agent-uv-cache make test
git diff --check
```

One earlier full run found a stale assertion expecting issue-keyed review retry
state. It was corrected to the stable `-PR` contract, and the subsequent full
run above was green.

## Adversarial-review history

Three independent reviews found genuine composition variants, all addressed in
the current diff:

1. The reservation target was not carried into paid admission; a second lookup
   could recreate `dispatch_claim_unavailable`.
2. An old issue hint could be combined with a freshly relinked PR, mixing issue
   A's worktree/session with PR state now linked to issue B.
3. Review failures were written under a mutable issue key, so a later relink
   could reset the effective retry ceiling; journal/control-store recovery had
   to align with the stable PR key.

A fourth clean-slate adversarial review was started only after the full green
run, but was explicitly interrupted for this handover. It did not report PASS
or findings. Before treating this WIP commit as ready or starting the canary:

1. inspect the WIP commit against its parent;
2. give the **entire checkpoint diff** to a fresh subagent without suggesting a
   preferred conclusion;
3. ask it to inspect surrounding production code, races, recovery, migrations,
   and live-canary failure modes;
4. resolve every actionable finding;
5. rerun the full gate and repeat fresh review until PASS.

Suggested neutral prompt:

> Perform a fresh adversarial review of the current WIP checkpoint against its
> parent. Read
> CLAUDE.md and applicable repository instructions. Review correctness, races,
> architectural boundaries, incomplete migrations, tests, and live-canary
> failure modes. Inspect surrounding production code and run focused tests as
> useful. Do not modify files. Report actionable findings with severity and
> file/line evidence, or PASS if none.

## WIP boundary

The user explicitly authorized a WIP commit so the green implementation and
handover have a durable checkpoint. This does **not** waive the missing final
adversarial verdict and is not authority to start a live canary.

After fresh review findings are resolved and the full gate passes again, make a
follow-up coherent commit. A suitable message is:

```text
fix: preserve dispatch target authority across review
```

Do not start the canary merely because the code is now committed: live daemons
hot-reload committed HEAD between ticks, so the reviewed follow-up state must be
HEAD first.

## Disposable self-canary state

Configuration:

- deployment name: `self-e2e`
- repository: `xnmp/autonomous-issue-agent`
- target checkout: `/home/chong/Repos/autonomous-issue-agent-target`
- base branch: `canary/a76e620-dashboard-e2e`
- disposable issue: #150
- disposable PR: #152
- PR head: `fix/150-live-e2e-canary-add-dashboard-health-end`

The user approved pushing canary-base commit `f993c22` to
`canary/a76e620-dashboard-e2e`; that push was completed from a separate
worktree. Local remote-tracking refs may be stale, so query the remote instead
of trusting them.

As of the last read-only GitHub query:

- PR #152 was open and mergeable;
- merge state was `CLEAN`;
- CI workflow `test` completed successfully;
- review decision was empty;
- head commit was `87fecd16743242a4c0d0cb335b26f5cc852d585a`;
- base was `canary/a76e620-dashboard-e2e`.

The failed prior reviewer attempt consumed no successful review and left the PR
waiting for the refactored reviewer. Do not manually paper over that result;
the purpose of the canary is to prove the composed review path.

## Dashboard state and why it showed no issues

A dashboard server was already running on the host at
`http://127.0.0.1:8321/`. Do not start a second copy unless the existing server
has actually stopped.

The last API check showed:

- `in_flight_issues = 0`;
- fix, review, and steward health all `alive=false`, `stale=true`;
- their final events were clean `draining -> stopped` transitions;
- historical spend was visible (`today_usd = 3.844`);
- the current issues list was empty.

This was not evidence that the page failed to render. The bounded daemons had
finished, emitted `world.feed.retired.v1`, and the current-world projection
removed their unfinished feed rows. The dashboard is currently a live/current
projection, not a promise to retain an inactive deployment's last issue card.
Do not change the UI merely because the stopped deployment is empty. The next
required proof is that issue/PR #150/#152 appears while the refactored daemons
are actively observing and processing it, and that user-visible stage/cost/leg
fields advance.

The sandbox cannot normally see the host loopback namespace. Host dashboard
queries may need approved escalation, for example:

```bash
curl -fsS http://127.0.0.1:8321/api/repos/self-e2e/overview
curl -fsS http://127.0.0.1:8321/api/repos/self-e2e/issues
```

Avoid dumping the enormous raw overview in the user response; filter it to
health, metrics, and the #150/#152 card.

If no dashboard process is running, the known command is:

```bash
UV_CACHE_DIR=/tmp/issue-agent-uv-cache \
  PYTHONPATH=src uv run --offline --no-sync issue-agent dashboard
```

## Next live steps

Only after commit and final review:

1. Confirm PR #152 is still open, clean, and CI-green.
2. Run a bounded foreground self-canary from the committed HEAD. Start with one
   tick as required by the maiden-voyage runbook:

   ```bash
   ISSUE_AGENT_MAX_TICKS=1 \
   GATE_PROBE=0 \
   UV_CACHE_DIR=/tmp/issue-agent-uv-cache \
   bash bin/point-at.sh self-e2e
   ```

3. While it is running, query the dashboard overview/issues endpoints and
   capture concise evidence that #150/#152 is visible with an active role and
   changing user-visible progress.
4. Watch the review log and durable control state. The review must reach the
   runner rather than return `dispatch_claim_unavailable`, submit its verdict,
   and settle its reservation/claim.
5. If one tick is insufficient to advance the whole lifecycle, run another
   bounded foreground window. Keep supervision and caps in place; do not jump
   straight to an unattended loop.
6. Let the normal harness lifecycle approve/merge PR #152 and close/resolve
   issue #150. The user's requested proof is the main user path, not a manual
   GitHub shortcut.
7. Verify the final dashboard state, GitHub labels/state, no live child process
   trees, no lingering dispatch claims/leases, and all reservations settled or
   correctly recovered.

Read and follow
`docs/open/2026-07-31-maiden-voyage-babysitter-runbook.md` before the live run.
Do not reveal secrets from generated active env files in logs or the handoff.

## Keifu follow-on

Do not start Keifu until the self-canary is fully clean.

Prepared config:

- `config/repos/xnmp-keifu.env`
- repo `xnmp/keifu`
- target `/home/chong/Repos/keifu`
- base `chong-dev`

The doctor check previously passed with warnings only. Planned issue set:

- #158
- #155
- #39
- #145
- #156
- #160 only as a backup / suspected duplicate

The primary Keifu checkout has user changes that must not be touched:

- modified `src/ui/issue_compose.rs`
- untracked `test.py`

Use harness-managed issue worktrees, supervise at least five issues, keep the
dashboard live, and apply the same stop-on-anomaly rules as the self-canary.

## Remaining completion criteria

The overarching task is not complete until all are true:

- the WIP checkpoint passes the full `make test`;
- a clean-slate adversarial reviewer returns PASS;
- any review findings are resolved in a coherent follow-up commit;
- self-canary PR #152 is reviewed/merged through the harness;
- dummy issue #150 reaches its expected resolved/closed state;
- dashboard visibly tracks the active run and reports truthful final state;
- durable claims, reservations, leases, and process trees are clean afterward;
- at least five Keifu issues have been supervised under bounded stress;
- final live evidence and residual findings are recorded.
