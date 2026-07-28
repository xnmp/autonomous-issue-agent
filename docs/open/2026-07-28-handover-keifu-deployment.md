# Handover — keifu deployment (the SECOND concurrent target, 2026-07-28)

Written at the end of the session that pointed the harness at **xnmp/keifu**
while the tauri-explorer deployment kept running. Batch result: **4 of 5 issues
merged, 1 correctly closed**. 14 harness issues filed or materially advanced.

---

## 1. Read this first — three things will confuse you

**(a) TWO handovers were written for 2026-07-28 by two different sessions.**
This one covers **keifu**. `2026-07-28-handover-sixth-deployment.md` covers the
**tauri-explorer** batch that ran in parallel. Both are accurate; they describe
different deployments. **Do not add their cost numbers together** — they draw on
one shared global ledger, so summing double-counts nothing but comparing them
as if they were sequential days will mislead.

**(b) DO NOT DELETE `config/.active.env`** (the legacy, name-less one). The
tauri launcher was started *before* `8b39404` (per-name composed env), so its
relaunch loop still re-sources that exact path on every hot reload. No current
code path writes it, so it is safe where it is — and fatal to remove. It becomes
deletable only after that deployment is fully restarted through `point-at`.

**(c) The harness repo was being committed to by other sessions all night.**
`7098f47`, `8b39404`, `665f80e`, `2a16a9d` and issues **#37–#42** are not mine.
This matters twice over:

- The hot-reload watch fired repeatedly mid-batch. **It behaved correctly** — a
  concurrent commit landed during a paid resolve and did *not* kill it (reload
  defers to a tick boundary, exactly as `adapters/reload.py` documents). That
  guarantee is now field-verified, not just asserted.
- **Check for duplicates before filing.** My router-leg finding was already
  #40 by the time I wrote it up; I added evidence to that instead. Do the same.

`c2a0b5f` (the findings doc) is currently **unpushed** on `main`.

---

## 2. What landed on the harness

`2b87a2e` — **five preflight checks**, every one traced to something hit by hand
during this deployment, with tests that drive the real `doctor.sh`:

| | Check | Where |
|---|---|---|
| A | ERROR when active workflows exist but not one of the last 10 PRs into `BASE_BRANCH` got a check | `doctor.sh` |
| B | Run `TEST_CMD` on a throwaway worktree of `origin/BASE_BRANCH`; refuse to launch if red | `point-at.sh` 3b |
| C | Refuse to create/push `BASE_BRANCH`; opt-in via `CREATE_BASE_BRANCH=1` | `point-at.sh` 3 |
| D | WARN on a check red on *every* sampled PR, with a paste-ready escaped regex | `doctor.sh` |
| E | ERROR when the repo forbids the configured `MERGE_METHOD` | `doctor.sh` |

The one that matters most is **A**, because `ci_state` returns `CIState.NONE`
for a PR with no checks and `domain/fix.py:315`/`:340` treat `NONE` **exactly
like `OK`**. keifu's CI triggered on `main` while every PR targets `chong-dev`,
so `gh run list` was empty and five merged PRs had zero checks between them.
Pointed at that unchanged, the harness would have auto-merged approved PRs with
**no CI verification at all**, silently.

**C fixed a second bug found while writing it**: the old code read the default
branch from local `refs/remotes/origin/HEAD`, a clone-time cache that is never
refreshed. keifu's still said `main`. With the un-overridden `BASE_BRANCH=dev`
it would have created a junk `dev` branch **off the wrong commit** and pushed it
into the repo as a side effect of "start the daemons".

`7aa396e` — documents the new knobs (`CREATE_BASE_BRANCH`, `GATE_PROBE`,
`GATE_PROBE_TIMEOUT`) and the advisory-job case for `CI_IGNORE_CHECKS`.

`c2a0b5f` — `docs/open/2026-07-28-keifu-deployment-findings.md`, the full
evidence base. **Read that before acting on any issue below**; this handover is
the index, that is the substance.

---

## 3. Target-repo preflight — ~2 hours, and worth knowing about

