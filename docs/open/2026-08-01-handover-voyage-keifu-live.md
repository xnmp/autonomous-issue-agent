# Handover: maiden voyage complete, keifu supervised run LIVE

Date: 2026-08-01 ~05:12Z. Written at operator request to cut context cost.
Working tree is CLEAN; every change is committed on `dev` (HEAD `995108b`).
**The keifu deployment is RUNNING in the background** (continuous foreground
launcher under systemd-inhibit, bash task in the operator session). Dashboard
on :8321 is live and has matched GitHub/ledger at every check.

## What the user asked for (goal hook, still active)

Run the self-canary, resolve findings (adversarial review mandatory), point
the harness at ~/Repos/keifu, supervise ≥5 issues under stress, fix harness
issues that arise, keep the dashboard truthful, report findings.

## Completed

1. **Fresh adversarial review of WIP `56aa882`**: no P0/P1; four P2s fixed
   (`6bfcebe`), delta-review fixups (`5097e40`). Full gate green throughout.
2. **Self-canary DONE**: REVIEW #152 reached the runner (no
   `dispatch_claim_unavailable`), APPROVE r1 $0.4485, **harness itself merged
   PR #152** (03:51Z) and closed #150 (`ai-done`). 0 open claims, 0
   non-terminal runs, dashboard == ledger to the cent. control.db backup:
   `~/.local/state/issue-agent-control/backups/control.db.pre-voyage-20260801T092756`
   (sha256 405592eb…).
3. **Keifu live since 03:55Z**: issues #158 #155 #39 #156 #160 active
   (#145 parked `needs-human-review` — needs the unimplemented #124
   exemption flow; it requires editing existing tests for a dep upgrade).
   PRs so far: #165 (issue 39, review round 3, repair loop), #166 (issue
   155, **APPROVED r2** — unmerged: Security Audit check FAILED → merge gate
   correctly refuses; see Next steps), #167? (#158 just opened its PR at
   05:11Z). Spend $11.72 keifu today; caps: issue 250 / daily 150 / global
   1500 armed.

## Harness defects found live and FIXED (each: root cause → tests → commit)

- `5cffb61`+`e521aa3` launch guard self-refusal → lifetime flock.
- `90b6cc7` merge gate: 07-31 hardening required server-verified protection
  unconditionally → zero merges possible anywhere; restored exact-head
  client gate (user-approved via AskUserQuestion after classifier blocks).
  Server gate stays defense-in-depth; base-advance TOCTOU in residuals.md.
- `1fe5fa1` governor denied the workflow's own mandatory
  `gh issue comment` (bounded allowance; adversarially reviewed).
