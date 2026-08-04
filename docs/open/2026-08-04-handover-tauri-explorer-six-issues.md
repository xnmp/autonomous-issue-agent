# Handover — tauri-explorer six-issue run

Snapshot: 2026-08-04 21:46 AEST

## Objective

Complete the six armed `xnmp/tauri-explorer` issues end to end, fixing harness
gaps as they appear and requiring adversarial review of every harness change.
The user asked to use the setup wizard where possible and to use the same
credential arrangement as Keifu for all roles.

The six-issue objective is **not complete**. Two issues are merged and closed;
four remain open. At the user's direction, work stopped after completing and
testing the current harness fix. No target issue was rearmed, relabelled, or
otherwise mutated during this final harness-only pass.

## Executive state

- Complete: issues `#479` and `#564`; PRs `#578` and `#580` merged green.
- Human-held but otherwise green: `#495` / PR `#579` and `#555` / PR `#582`.
  Both are approved and clean, but the reviewer refused an automatically
  required `tests-immutable` exemption after additive coverage was placed in
  an existing test file. The merge side correctly withheld both.
- Human-held with a real outstanding defect: `#508` / PR `#581`. The fourth
  review found that PowerShell's `$ErrorActionPreference = 'Stop'` makes the
  first `Write-Error` terminate before the required per-tool diagnostics.
  `invocation` and `frontend` are failed.
- Active: `#496` / PR `#583`. The fixer repaired the visual evidence and the
  reviewer approved with three authenticated `tests-immutable` grants. CI
  then exposed a Ctrl+backtick regression. The harness reran failed jobs;
  `frontend` failed again while `smoke` and `webkit` were still running at the
  snapshot.
- The completed harness patch addresses approved-but-unmergeable exemption
  reviews, evidence-read deferral, grant persistence, current-head
  reconciliation, mechanical rebase validation, operator rearm, abandoned
  ceiling recovery, and zero-valued rejection ceilings. The full `make test`
  suite is green. The two findings from the fourth adversarial pass were fixed
  with non-vacuous regressions; the final complete-diff adversarial review
  returned PASS.

## Live service and configuration state

Installed user units:

- `issue-agent-fix-watcher-tauri-explorer.service`: active/running,
  `TasksMax=1024`.
- `issue-agent-review-watcher-tauri-explorer.service`: active/running,
  `TasksMax=1024`.
- `issue-agent-steward-tauri-explorer.service`: active/running,
  `TasksMax=512`.

The services were installed through `issue-agent install-service` after using
the repository setup path. The last doctor run passed with zero errors and
three warnings. Credential identities matched the Keifu arrangement:

- fixer: `xnmp`;
- reviewer: `xnmp-bot` via the configured review credential;
- steward: `xnmp-bot` via the same role-credential pattern.

Do not print or copy credential values into logs or documentation.

The steward service is healthy but intentionally capability-inert for this
bounded run. The active snapshot has `STEWARD_TRIAGE=0` and
`STEWARD_PROMOTE=0`; the other optional steward capabilities are not enabled.
This is unrelated to the earlier stale-revision bug. Credentials establish
identity; they do not enable capabilities.

The target checkout `/home/chong/Repos/tauri-explorer` was clean on `dev` but
two commits behind `origin/dev` at the prior 21:33 snapshot, corresponding to
the two harness merges. The four remaining per-issue worktrees were present
and clean at their PR heads. This state was deliberately not re-queried during
the final harness-only pass.

## Six-issue ledger

| Issue | PR | Current state | Evidence / next action |
|---|---:|---|---|
| `#479` virtualization windowing guard | `#578` | **Done** | PR merged 2026-08-04 09:25Z; all checks succeeded; issue closed with `ai-done`. |
| `#495` full image preview | `#579` | **Held** | Approved, clean, all checks green. PR has `needs-human-review`. Reviewer refused `tests-immutable` for `e2e/image-preview-fullscreen.spec.ts`, saying additive coverage needs no exemption. Rearm only after the harness fix is complete; the corrected workflow should request moving additive coverage to a new file or granting the existing-file edit. |
| `#496` terminal key ownership | `#583` | **Active CI repair** | Head `16b33806`. Reviewer approved at 10:58Z with authenticated grants for `e2e/terminal-panel.spec.ts`, `tests/domain/terminal-keys.test.ts`, and `tests/state/f5-graph-chain.test.ts`. Real-PTY Ctrl+Q evidence passed. CI found that Ctrl+backtick no longer hides the terminal panel (`e2e-tauri/specs/terminal.spec.ts:93`, “panel never hid”). Failed jobs were rerun; frontend failed again, smoke/webkit were running at snapshot. |
| `#508` one Windows installer | `#581` | **Held** | `CHANGES_REQUESTED`, `needs-human-review`, fourth-review ceiling. Remaining defect: `windows_install.ps1:54` terminates at the first `Write-Error` under `$ErrorActionPreference = 'Stop'`, so per-tool names and `winget` commands are never printed. Use non-terminating emission such as `-ErrorAction Continue`, then exit nonzero. `invocation` and `frontend` failed; WebKit passed. Rearm after the harness fix. |
| `#555` GitHub issue submission fallback | `#582` | **Held** | Approved, clean, all checks green. PR has `needs-human-review`. Reviewer refused `tests-immutable` for `e2e/bug-report.spec.ts` because the change adds regression coverage. Rearm only after the harness fix; the corrected workflow should make the refusal actionable instead of leaving an unmergeable approval. |
| `#564` FileChooser portal activation | `#580` | **Done** | PR merged 2026-08-04 10:20Z; all checks succeeded; issue closed with `ai-done`. |

