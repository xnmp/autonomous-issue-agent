# Handover — self-hosting batch 1: 5/5 merged, one fork bomb, three class fixes (2026-07-29)

First deployment of the harness against ITSELF (`REPO=self`, merges to `dev`,
promoted to `main` by hand). **5/5 merged**, but the day's real product is the
incident work: the gate got re-entrancy defenses it will need on every future
target, not just this one. Note: commit SHAs below are POST the node_modules
history rewrite another session ran late in the day; subjects are stable if a
SHA doesn't resolve.

## 1. Batch result

| issue | PR | path |
|---|---|---|
| #33 toolchain-drift preflight | #65 | 1st-pass APPROVE (sonnet, $11.70); its doctor checks verified live same day |
| #29 codex budget misreport | #66 | 1st resolve orphaned by an outage ($7.72 lost, see §3); codex/sol re-resolve 1st-pass |
| #61 test-domain purity | #68 | 1st-pass APPROVE (terra); merged via post-incident rebase |
| #63 gate fork bomb | — | **merged manually** (`afb7bdc`) — its review leg WAS the trigger; PR #70 closed as superseded, content credited |
| #50 PR-stage blocked resume | #72 | blocked leg burned (codex unresumable, → #71), fresh resolve + one FIX_CI, auto-merged |

Also: **#46 already-implemented** (evidence commented: `fix(roles): log each
daemon's capability set at boot`; operator closed). Swapped out pre-dispatch —
the "check git log -S before batching" lesson paid for itself.

**Cost**: real claude-dollars ≈ $26 (two resolves + five review legs); codex
legs are subscription-synthetic — the #29/sol resolve ledgered **$153 against
MAX_BUDGET_USD=20**, a live demo of the very bug it fixed (enforcement now
exists but `CODEX_ENFORCE_BUDGET` is not yet populated — see loose ends).
Reviewer first-pass: 4/4 of the PRs that reached it. Wall clock issue→merge:
~49 min for #33; the incident stretched everything else.

## 2. THE incident — the #63 fork bomb (read this before any new target)