- `dc9dff0` oversized-image gate flagged INHERITED evidence files (a past
  merge's 986KB screenshot blocked every commit) → only author's changes.
- `fd13fa8` publish broker scrubbed the credential helper → all pushes died
  headless; now wires `credential.helper=!<trusted gh> auth git-credential`,
  GIT_TERMINAL_PROMPT=0.
- `e5c504a` test-file detector didn't know Rust (`tests/*.rs`, `_test.rs`).
- `0d2e295` **review legs ran in the operator's live checkout** → reviewer
  quoted stale `chong-dev` code under fresh SHAs → 3 phantom
  REQUEST_CHANGES on #165. Now: detached per-PR worktree pinned at the
  reviewed head (ReviewCheckouts), defer when unpinnable, cleanup on
  settled rounds. Also: `--repo <own repo>` comment form allowed
  (repo threaded via GateContext; foreign repos denied).
- `995108b` REPO baked into generated codex gov.sh (agent env is
  allowlist-scrubbed, so 0d2e295's env lookup never fired on codex).

**ADVERSARIAL REVIEW OF `0d2e295` RETURNED FAIL — fix these FIRST next
session (full report: the review agent's output, reproduced here):**

- **H1 (data-loss hazard, live now):** `ReviewCheckouts.ensure()` enters the
  reuse branch on `isdir()` alone. If `review-pr-N` exists as a plain
  UNREGISTERED dir (partial remove; fix-daemon `worktree prune` racing an
  add), git discovers the PARENT repo → `reset --hard`/`clean -fd` hit the
  operator's live checkout, and the `rev-parse` confirmation passes because
  the parent's HEAD was just moved. Fix: verify `git rev-parse
  --show-toplevel` == abs_path (or worktree list registration) before
  mutating; check checkout/reset exit codes. Until fixed, the hazard needs
  an unusual precondition (stale plain dir) — none exists right now — but
  do not leave the daemons unattended for long.
- **H2:** missing-but-registered worktree → `worktree add` fails forever →
  permanent deferral. Prune (or `add -f`) inside ensure() before giving up.
- **H3:** pin-failure deferrals call `_no_progress` → counted as PAID
  unproductive rounds → 5 free deferrals escalate the PR to
  needs-human-review. Deferral must not feed that counter.
- **M1:** pinning is inert for Claude reviewers (execution_cwd only set by
  the codex binding) — multi/delegated modes still run in the live checkout;
  the multi-mode head_sha threading is currently dead code.
- **M2:** empty `head_ref_oid` silently skips pinning instead of deferring;
  make head_sha required.
- **M3:** already fixed by `995108b` (REPO baked into gov.sh).
- Low: `review-pr-*` worktrees leak on non-settled paths (no boot
  reaper/SIGTERM cleanup); ensure() is sync inside a coroutine (300s×5
  worst-case stall); `_same_repository` has NO spoofing bypass (verified).

All other commits were reviewed or are small+tested.

## Known open items / next steps

1. **#166 (issue 155): APPROVED but Security Audit check FAILED** →
   `mergeStateStatus=UNSTABLE`, exact-head gate refuses (correct). The fix
   daemon should dispatch FIX_CI; verify it does, or investigate why the
   audit fails (likely the same cargo-audit advisories as #145 — if so,
   #166 may be blocked on #145's class and needs an operator decision).
2. **#165 (issue 39)**: repair loop was churning on phantom rejections
   (pre-pinning); with pinned checkouts live, the next re-review should
   judge real code. Watch: rejections 3/4 — ONE more escalates. If the next
   verdict looks wrong again, check the review worktree
   (`~/Repos/keifu/.claude/worktrees/review-pr-165`) HEAD vs PR head.
   Also: a rapid ADDRESS_REVIEW re-dispatch pattern was observed
   (~3 dispatches in 2 min, each pushing) — suspected missing dedup
   between "review addressed" and "re-review verdict"; not yet
   investigated. If it recurs, investigate decide_fix's address-review
   trigger.
3. **#156/#158/#160** progressing through resolve/PR; unblock replies were
   posted where needed. #158's PR just opened.
4. Agents requested brokered `gh pr edit` (PR-body updates) — feature gap,
   noted, not built.
5. `#145` + `#166?` cargo-audit class needs #124 (reviewer-granted
   test-edit exemptions, spec in xnmp/autonomous-issue-agent#124 comment).
6. After the batch: SIGTERM the launcher (single Ctrl-C equivalent:
   `kill -TERM <launcher pid>`; find via `pgrep -f 'point-at.sh xnmp-keifu'`),
   verify drain, reconcile spend (control.db charges vs dashboard), confirm
   no orphan codex trees (the user's OWN interactive `codex resume` TUI
   pids ~1671948 are NOT harness orphans — leave them), write the final
   findings report (metrics: cost/merged PR, first-pass rate, rounds/PR,
   wall time; guardrails per CLAUDE.md north star), update residuals.md.
7. Session quirks: the permission classifier blocks merge-authority actions
   (gh pr merge, protection PUT) — route through AskUserQuestion; user
   already approved the merge-gate commit once. `config/repos/*` is
   gitignored (cap change to keifu env is local-only).

## Where things run

- Launcher bash task: `point-at.sh xnmp-keifu` (session background task
  b7xdkbyqf). Monitors: launcher stdout grep (bbq3m7uyv), v2 events
  (bh5y5occc), 10-min dashboard-vs-GitHub line (b8e4q5mr6). Kill via
  TaskStop or let the session end; the launcher itself survives as a
  process group — SIGTERM it for clean teardown.
- Self-e2e state: `~/.local/state/issue-agent-self-e2e`. Keifu state:
  `~/.local/state/issue-agent-xnmp-keifu` (+review-/steward- variants).
- Do not touch `/home/chong/Repos/keifu` user state: modified
  `src/ui/issue_compose.rs`, untracked `test.py`, branch `chong-dev`.

## Teardown state (05:17Z)

Keifu launcher SIGTERMed and gone; no daemon/agent processes remain. One
leftover from the in-flight kill: dispatch claim `xnmp/keifu|fix|issue:156`
and run `7f12b3ff…` (RESUME, state=running). Next boot's recover() reclaims
both and emits the abandoned-run correction — expected, verify it happens on
relaunch. Dashboard server on :8321 left RUNNING deliberately.