## Committed harness fixes already deployed

The harness checkout is on `dev`. Relevant recent commits:

- `b302c06 fix: isolate doctor overlay state scan`
  - Prevents one repo overlay's ambient values from contaminating another
    role/repo during doctor composition.
- `2831b6a fix: serialize recovery after dispatch ownership`
- `e7a8b4b fix: harden live multi-repo supervision`
- `9e70231 fix: rearm operator unblocks`
  - Operator unblock now restores the appropriate issue/PR trigger rather
    than merely removing hold labels and leaving the target invisible.
  - Adds issue-vs-PR provenance, stale-episode protection, conditional label
    effects, and durable outcome reporting.
- `cf47719 fix: use reviewer identity for operator rearm`
  - Review dismissals now use the reviewer credential, while label changes
    continue to use the fixer credential.
  - Credential resolution matches the daemon's App/token/ambient precedence.
  - The final adversarial review for this committed change returned PASS and
    the full suite passed before commit.

The existing untracked user documents must be preserved and must not be swept
into a harness commit:

- `docs/open/2026-08-02-handover-keifu-batch2-drained.md`
- `docs/open/2026-08-03-handover-exemptions-deployed.md`

## Harness fix completed in this handoff

Current modified files:

- `src/issue_agent/app/dashweb/service.py`
- `src/issue_agent/app/workflows/review.py`
- `src/issue_agent/domain/exemptions.py`
- `src/issue_agent/domain/ports.py`
- `src/issue_agent/domain/review.py`
- `tests/app/test_dashweb_actions.py`
- `tests/app/test_review_workflow.py`
- `tests/domain/test_exemptions.py`
- `tests/domain/test_review.py`
- `tests/fakes/in_memory_github.py`

Current code/test diff size before the handover document: 1,045 insertions and
98 deletions across ten files. The patch is coherent and fully tested.

### Root incident being fixed

Since 2026-08-03, `tests-immutable` is automatically declared from the
revision-checked diff whenever an existing test file is modified. The review
prompt correctly asks the reviewer to grant or refuse every derived pair.
However, the workflow accepted a raw `APPROVE` even when the structured
`exemption_grants` field omitted required pairs. The merge side then correctly
withheld the PR and escalated it to `needs-human-review`. This produced an
approved, green, clean PR that no role could advance—the exact state of PRs
`#579` and `#582`.

The intended contract is:

- a mergeable approval remains `APPROVE`;
- an approval missing required grants becomes actionable
  `REQUEST_CHANGES` before submission;
- for `tests-immutable`, additive coverage should go in a new test file when
  changing the existing file is not itself justified;
- authenticated grants persist for identical patch content, as ADR 0014
  requires;
- every entry path—new submission, recovered/external current-head verdict,
  and mechanical-rebase carry-forward—uses the same completeness rule;
- transient commit-message/diff read failures defer without posting a verdict,
  are journalled, and enter a bounded breaker after five free deferrals;
- `REVIEW_MAX_REJECTIONS=0` really disables the ceiling.

### Implemented

- Converts grant-incomplete approvals into `REQUEST_CHANGES` with a
  harness-authored explanation and an exact ungranted pair list.
- Drops partial newly minted grant lines from a review that becomes
  `REQUEST_CHANGES`; merge collection trusts grant lines only in approved
  reviews.
- Aggregates authenticated grants across all reviewer-bot approvals for the
  same issue and rebase-invariant patch ID, preserving ADR 0014 authority.
- Adds `authenticated_grant_pairs` as the shared pure MAC/issue/patch checker.
- Validates current-head reconciled approvals and mechanical-rebase approvals
  before skipping a paid review.
- Defers unreadable commit-message evidence instead of treating it as no
  `required-test` declaration.
- Adds the missing `pr_commit_messages` operation to `WorkflowGitHubReads` and
  brings the in-memory fake into conformance.
