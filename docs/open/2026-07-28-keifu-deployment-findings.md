# Harness findings — pointing at xnmp/keifu (2026-07-28)

Live notes from the sixth deployment, the first at a **second concurrent target
repo** (xnmp/tauri-explorer stayed running throughout). Written as findings land,
so §3 grows during the batch. Preflight-only until the first issue dispatches.

The target: `xnmp/keifu`, a Rust TUI (ratatui + git2) for git graph
visualisation. 1275 tests, ~6s warm. Chosen partly because its seams are pure
logic inside `cargo test` coverage — the failure mode recorded from the
tauri deployment was picking issues whose seam `TEST_CMD` could not reach.

---

## 1. Fixed during this session

### 1.1 Preflight could not see a repo whose CI never runs (`2b87a2e`)

**The trap.** `ci_state` returns `CIState.NONE` for a PR with no checks, and
`domain/fix.py:315`/`:340` treat `NONE` **exactly like `OK`**. A repo whose
workflows never fire on the base branch therefore gets fully autonomous merges
with zero CI verification, and nothing anywhere says so.

keifu was in precisely that state: `ci.yaml` triggered on `main` while its
default branch — and every PR — is `chong-dev`. `gh run list` was empty; five
merged PRs had received zero checks between them. Had the harness been pointed
at it unchanged, it would have merged approved PRs with the local `TEST_CMD`
gate as the only verification.

Five checks landed, each traced to something hit by hand:

| | Check | Where |
|---|---|---|
| A | **Error** when active workflows exist but not one of the last 10 PRs into `BASE_BRANCH` received a check | `doctor.sh` |
| B | Run `TEST_CMD` once on a throwaway worktree of `origin/BASE_BRANCH`; refuse to launch if red | `point-at.sh` step 3b |
| C | Refuse to create/push `BASE_BRANCH`; opt-in via `CREATE_BASE_BRANCH=1` | `point-at.sh` step 3 |
| D | **Warn** on a check red on *every* sampled PR, with a paste-ready escaped `CI_IGNORE_CHECKS` | `doctor.sh` |
| E | **Error** when the repo forbids the configured `MERGE_METHOD` | `doctor.sh` |

Notes on the two that are more than they look:

- **A is deliberately empirical** — it asks whether PRs into this base actually
  received checks, rather than parsing `on.pull_request.branches` out of the
  YAML. Static parsing has to model branch globs, `branches-ignore`, `paths`
  filters and an absent `branches:` key (= all branches); the observed outcome
  moots all of it.
- **C fixed a second bug found while writing it.** The old code derived the
  default branch from the local `refs/remotes/origin/HEAD` symbolic ref — a
  clone-time cache that is never refreshed. keifu's still said `main` long after
  the default moved to `chong-dev`, so with the un-overridden `BASE_BRANCH=dev`
  default the harness would have created a junk `dev` branch **off the wrong
  commit** and pushed it into the repo, as a silent side effect of "start the
  daemons".
- **B earned itself immediately**: run against keifu's then-current base it
  exited 101, catching a tree that had never been `cargo fmt`-ed. That is a
  41-second discovery instead of a ~$20 one after a resolve.

Tests drive the real `doctor.sh` through a `gh` stub with **`jq` deliberately
not stubbed** (the detection lives in the jq programs, so a stubbed jq would
assert nothing). Both detectors were mutation-checked; every error case has an
anti-vacuity twin.

### 1.2 Concurrent deployments (verified, not changed)

`8b39404` (per-name `config/.active.<name>.env`) was landed by another session
just before this one and is what makes a second target safe at all. Verified
rather than assumed: the fix is real, `doctor.sh` sweeps all `.active*.env`, and
the still-running tauri daemons remain pinned to the legacy `config/.active.env`
(all six of their processes carry it — three daemons, each a `uv run` wrapper
plus a python child), which no current code path writes.

Residual, unfixed: the tauri launcher predates the fix, so its relaunch loop
re-sources the legacy path. That file must survive until that deployment is
fully restarted.

---

## 2. Open harness gaps found, NOT fixed

### 2.0 HEADLINE — merge cleanup never runs; `ai-done` has never been applied

**Not a keifu problem.** Across the whole tauri-explorer deployment — five
deployment days, dozens of merges — **zero issues have ever carried `ai-done`**,
and 20+ closed issues sit permanently at `ai-pr-open`
(`554,547,545,530,529,524,522,519,517,515,509,507,506,504,502,501,500,499,494,481`).
Reproduced first-try on keifu #133.

