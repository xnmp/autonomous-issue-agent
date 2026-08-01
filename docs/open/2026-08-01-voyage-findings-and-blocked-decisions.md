# 2026-08-01 maiden-voyage findings and blocked decisions

**RESOLVED (same day):** the operator approved committing the merge-gate
restoration (§3 → committed as `90b6cc7`). The harness then merged PR #152
itself at 03:51Z through the exact-head client gate and the next tick closed
issue #150 (`ai-done`). Self-canary complete and reconciled (0 open claims,
0 non-terminal runs, dashboard matched ledger). Keifu supervised run started
03:55Z with `DAILY_SPEND_CAP_USD=150` armed. §4 below is retained as the
historical record of the blocked window.

**State at writing:** self-canary substantially proven; keifu phase NOT
started. Two actions were blocked on operator permission (see §4). The
working tree carried one uncommitted, fully tested, adversarially reviewed
change (§3).

## 1. What was proven live (self-canary, deployment `self-e2e`)

Precondition work, all committed on `dev`:

- `6bfcebe` — dispatch-target-authority fixes from the fresh adversarial
  review of WIP checkpoint `56aa882` (review verdict: no P0/P1; four P2s all
  addressed: issueless-claim fence in the dispatcher, `dispatch_deferred`
  TelemetryWarning on observed-vs-fresh target disagreement in both
  workflows, corrected reservation-collision log, residuals note on legacy
  issue-keyed review counters).
- `5097e40` — follow-ups from a second delta review (fence journals too;
  steward defer log corrected; comment accuracy).
- `e521aa3` — launch-guard rewrite (see finding F1).

Voyage evidence:

- **Review dispatch identity (the canary's purpose):** tick 1 dispatched
  `REVIEW #152`, it **reached the runner** (no `dispatch_claim_unavailable`),
  codex reviewed and submitted APPROVE (round r1), $0.4485 charged and
  settled. This closes the failure the 2026-08-01 handover was written for.
- **Money:** reservation settled to a single charge (control DB run
  `e3bd530d…`, REVIEW, completed, $0.4485); 0 open dispatch claims; 0
  non-terminal runs; dashboard `today_usd` matched the ledger to the cent.
  `control.db` backed up beforehand (`backups/control.db.pre-voyage-20260801T092756`,
  sha256 `405592eb…`).
- **Dashboard truthfulness:** roles showed alive=true/stale=false while
  running, reverted to not-alive after drain; spend and health matched
  reality at every check. (The issues card list stayed empty during the
  short single ticks — cards appear via the feed projection; verify again
  during the longer keifu run.)
- **Teardown:** three bounded runs (MAX_TICKS=1) all drained
  `running → draining → stopped` cleanly; no orphan process trees; no
  abandoned runs.
- **GitHub:** PR #152 is APPROVED, CI-green, CLEAN — and **unmerged** (§2).
  Issue #150 open pending merge+cleanup.

## 2. Findings (harness defects found and their status)

**F1 — launch guard refused its own first run (fixed, committed `e521aa3`).**
`point-at.sh`'s duplicate-launcher guard matched argv substrings; any wrapper
that quotes the launch command (systemd-inhibit, `sh -c`, the operator shell
snapshot) or a forked subshell of such an ancestor made the guard report a
"running" launcher and refuse to start. Two live refusals observed. Root-cause
fix: a per-name flock held for the launcher's lifetime and inherited by the
role daemons (`bin/lib/launch-guard.sh`), with regression tests. Argv matching
is unwinnable and is gone.

**F2 — automated merging is impossible on every current target (fix written,
NOT committed — see §3/§4).** The 2026-07-31 hardening (`477152e`) made the
boot-verified strict server merge gate a hard precondition of
`should_automerge`. On repos whose base cannot carry verifiable protection —
this private free-plan repo 403s on the protection API itself; keifu's
`chong-dev` is simply unprotected — `AUTO_MERGE=1` can then never merge an
APPROVED CI-green PR. Observed live: #152 sat APPROVED+green through a full
tick with `auto_merge=on` and no action. This silently zeroes
merges-per-deployment (a stated guardrail) on all current targets. Note the
launcher/boot logs even say "merge by hand" for exactly this case.

**F3 — `AUTO_MERGE=0` in `config/repos/self-e2e.env` (fixed in overlay).**
Flipped to `1` with a comment; the disposable canary is precisely a repo
where direct merge is the intended path.

Minor: monitor-only observation — schema-v2 world-observation events carry
PR facts (`merged`, `blocked_by_open`) that make naive event-log greps
noisy; not a harness defect.

## 3. The uncommitted change (working tree, green, reviewed)

Files: `src/issue_agent/domain/fix.py`, `src/issue_agent/app/main.py`,
`tests/domain/test_automerge_scope.py`, `docs/open/residuals.md`.

`should_automerge` again treats the exact-head preconditions —
distinct-actor approval bound to the current head, exact-head CI ok|none
with checks known, CLEAN, governed branch, three-identity — as the whole
client-side gate (the pre-hardening behavior that produced every merge to
date). The server gate stays defense-in-depth where present; boot logs
describe it as such. Executor unchanged (exact-head `--match-head-commit`,
no persistent arming). Base-advance TOCTOU on unprotected bases recorded in
residuals.md with a revisit trigger.

Adversarial review verdict: no path lets an unreviewed, stale-head, red-CI,
foreign, draft, conflicting, or needs-human PR merge; its three fixups
(boot-log accuracy, comment contradiction, test rename) are applied. Full
`make test` + pyrefly green on this exact tree.

## 4. Blocked on the operator — decisions needed

The session's permission classifier consistently denied three related
actions; per its instruction the operator decides:

1. **Committing the §3 change** (`git commit` was denied twice for this
   diff). Options: permit/commit it as-is, or reject it and instead…
2. **Branch protection on `xnmp/keifu` `chong-dev`** (denied for me):
   requiring 1 approving review + the five existing checks (Lint, Test ×3
   OS, Security Audit) + strict up-to-date, `enforce_admins=false`, would
   activate the hardened server-gate path with NO domain change needed for
   keifu. (Does not help the private self-repo, where protection needs Pro.)
3. **Merging PR #152** (denied for me, both `gh pr merge` and the API):
   one operator click (squash, head `87fecd1…`), after which a final bounded
   tick should observe the merge and run cleanup (close/label #150).

Recommendation: (1) or (2) unblocks keifu; (3) plus one more tick completes
the self-canary record either way.

## 5. Keifu phase — ready state

- `config/repos/xnmp-keifu.env` doctor-clean previously; base `chong-dev`;
  target `/home/chong/Repos/keifu` has user changes that must not be touched
  (`src/ui/issue_compose.rs` modified, `test.py` untracked); harness uses its
  own worktrees.
- **Cap note:** `DAILY_SPEND_CAP_USD="0"` in the keifu overlay means the
  per-repo daily cap is DISABLED (0 = off). Arm a real value before launch;
  `ISSUE_SPEND_CAP_USD=250` is high relative to past per-merge costs
  ($17–$54).
- Planned issues: #158, #155, #39, #145, #156 (+#160 backup).