- Makes the rejection ceiling's `<=0` disabled contract effective.
- Treats a trailing raw approval as non-resetting only when durable lifecycle
  provenance attributes the `needs-human-review` hold to the reviewer. This
  lets unblock dismiss the standing reviewer-bot change requests behind a
  grant-incomplete approval without retracting resolved reviews for unrelated
  fixer or budget holds.
- Makes abandoned-ceiling recovery reconstruct the rejection suffix preceding
  a current-head reviewer escalation comment, so a trailing incomplete
  approval cannot make the reviewer inert after a legacy/manual label clear.
- Replaces the mechanical-rebase shortcut's boolean result with a value that
  carries a proven incomplete prior-head approval into later rejection
  accounting.
- Adds behavior tests for no grant, partial grant, forged prose, no MAC key,
  no issue binding, current-head reconciliation, mechanical rebase,
  grant persistence, evidence deferral/reset/bounds, rejection accounting,
  held-PR operator rearm, abandoned-ceiling recovery, and the exact
  three-rejections-plus-incomplete-approval rebase history.

### Verification run on this exact diff

The most recent full run completed successfully:

- governor and accepted-residual suites passed;
- hooks: 116 passed;
- point-at checks: 11 passed;
- domain: 993 passed;
- adapters: 1083 passed;
- app: 2061 passed;
- gate parity passed;
- all four import-linter contracts held;
- pyrefly reported zero errors;
- `git diff --check` passed.

The focused domain/review-workflow/dashboard suite also passed before the full
run. The first full-suite attempt was blocked by sandbox DNS while `uv` fetched
the declared `tree-sitter` dependency; the approved network retry completed
successfully. Final complete-diff adversarial review: **PASS**, with explicit
confirmation that budget, `ai-blocked`, fixer, and unknown-provenance holds
retain normal approval-reset semantics.

## Resolved adversarial-review findings

The fourth review pass found two actionable issues. Both are now fixed.

### 1. Operator rearm disagreed with grant-aware rejection accounting (P1)

`count_prior_rejections` could exclude a current-head approval proven
grant-incomplete, while dashboard rearm and abandoned-ceiling recovery still
treated every raw `APPROVED` event as a reset.

Reproduction shape:

```text
REQUEST_CHANGES, REQUEST_CHANGES, REQUEST_CHANGES,
APPROVED-without-required-grant on the same head
```

Repair: dashboard unblock passes only `needs-human-review` targets whose
durable lifecycle event attributes the hold to the reviewer. Only those PRs
ignore trailing approvals when selecting stale reviewer change requests;
default selection is unchanged for budget/fixer holds and everywhere else.
The abandoned arm independently reconstructs the rejection sequence before
the current-head escalation comment. Domain and dashboard-level positive and
negative tests cover the raw history above.

### 2. Mechanical-rebase incomplete approval reset the ceiling (P2)

`re_review` computed its non-resetting approval set before the mechanical
rebase classifier proved the prior-head approval incomplete, losing that
classification before the later rejection count.

With three prior rejections followed by an incomplete approval on `old-head`,
then an unchanged rebase to `new-head`, an omitted-grant approval posts ordinary
`REQUEST_CHANGES` instead of the ceiling `COMMENT` plus hold.

Repair: `_patch_unchanged` now returns `_PatchShortcut`, which distinguishes a
settled shortcut from a normal review and carries prior-head incomplete
approval commits. `re_review` unions that evidence into its rejection count.
The exact three-rejections + incomplete old-head approval + unchanged rebase
test proves the resulting verdict is the ceiling `COMMENT` and PR hold, not an
ordinary `REQUEST_CHANGES`.

## PR-specific technical notes

### PR #579 / issue #495

All CI is green and GitHub reports `CLEAN` / `APPROVED`. The last review says
the tall-image evidence and regression behavior are correct but refuses
`tests-immutable` because the change “only adds a new test.” The protected
file is `e2e/image-preview-fullscreen.spec.ts`. The harness gate's rule is
file-level: adding a test to an existing file still modifies that file. The
new workflow must turn this into a fixer instruction to move additive coverage
to a new spec, unless the reviewer explicitly grants the existing-file edit.

### PR #582 / issue #555

All CI is green and GitHub reports `CLEAN` / `APPROVED`. The last review says
the GitHub sign-in evidence and opaque-toast regression are correct but refuses
`tests-immutable` for `e2e/bug-report.spec.ts`. It is the same harness dead-end
class as PR #579.

### PR #581 / issue #508

The fourth review escalated with a concrete blocker, not design ambiguity:

```text
windows_install.ps1:54
$ErrorActionPreference = 'Stop'
```