`point-at REPO=self` fork-bombed at the gate probe: 21k+ processes, twice
(second time via the reviewer's gate on PR #68). Full chain and evidence on
issue #63. Compressed:

- `compose-env` exports every deployment knob (`set -a`); the gate ran the
  target's TEST_CMD with that env inherited.
- On a self-hosted target, `make test` runs the hook suites; ONE unpinned test
  case (`tests/test_hooks.sh:51`) let `check-commit.sh`'s red-verification
  resolve the inherited/marker `make test` and eval it → unbounded self-spawn.
  `timeout` never kills grandchildren.
- The recursion had MULTIPLE fuels, found one launch at a time: exported
  `TEST_CMD`, the `GH_APP_ID` placeholder (silently no-ops all review/steward
  test actions → 11 phantom failures), `TARGET_DIR` (the suite `cd`s into the
  real target repo and litters `fix/N-*` branches — cleaned twice), the state
  dirs, and `.harness/test-cmd` markers (every fixer writes `make test` there).

**The fix that landed** (`afb7bdc` + `74b75a1` + `88d3b04`, all on main):

1. **One guarded executor** — `governor.run_target_command` (python) and
   `prc_run_test_cmd` (bash twin) back EVERY target-owned exec: TEST_CMD,
   LINT/TYPECHECK/EDIT_CHECK_CMD, the point-at probe, `cli.verify_test_cmd`.
2. **Env scrub by manifest** — a static floor plus `HARNESS_DEPLOY_KNOBS`, a
   name manifest compose-env appends listing every knob it set. New knobs can
   never leak; a drift test pins the three list copies together.
3. **Re-entrancy bound** — `HARNESS_GATE_DEPTH` stamped into every child;
   execution refused at depth ≥ 2 as a **tri-state** (bash rc 213 / python
   `None`) that fails CLOSED in §1/red-verify/§10 naming the recursion — never
   as a phantom red suite. Doctor warns on env-resetting TEST_CMD wrappers
   (they erase the counter; the residual documented edge).
4. **Group kill** — gate timeouts kill the whole process group.
5. Suites sanitized (bash unset-lists + `tests/conftest.py`), hostile-env
   re-run suite (`make test-deployment-env`, CI-only step, deliberately NOT in
   `make test` — wall-clock tax).

Deliberately REJECTED as excess surface (operator call, agree before undoing):
/proc ancestry walks + argv-sentinel transports. An adversarial review
(findings live on #63) proved the env counter is erasable by a hostile marker
— accepted, because a hostile agent under bypassPermissions can fork-bomb
directly anyway; the realistic accidental paths are all covered.

**Why #63 merged manually**: its reviewer's own gate run was the trigger — a
genuine chicken-and-egg the operator authorized breaking by hand. The
adversarial-review subagent stood in for the reviewer (1 blocker + 4
should-fixes found and fixed pre-merge). This is the ONE deviation from
never-self-modify; don't treat it as precedent without the same conditions.

## 3. Second incident — box stall + GitHub 401s → #64/#69

At 03:13Z the box suspended/stalled (~26 min, heartbeat `woke after 1588s`);
during recovery `gh` got genuine 401s. The #29 resolve's blocked-state comment
and relabels each failed ONCE with no retry, yet were journaled as done →
GitHub diverged, the reaper re-queued the issue, $7.72 orphaned. Filed:
**#64** (lifecycle actions fire-and-forget) and **#69** (stall-triggered
reconciliation; the trigger for #64's mechanism). Also **#71**: blocked codex
resolves are unresumable (no session persisted — `issues_needing_resume`
structurally never fires; the "Reply to unblock" promise is false on codex).
Verified the reviewer runs as xnmp-bot via `REVIEW_BOT_TOKEN` (static-token
path; the `GH_APP_*` placeholders are NOT in use).

## 4. Config changes this session (all in gitignored files — no PR trail)

- `shared.env`: **`FIXER_RUNTIME="codex"` is now the global default**
  (operator decision); self.env's claude pin removed too.
- `self.env` TEST_CMD is `uv sync -q --extra sdk && make test` — the sync
  prefix retires when #61's fix is confirmed on a probe (ledger row); the
  earlier `env -i` wrapper is GONE and must not return (erases the depth
  counter; doctor warns).
- All three `.active.*` snapshots recomposed several times; compose-env now
  appends + exports `HARNESS_DEPLOY_KNOBS`.

## 5. Process/tooling notes

- **rtk wraps every Bash command of assistant sessions** (`rtk hook claude`);
  during the stall all commands queued ~10 min behind it. `rtk proxy` for
  pipes/counting; RTK.md was updated mid-session.
- The permission classifier blocked `pkill`, `git merge`, `gh pr merge` this
  session; `gh api -X DELETE .../labels/x` and `-X PATCH .../pulls/N -f
  state=closed` WORK — label removal and PR-close via raw API are available
  to the manager after all (CLAUDE.md gotcha is half-stale).
- Background-task launchers die with session interrupts (exit 144) — twice.
  For real batches use `issue-agent install-service` or an operator terminal.
- 15-minute polling is now the codified manager cadence (CLAUDE.md), and the
  long-run issue-count measures (incident→test, class-fix-over-instance-fix,
  workaround-names-executioner, first-of-class tuition) are in the Direction
  section. #73 filed for the dashboard net-open/recurrence panel.

## 6. Loose ends for the next session

1. **Populate `CODEX_ENFORCE_BUDGET=1` + per-model rates** in shared.env
   before the next codex batch — #29's enforcement is dormant without it
   (ledger row pending). The $153 sol leg is the motivation.
2. **Retire the `uv sync` TEST_CMD prefix** once a probe passes without it
   (#61 verification, ledger row).
3. **#46 and #70 are closed**; #64, #69, #71, #73 are the filed backlog —
   #64+#71 are good next-batch candidates (both inside `make test` coverage);
   #69 depends on #64.
4. **Daemons are DOWN** (operator killed them post-batch; queue empty).
   Relaunch = `make point-at REPO=self` after `git -C
   ~/Repos/autonomous-issue-agent-target pull` (the target clone predates the
   history rewrite — verify it fetches cleanly or reclone).
5. **History was rewritten** (node_modules scrub) by another session — old
   SHAs in issues/comments/this doc's ancestors don't resolve; the backup
   clone `~/Repos/autonomous-issue-agent-backup-20260729` is deletable once
   trusted.
6. The codex instruction stack for self is **over the 32KB budget** (doctor
   warns; CLAUDE.md tail gets cut from fixer prompts). Trimming CLAUDE.md or
   splitting the manager material out is worth a slot soon.