keifu needed real work before the harness could run at all. All in PR **#144**:

1. **CI had never executed on the repo.** Default branch is `chong-dev`, every
   PR targets it, the workflow triggered only on `main`. Fixed by adding
   `chong-dev` to both triggers — and because GitHub reads the workflow from the
   PR head for `pull_request` events, that PR gated itself.
2. **The tree had never been `cargo fmt`-ed** — 997 diffs across all 106 files.
3. **Four clippy lints**, invisible locally because the operator toolchain was
   **six months stale** (1.93.0 vs CI's 1.97.1). Green-local/red-CI — see #33.
4. **Nine Windows test failures** (CRLF; every one shells out to real `git`).
   One line in `tests/common/mod.rs`: pin `core.autocrlf=false` on the temp repo.

Filed keifu#145 for the advisories the first-ever audit run surfaced.

**Budget ~2h for this on any repo whose CI has never run against the base
branch.** Check A now catches the headline case at preflight in one API call.

---

## 4. Batch result

| Issue | Result | Rounds | Resolve $ |
|---|---|---|---|
| #133 approved-tick green | merged #146 | 1st-pass APPROVE | 11.17 |
| #128 mute base-update merges | merged #147 | 1st-pass APPROVE | 28.74 |
| #134 Ctrl+P branch jump | merged #148 | REQUEST_CHANGES → APPROVE | 19.02 |
| #139 word-wrap toggle | merged #149 | REBASE + REQUEST_CHANGES → APPROVE | 27.65 |
| #140 ctrl+backspace | closed, works as intended | correctly blocked | 2.27 |

**#140 is a success.** The agent found the feature already implemented, refused
to open a PR because its regression test passed on the base (which would have
defeated the red-fail gate), and asked a precise question. The operator
confirmed it works. $2.27 to correctly decline is the cheapest outcome in the
table.

### Cost by leg (synthetic codex dollars — read #43 first)

| leg | n | $ |
|---|---|---|
| RESOLVE (incl. 5 router legs — see #40) | 10 | 89.18 |
| ADDRESS_REVIEW | 2 | 12.77 |
| REBASE | 1 | 4.73 |
| REVIEW | 4 | 6.63 |
| RE_REVIEW | 2 | 2.94 |
| **total** | | **116.25** |

- **Repair share 16.4%** of fix spend, against **49%** on tauri. Real
  improvement; n is small.
- **Review is 8.2% of total** — $9.57 for 4 reviews + 2 re-reviews, while
  producing **more output tokens (79.3K) than the implementer (73.1K)**.
- **First-pass approval 2/4 (50%)** vs a 64% baseline. Both rejections were
  correct and substantive. n=4 — do not act on this yet.
- **$/merge reads $29.06 and should not be trusted in either direction** (#43).
  Tokens are the honest meter: **~29.3M for 4 merges**, **95.8% of fix input
  served from cache** — so P4.9's cross-leg cache trigger is decisively NOT
  fired.

---

## 5. Issues filed — recommended order

| # | Impact/Effort | Why this order |
|---|---|---|
| **#28** | high/med | **Do first.** Merge cleanup has never run on ANY deployment: `ai-done` never applied → the #10 recurrence guardrail is structurally inert, and one worktree leaks per merged issue (24 / 2.3GB on tauri). Self-reinforcing: the closed issue is excluded from both world queries forever. |
| **#43** | high/low | Codex cost is ledgered at one flat price regardless of routed model. Everything ran `terra` priced at `sol`. **Routing is unmeasurable on its own instrumentation** — which the repo's "must say which metric it moves" rule requires. Fix before trusting any routing number. |
| **#29** | high/med | `MAX_BUDGET_USD` does not bound a codex run at all; the boot log reports `fix_budget=$20.00` as if it does. #128 ran to $28.74. The warning that would say so is suppressed exactly when pricing is configured correctly. |
| **#45** | high/low | doctor has no check on `REVIEW_ADOPT_UNLABELED=1` + `AUTO_MERGE=1` against third-party PRs = auto-merging PRs the harness didn't author. Already happened once (dependabot, tauri, 2026-07-26). |
| **#27** | high/low | Spend-cap check runs after the claim relabel → label thrash + ~2,520 wasted GraphQL points/hour while capped. Mechanical fix. |
| **#30** | high/low | No deadline on a PENDING check; one stuck job deadlocks a PR permanently, silently. |
| #31, #32, #33, #34, #35, #36, #44, #46 | med–low | See the findings doc. #34 is filed against a check added *this* session (it needs PR history, so it could not have protected keifu). #36 is a **constraint**, not a fix. |

Also advanced, not filed fresh: **#40** (router legs journalled as `RESOLVE` —
added the quantified corruption: median reads $1.17 vs a real $19.02, which can
trip the routing trigger by itself) and **#24** (flagged that its concurrency
safety precondition leans on a serializer that is near-inert; suggested #44 as a
prerequisite).

---

## 6. The reviewer is the strongest component — protect it

Across four reviews, unprompted: revert-tested each half of a fix independently;
**mutation-tested the tests** (forced the behaviour on, confirmed the tests fail,
restored the worktree); drove the real TUI through `--debug-listen` to verify
ACs against rendered output; caught a **fabricated** test-failure caveat in a PR
body (a resolve invented an "environment-sensitive baseline failure" that does
not reproduce); caught a stale published keymap in `README.md`; and caught a
violation of **keifu's own CLAUDE.md** load-bearing decision (a new setting
bypassing the settings registry), citing the conforming twin across four
file:line references.