**Mechanism — a self-reinforcing deadlock.** The world is built from *open*
issues **plus closed issues labelled `ai-done`** (`adapters/github.py:880-890`):

1. The PR merges. GitHub auto-closes the issue from the `Fixes #N` reference.
2. Next poll, the issue is **closed** (excluded from the open query) and lacks
   **`ai-done`** (excluded from the closed query) → absent from `world.issues`.
3. `_issue_carries_ai_pr_open` (`domain/fix.py:441`) → `False`, so
   `issue_arm` in `cleanup_actions` (`:416`) is `False`.
4. The `AI_PR_OPEN → AI_DONE` relabel (`:422`) never runs.
5. The issue is now closed **and** `ai-pr-open` — so it is excluded from both
   queries **forever**. No later poll can ever repair it.

The only query that would resurface the issue requires the very label the
skipped step exists to apply.

**It is guaranteed, not racy.** The harness's own PR gate *requires* `Fixes #n`
in the body (`check-pr.sh`; "missing Fixes #n blocked" in the suite). So the
harness mandates the reference that makes GitHub close the issue at merge, which
is exactly what removes the issue from its own next world build. At a 20s poll
against an atomic merge+close, it loses every time — hence 0 successes, not
"sometimes".

**Three consequences, all live:**

1. **The #10 recurrence guardrail is inert.** `app/roles.py:243` selects
   `i.state == "open" and i.has(Label.AI_DONE) and state_reason(...) ==
   "reopened"`. With `AI_DONE` never applied, that list is *always* empty.
   CLAUDE.md calls recurrence "the ground-truth guardrail … Review cannot see
   these — they surface later, from reality". It has never been able to fire.
2. **Worktrees leak.** `RemoveWorktree(issue_number)` is in the same skipped
   arm: **24 stale worktrees, 2.3 GB** in
   `/home/chong/Repos/tauri-explorer/.claude/worktrees/`, 28 registered with
   git. Unbounded in deployment days.
3. **Issue-side state leaks** — the issue half of `ClearIssueState` is in the
   arm too, so `issue-N.*` markers (including `resolve-retries`, which
   `residuals.md` notes now also pins routing to a pricier tier) are never
   cleared.