The first `Write-Error 'error: missing prerequisites:'` terminates execution.
The acceptance criterion requires the subsequent per-tool names and `winget`
commands to be emitted. Use non-terminating error output for those lines, then
exit nonzero explicitly. Re-arm must dismiss the reviewer-bot's standing
change requests with the reviewer credential and restore the PR trigger.

### PR #583 / issue #496

The original visual-evidence rejection was resolved. The latest approved head
contains five healthy real-PTY screenshots and a visible
`terminal-key-byte=17` result for Ctrl+Q. The three modified existing test files
have valid authenticated grants.

The latest CI failure is now behavioral: the existing core Ctrl+backtick
shortcut is no longer retained inside the terminal. The real smoke failure is:

```text
e2e-tauri/specs/terminal.spec.ts:93
embedded terminal Ctrl+` from inside the terminal hides the panel and keeps the session
panel never hid
```

The harness logged:

```text
re-run failed checks PR #583 (16b3380; runs 30902657815,30902657839)
CI re-run PR #583: captured workflow runs were not executable — the paid CI-fix leg dispatches next poll
```

At the snapshot the rerun had started; frontend failed again, while smoke and
webkit were still running. Once all jobs settle, the fixer should dispatch a
paid CI-repair leg. If it does not, inspect the latest fix-service journal and
the CI pending/retry breaker before changing the target manually.

## Safe continuation sequence

1. Preserve the two pre-existing untracked user documents. Do not stage them
   accidentally.
2. Confirm all three services are active after the committed reload and run
   doctor again without exposing credentials.
3. Rearm held issues/PRs `#495/#579`, `#508/#581`, and `#555/#582` through the
   dashboard/backend operator-unblock path. Do not manually toggle labels: the
   real path also clears retry counters, dismisses reviewer-bot change requests
   with the reviewer credential, restores lifecycle triggers, writes durable
   commands/receipts, and journals the outcome.
4. The prior attempt to execute these unblocks was rejected by the external
   safety approval because the targets are human-held. Obtain an explicit user
   approval to rearm those three targets before applying the operator action;
   do not bypass that decision with raw `gh label` or review-dismissal calls.
5. Let `#579` and `#582` return to the fixer as actionable exemption refusals;
   verify the additive tests move to new files or a reviewer grants the exact
   current-patch pairs. Let `#581` fix the terminating PowerShell diagnostics.
6. Continue monitoring `#583` through CI repair, re-review, and merge.
7. Completion requires all six issues closed with `ai-done`, all six PRs
   merged, and the merged heads' required checks green. Re-query GitHub rather
   than inferring completion from service logs.

## Useful commands

From `/home/chong/Repos/autonomous-issue-agent`:

```bash
git status --short
git diff --check
UV_CACHE_DIR=/tmp/issue-agent-uv-cache make test
systemctl --user show issue-agent-fix-watcher-tauri-explorer.service \
  --property=ActiveState,SubState,MainPID,TasksCurrent,TasksMax
systemctl --user show issue-agent-review-watcher-tauri-explorer.service \
  --property=ActiveState,SubState,MainPID,TasksCurrent,TasksMax
systemctl --user show issue-agent-steward-tauri-explorer.service \
  --property=ActiveState,SubState,MainPID,TasksCurrent,TasksMax
journalctl --user -u issue-agent-fix-watcher-tauri-explorer.service \
  --since '10 minutes ago' --no-pager -o cat
```

From `/home/chong/Repos/tauri-explorer`:

```bash
gh issue view 495 --repo xnmp/tauri-explorer --json state,labels,closedAt,url
gh pr view 579 --repo xnmp/tauri-explorer \
  --json state,labels,reviewDecision,mergeStateStatus,statusCheckRollup,headRefOid
gh pr view 581 --repo xnmp/tauri-explorer \
  --json state,labels,reviewDecision,mergeStateStatus,statusCheckRollup,headRefOid
gh pr view 582 --repo xnmp/tauri-explorer \
  --json state,labels,reviewDecision,mergeStateStatus,statusCheckRollup,headRefOid
gh pr view 583 --repo xnmp/tauri-explorer \
  --json state,labels,reviewDecision,mergeStateStatus,statusCheckRollup,headRefOid
```

Use `gh run view <run-id> --log-failed` only after the run is complete; GitHub
does not expose failed-job logs while another job in the same run is still in
progress.

## Completion proof required

Do not mark the persistent goal complete until current GitHub evidence proves:

- issues `#479`, `#495`, `#496`, `#508`, `#555`, and `#564` are all closed;
- PRs `#578`, `#579`, `#580`, `#581`, `#582`, and `#583` are all merged;
- the final merged heads have no failed or in-progress required checks;
- no selected target remains under `needs-human-review`, `ai-pr-open`, or an
  unprocessed blocker;
- every harness change made during the run is committed only after a full
  green suite and adversarial PASS.