The last three are invisible to a diff-reading reviewer. It catches them because
it **checks the branch out and runs the suite itself**. That property — not the
model tier — is the guardrail. #36 records it as a constraint on future
review-cost work.

---

## 7. Working notes

- **The keifu overlay is gitignored** (`config/repos/xnmp-keifu.env`). Two
  entries are non-obvious and would be got wrong on a rewrite: `TEST_CMD` puts
  **`cargo test` FIRST** (a lint-first order lets a rustfmt complaint satisfy
  the red-fail gate without the new test ever failing — a false red), and
  `LINT_CMD` is **fmt-only** (runs per commit; clippy is a ~40s rebuild
  `TEST_CMD` already covers once per gate). Also `CI_IGNORE_CHECKS='^Security
  Audit$'` and `DAILY_SPEND_CAP_USD=0`.
- **`DAILY_SPEND_CAP_USD` is a cross-repo pool** (#32). A stale `700` copied
  into the new overlay blocked keifu outright on tauri's $709.55 before it did
  any work. Copying a sibling overlay's spend block is how this recurs.
- **Diagnosing a TUI key binding**: `cat -v` is useless (the tty line discipline
  eats the erase char in canonical mode). keifu ships a **debug key display on
  F12** showing the exact `KeyEvent` the app receives. Reach for the target
  repo's own affordances — `AGENTS.md` documents them.
- **Monitor filters must exclude the repeating block lines** (`SPEND CAP`,
  `relabel issue`, `dispatch RESOLVE`) or a tripped cap fires an event every 20s
  and the monitor gets auto-stopped, losing the watch.
- Carried and still true: `pgrep -f` needs a char-class breaker under the rtk
  hook; the daily spend window is per **UTC** day.

---

## 8. Open, unresolved, honest

- **Unexplained**: the review daemon logged **6 hot-reloads against fix's 2**. It
  was healthy and reviewed four PRs correctly, but I never explained the
  discrepancy. "It worked anyway" is not a diagnosis — worth 10 minutes next
  session.
- **Left deliberately as evidence**: keifu #133 and #128 are closed-but-
  `ai-pr-open` with `issue-133`/`issue-128` worktrees still present. That is #28
  reproducing live. Clean them when #28 lands, not before.
- **6 daemons still running** against keifu (plus 6 against tauri). Stop with
  the `point-at` foreground process, or leave them — nothing is queued.
- **#34 is a bug in a check added this session.** Filed against myself rather
  than quietly left; the detector needs PR history, so it would not have
  protected the repo that motivated it.