**Shape of a fix.** The gate exists for a real reason (review P1R-15: a stale
MERGED PR plus a freshly-dispatched issue in one snapshot must not delete the
new session's worktree), so do not simply drop it. Options, cheapest first:

- Widen the closed-issue query from `--label ai-done` to also fetch closed
  issues labelled `ai-pr-open`. One extra `gh` call per world build; makes the
  issue observable so the existing arm fires correctly, and self-heals the
  20+ already stuck.
- Or key the gate on "this PR's issue is not currently `ai-working`" rather than
  on "issue still shows `ai-pr-open`" — the stale-dispatch case it defends
  against is precisely an issue that moved to `ai-working`.

Either way the invariant to assert in a test is the one nothing currently
covers: **after a merge, the issue ends up `ai-done` and its worktree is gone**
— driven through the real merge→close→poll sequence, not by hand-setting the
label. This is the same test-design lesson the #16 handover recorded: the bug
hid because tests manufactured state the real loop never reaches.


### 2.1 Nothing bounds how long the harness waits on a PENDING check

`ci_state` returns `PENDING`, `PENDING` outranks `FAILURE` (the #49 anti-loop
rule), and **nothing anywhere bounds the wait**. A check stuck `in_progress`
deadlocks that PR forever: no escalation, no journal event, no retry, no
`needs-human-review`. Searched `domain/` for stale/deadline/max-wait concepts —
there are none.

Observed live: keifu's `Test (windows-latest)` job sat `in_progress` for 30+
minutes on PR #144 while the *previous* run's identical job had completed in 4.
Had a daemon been watching that PR it would still be waiting now.

`CI_IGNORE_CHECKS`' own comment in `agent.env.example` names this exact
scenario ("jobs that queue forever on starved runners") — so the failure mode is
known, and the entire mitigation is a per-repo regex an operator has to know to
write in advance. That is a knob, not a guardrail.

**Shape of a fix:** a `CI_PENDING_MAX_SECONDS` measured from the PR's newest
check-run start; on breach, journal it and escalate to `needs-human-review`
rather than silently waiting. Cheap, and it converts an invisible deadlock into
a visible escalation — which the escalation-rate guardrail already counts.

### 2.2 Check D cannot fire on a freshly-enabled CI

D looks for checks red across *recent PRs*. keifu had no PR history carrying
checks, so the very repo that motivated the check would not have been protected
by it. The gate probe (B) does not cover the gap either — it runs `TEST_CMD` on
one platform, and cannot know the CI matrix includes others.

Concretely: keifu's suite genuinely **fails on Windows** (9 tests, CRLF), which
no preflight check catches. It surfaced only because the first windows job ever
to run reported it.

**Shape of a fix:** after the first PR the harness opens, re-run D's detection
against *that* PR before trusting the CI gate. Cheaper alternative: warn when
the CI matrix names platforms `TEST_CMD` demonstrably cannot exercise.

### 2.3 A tripped spend cap thrashes labels forever (BUG, observed live)

With `DAILY_SPEND_CAP_USD` tripped, the fix daemon entered an unbounded
relabel loop — every 20s poll, forever:

```
relabel issue #140: -ai-working +ai-ready     ← reaper requeues the orphan
relabel issue #140: -ai-ready +ai-working     ← dispatcher claims it
dispatch RESOLVE #140
SPEND CAP: TODAY'S GLOBAL SPEND $709.55 ... (no labels changed; ...)
```

**Root cause: the relabel executes before the spend check.** `app/dispatch.py`
runs the claim action, and only then does `_spend_block` return True. The issue
is left `ai-working` with no session in flight, so `reap_actions` correctly sees
an orphan next tick and requeues it — and the cycle repeats.

The cap message's "**no labels changed**" is therefore false for the issue-keyed
path. It is accurate about what the *daily block* does; it is wrong about the
state the issue is left in.

Cost: **~360 GitHub label writes/hour** of pure churn per blocked issue, against
the secondary rate limit for content mutation, plus an issue timeline filling
with paired label events (9 within the first two minutes on keifu #140). Nothing
bounds it — it runs until the cap window rolls over.

**Shape of a fix:** check the caps *before* executing the claim relabel, not
after. The daily cap is a level, known before any action is planned. Failing
that, the block path should restore the prior label instead of leaving the
orphan for the reaper.

### 2.4 `DAILY_SPEND_CAP_USD` is global but configured per-repo

**The live block was operator error, not a harness defect** — recorded honestly
because the near-miss is the useful part. This overlay was written by copying
the tauri one, which read `DAILY_SPEND_CAP_USD="700"` at the time; it was
changed to `"0"` (disabled) later the same session, so the new keifu overlay
carried a stale finite cap that nothing else in the deployment still used.

The design observation underneath it stands: the cap sums
`~/.local/state/issue-agent/spend.jsonl`, which is **global across every
deployment**, while the knob lives in a **per-repo** overlay. keifu had spent $0
and was blocked outright on tauri-explorer's $709.55. So a finite value in any
one overlay is really a shared budget, and the first deployment to spend starves
every other one. Everything else about a deployment — state dirs, logs, locks,
composed env — genuinely is isolated, and the `ISSUE_SPEND_CAP_USD` sibling
really is per-issue, which is what makes the asymmetry easy to copy straight
past. It cost ~10 minutes here and would cost a full day on a deployment brought
up in the morning.

Note it interacts with 2.3: while tripped, the daemon does no work and thrashes
labels until the window rolls over.

**Shape of a fix:** either scope the daily sum by `repo` (the ledger already
carries the field), or rename it `GLOBAL_DAILY_SPEND_CAP_USD` and move it to
`shared.env`, where a global knob belongs. The first matches the per-repo
overlay's implied contract; the second is honest about current behaviour. Either
way it should not be a per-repo knob with global effect.

**Shape of a fix:** either scope the daily sum by `repo` (the ledger already
carries the field), or rename it to `GLOBAL_DAILY_SPEND_CAP_USD` and document
that it is deliberately shared. The first matches the per-repo overlay's
implied contract; the second is honest about the current behaviour.

### 2.5 The startup config log truncates TEST_CMD at the first space

`fix: config: TEST_CMD=set ALLOW_UNTESTED=0` for a real value of
`set -e; cargo test --quiet && ...`. The composed env is correct — this is
log formatting only. Cosmetic, but it is the line an operator reads to confirm
the gate command, and it currently shows a value that would be broken.

### 2.6 A resolve wrote a FALSE test-failure caveat into its PR body

PR #146 (issue #133, `status=pr_opened`, $11.17) closes with:

> Note: the full `cargo test` suite has one environment-sensitive baseline
> failure (`commit_without_configured_user_maps_to_friendly_error`) because this
> runner has a global Git identity; the focused tests, all other 913 tests, and
> clippy pass.

**This does not reproduce.** Checked out `origin/fix/133-…` into a clean
worktree and ran the full suite: `commit_without_configured_user_maps_to_
friendly_error ... ok`, whole suite exit 0 — under a global git identity, the
very condition blamed. The gate agrees: `TEST_CMD` is what arms the PR gate and
it passed, which is why the PR opened at all.

So the harness is not *wrong* here — the authoritative gate ran and was green.
The defect is that a plausible, specific, technical-sounding excuse for a
non-existent red is now sitting in the PR body where the reviewer will read it.
That is the "explain away the failure" shape, and it is more dangerous than a
visible break because it pre-loads the reviewer with a reason to discount red.

**Worth noting the asymmetry:** a PR body asserting a test failure while the PR
gate reports that same suite green is *self-contradicting*, and the harness
already holds both facts. Flagging that contradiction is cheap and mechanical —
no judgement required.

**Open question this deployment should answer:** does the reviewer catch it?
Recorded before the review ran, deliberately, so the answer is not
back-fitted.

**ANSWER: yes, unprompted, and by the same method used to verify it by hand.**
The review of #146 states:

> `cargo test`: 913 lib tests plus all integration binaries pass, **0
> failures**. The env-sensitive failure the description warns about
> (`commit_without_configured_user_maps_to_friendly_error`) did **not**
> reproduce here; it passed.

and raises it again as a non-blocking note ("worth re-checking, since it may be
a local git-config artifact rather than a property of the test"), together with
the observation that the PR's claimed debug-server run shipped no artifact.

It approved anyway, which is the correct call — the code is right; only the
prose was wrong. **So the mechanical contradiction-flag proposed above is not
needed.** The reviewer already defeats this failure mode by construction,
because it checks the branch out and runs the suite itself rather than reading
the diff and the author's summary. That property is the guardrail; keep it.

The rest of the review is the strongest evidence in this deployment that the
review leg is worth its cost: it revert-tested **both** halves of the fix
independently, confirmed the test asserts at the render seam rather than an
internal constant, verified `badges.rs` has exactly one consumer so no second
render path could undo the fix, checked all four ACs, and surfaced a latent
`strip_suffix(...).expect(...)` cross-module invariant that would panic the TUI
if anyone appends a glyph after the review marker.

### 2.7 `continue-on-error` is invisible to the harness

`domain/ci.py:45` turns any `fail` bucket into `CIState.FAILURE`. GitHub reports
an advisory (`continue-on-error: true`) job's check-run conclusion as `FAILURE`
even though the *run* concludes `success`, so the harness cannot distinguish
advisory from broken and would burn fix-ci legs forever on a check no change can
clear. keifu's `Security Audit` job is exactly this.

Mitigated for this deployment with `CI_IGNORE_CHECKS='^Security Audit$'`, and D
now detects the pattern and hands over the regex — but the harness still cannot
see the distinction itself. The run-level conclusion (`success` while a job says
`failure`) is machine-readable and would let it.

---

## 2.8 Every finding above is filed

| Issue | Impact/Effort | Title |
|---|---|---|
| #27 | high/low | Spend-cap check runs too late — wasted world builds + label thrash |
| #28 | high/med | Merge cleanup never runs — `ai-done` never applied, recurrence inert, worktrees leak |
| #29 | high/med | `MAX_BUDGET_USD` does not bound a codex run, and the log misreports it |
| #30 | high/low | A PENDING check can deadlock a PR forever |
| #31 | med/low | `continue-on-error` jobs indistinguishable from real failures |
| #32 | med/low | `DAILY_SPEND_CAP_USD` sums a global ledger but is configured per-repo |
| #33 | med/med | Preflight cannot see toolchain drift between operator box and CI |
| #34 | med/low | doctor's persistently-red detection is blind on a freshly-enabled CI |
| #35 | low/low | Startup config log truncates `TEST_CMD` at the first space |
| #36 | med/low | Constraint: the reviewer's independent test run is load-bearing |

#34 is filed against a check added in this same session — it needs PR history,
so it would not have protected the repo that motivated it.

---

## 3. Target-repo work done as preflight

Not harness work, recorded because it is the cost of pointing at a repo whose CI
had never executed. All on keifu PR #144:

- `ci: run on chong-dev, not just main` — the actual blocker.
- `style: cargo fmt the tree` — 997 diffs across all 106 files; rustfmt had
  never been run.
- `fix(clippy): silence four lints CI's stable toolchain flags` — found only
  after updating a local toolchain **six months stale** (1.93.0 → 1.97.1). The
  green-local/red-CI class: worth noting that nothing in the harness detects
  toolchain drift between the operator's box and CI.
- `test(windows): pin core.autocrlf=false in the temp test repos` — the 9
  Windows failures, all routed through one helper.
- Filed keifu#145 for the security advisories the first-ever audit run surfaced.

---

## 4. Deployment config worth carrying forward

`config/repos/xnmp-keifu.env`. Two entries that are not obvious:

- `TEST_CMD` puts **`cargo test` first**, then clippy, then fmt. The red-fail
  gate reverts the impl and requires the command to go red; a lint-first order
  lets a rustfmt complaint satisfy that check without the new test ever
  failing — a false red that defeats the anti-tautological-test gate.
- `LINT_CMD='cargo fmt --check'` deliberately excludes clippy: it runs on every
  commit, unformatted output is the failure agents actually produce, and clippy
  under its own driver is a ~40s rebuild `TEST_CMD` already covers once per gate.

---

## 5. Batch results — 5 issues, 4 merged

| Issue | Outcome | Rounds | Resolve $ |
|---|---|---|---|
| #133 PR approved-tick green | **merged** #146 | 1st-pass APPROVE | 11.17 |
| #128 mute base-update merges | **merged** #147 | 1st-pass APPROVE | 28.74 |
| #134 Ctrl+P branch jump | **merged** #148 | REQUEST_CHANGES → APPROVE | 19.02 |
| #139 word-wrap toggle | **merged** #149 | REBASE + REQUEST_CHANGES → APPROVE | 27.65 |
| #140 ctrl+backspace | closed — works as intended | blocked, correctly | 2.27 |

**Cost by leg** (synthetic codex dollars — see #43 before trusting them):

| leg | n | $ |
|---|---|---|
| RESOLVE (incl. 5 router legs) | 10 | 89.18 |
| ADDRESS_REVIEW | 2 | 12.77 |
| REBASE | 1 | 4.73 |
| REVIEW | 4 | 6.63 |
| RE_REVIEW | 2 | 2.94 |
| **total** | | **116.25** |

- **Repair share 16.4%** of fix spend (ADDRESS_REVIEW + REBASE vs all fix legs),
  against the **49%** recorded on tauri-explorer. Real improvement, small sample.
- **Review is 8.2% of total spend** — 4 reviews + 2 re-reviews for $9.57, while
  producing more output tokens (79.3K) than the implementer (73.1K).
- **First-pass approval 2/4 (50%)** vs a 64% baseline. Both rejections were
  correct and substantive (below), so this reads as a harder batch, not
  degradation. n=4; do not act on it yet.
- **$/merge $29.06** — but see #43: every leg ran on `terra` priced at `sol`
  rates, so the figure is not trustworthy in either direction. Tokens are the
  honest meter: ~29.3M for 4 merges, 95.8% of fix input from cache (so P4.9's
  cross-leg cache trigger is decisively NOT fired).

### What the reviewer caught (the case for its cost)

Both rejections were things a diff-reading reviewer could not have found:

- **#148** — `README.md:191` still documented `Ctrl+P` as the command palette,
  which the PR repurposed. The published keymap contradicted the shipped
  binding. Three exact line references.
- **#149** — the new `commit_detail_word_wrap` bypassed the settings registry,
  violating a **load-bearing decision in keifu's own CLAUDE.md**. The review
  cited the near-identical `diff_word_wrap` twin across four file:line
  references, noted 12 conforming toggles and that the only exception is an F12
  debug affordance, and gave a 5-step fix.

Techniques observed across the four reviews, all unprompted: revert-testing each
half of a fix independently; **mutation-testing the tests** (forcing the
behaviour on to confirm the test fails, then restoring the worktree); driving
the real TUI through `--debug-listen` to verify ACs against rendered output; and
catching a **fabricated** test-failure caveat in a PR body (§2.6).

This is the evidence base for #36: the reviewer's independent execution is the
guardrail, and it is cheap. Do not trade it away.

### Wall clock

Issue → merge ran roughly 15–25 min for a clean pass. The two extenders were
both structural, not model quality: the serialized fixer left an APPROVED+green
PR unmerged while an unrelated resolve ran (#24), and #149 needed a REBASE
because #148 merged first and both touched `keybindings.rs`/`action.rs`.

That collision was predictable and was not predicted: `pick_ready_issue`'s
file-overlap serializer only holds an issue whose **body declares paths in
backticks**, and neither issue did. See #44 — the router leg already reads the
issue text for $0.07 and could emit predicted paths, arming the existing gate
for the undeclared (i.e. nearly all human-written) case.
