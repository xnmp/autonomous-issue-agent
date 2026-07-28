# Handover — keifu batch 2 + self-hosting setup (2026-07-28, evening)

Third session on 2026-07-28. **Do not sum this batch's cost with the other two
handovers of the same date** (`-keifu-deployment.md`, `-sixth-deployment.md`,
`-seventh/-eighth`) — they draw on one shared global ledger and ran partly
concurrently.

Two pieces of work: a **5/5 keifu batch** (07:26–09:37Z), and the **prep that
makes the harness able to fix itself** (evening). The second is unlaunched and
is the first thing the next session should consider running.

---

## 1. Batch result — keifu, 5 merged + 2 already-done

| issue | PR | path |
|---|---|---|
| #119 signature-status residue | #150 | 1st-pass APPROVE |
| #131 brighter trunk colour | #151 | 1st-pass APPROVE, resolved on **luna** |
| #127 merged-branch disconnect | #152 | 1st-pass APPROVE |
| #129 squash-merge hide delay | #153 | REQUEST_CHANGES → address → approve |
| #135 draft PRs grey | #154 | blocked → answered → RC → address → approve |
| #120, #121 | — | **closed as already implemented** (~$2 each) |

**$85.76 total → $17.15/merge.** First-pass 3/5. Zero FIX_CI and zero REBASE
legs. Cost mix: RESOLVE $48.93 (8) · ADDRESS_REVIEW $26.69 (2) · REVIEW $7.73
(5) · RE_REVIEW $2.38 (2) · ROUTE $0.03 (8). Repair share **34%** — higher than
keifu batch 1's 16%, from two address legs on a 5-issue batch; n is small.

**#120/#121 are the interesting outcome.** keifu commit `9123511` (2026-07-22)
implemented #119/#120/#121 *seconds after those issues were filed*, and nobody
closed them. Both resolve legs discovered this, verified with focused test runs
(`cargo test merged_lane`), commented with the evidence, and closed the issues
for ~$2 each. Correct and cheap — but it cost two batch slots, and I backfilled
with #127/#129. **Before picking a keifu batch, check the issue against recent
`git log` for a commit naming it.** #15 (steward grooming) is exactly this.

### Reviewer quality — still the strongest component

Both rejections were substantive and neither was diff-reading:
- **#129**: confirmed the root-cause diagnosis, then caught the fix putting gh
  polling on the 60s auto-fetch interval *permanently* — proved it by running
  `git fetch` twice and `stat`-ing `FETCH_HEAD` to show a no-op fetch rewrites
  it, and cited the invariant the change contradicted 20 lines up in the same file.
- **#135**: **pixel-sampled the agent's own evidence PNG** (sampled the badge
  row, tabulated fg colours) to show the badge was not actually muted, and
  caught the draft flag being read from the wrong GitHub field.

---

## 2. What landed on the harness

- **PR #59** (merged `1258eab`) — six quick wins closing **#31, #52, #54, #55,
  #56, #57**, five of them found during this very batch. Authored by a parallel
  session; I ran the full suite + import-linter contracts on its head by hand
  before merging (the repo had no CI at that moment) and merged it.
- **`96a0dbc`** — six **pending ledger rows** for those changes, evidence specs
  drawn from the diff. PR #59 shipped without them; the ledger rule is that rows
  land in the same PR as the behaviour change. Worth enforcing next time.
- **CI, finally** (`de86d2a` + `3818014`) — `.github/workflows/ci.yml` runs
  `make test` on push/PR to `main` and `dev`. **Green.**
- **Labels** — new `feature` label (+ existing `feature:*` area labels) applied
  to #11, #14, #15, #24, #44, the new-capability issues.

### The CI run exposed a real bug, still open

`make test-domain` calls plain `uv run pytest tests/domain/`, but
`tests/domain/test_convergence.py` imports `app.roles` → `adapters.governor` →
`tree_sitter`. It only passes from an already-synced project venv. On a clean
checkout it fails at *collection*. I worked around it in the workflow
(`uv sync --extra sdk` step, with the reason in a comment) — **the Makefile
target is still mislabelled "pure" and under-declared.** Not filed; file it.

---

## 3. Issues filed this session

| # | why |
|---|---|
| **#54** | `ci_pending_deadline` re-fires every tick — the operator cannot reverse a stall escalation while the check still runs. Observed as a live label tug-of-war on PR #150. **Fixed in #59.** |
| **#55** | Reply-to-unblock is **identity-dead** when `FIX_BOT` is the operator's own login: the filter excludes the fixer's login *and* all bots, so no identity this deployment owns can unblock an issue. Retroactively explains keifu #140 last batch. **Fixed in #59.** |
| **#57** | The already-implemented/no-PR terminal leaks its worktree + local branch (#120/#121 both; cleaned by hand). **Fixed in #59.** |
| **#52** | Role logs have no timestamps. Found while closing the previous handover's open question. **Fixed in #59.** |

**The previous handover's "6 hot-reloads vs 2" mystery is closed: not a bug.**
Real counts were 19 vs 20. Reload is commit-gated and checked once per tick
*between* ticks, so commits landing during fix's long paid legs coalesce into one
reload while idle review catches them ~1:1. The "6 vs 2" was a mid-day snapshot
of a never-rotated log. That diagnosis needed journal side-channels because the
logs had no timestamps — hence #52.

---

## 4. Self-hosting — READY TO LAUNCH, never run

Everything below is done and verified. Nothing has been started.

| piece | state |
|---|---|
| CI on the harness repo | ✅ green on `main` |
| `dev` base branch | ✅ created off main, fast-forwarded to the green tip |
| Target clone `~/Repos/autonomous-issue-agent-target` | ✅ |
| `config/repos/self.env` (gitignored) | ✅ |
| `xnmp-bot` write access on the harness repo | ✅ operator did it; doctor confirms |

Launch with `make point-at REPO=self`, then label a harness issue `ai-ready`.

**Why CI had to come first** (the decisive argument, don't lose it):
`domain/fix.py` treats `CIState.NONE` — a PR with zero checks — exactly like
`OK`. The harness repo had no workflows at all. Pointed at itself in that state
it would have auto-merged approved PRs into its own codebase with **no
verification whatsoever**. Same failure preflight check A was built to catch on
target repos.

**The three separations that make self-hosting safe** (all documented at length
inside `self.env`, because that file is gitignored and a rewrite would lose them):

1. **A second clone as `TARGET_DIR`, never the checkout the daemons run from.**
   The daemons resolve `plugin/` and `AGENT.md` by walking `__file__` from their
   *own installed* checkout (`app/main.py:125,141,165,360`), independent of
   `TARGET_DIR`. So a fix leg rewrites harness code in clone B while the process
   executing that code reads clone A.
2. **`BASE_BRANCH=dev`, human promotes `dev → main`.** The daemons hot-reload on
   the main checkout's HEAD, so an auto-merge to `main` would restart *every*
   deployment on code no human reviewed. Worst case of a bad self-fix is a red
   `dev`.
3. **`TEST_CMD='make test'`** — the full suite incl. import-linter architecture
   contracts. This is what makes harness issues legal batch candidates under the
   "seam inside TEST_CMD coverage" rule.

Also: `FIXER_RUNTIME=claude`, deliberately not codex — a codex-specific
regression shouldn't be able to both cause and mask its own failure while the
runtime under test is the one shipping the change.

**Good first candidates** (mechanical, inside `make test`): **#46** steward
polls with nothing to act on · **#33** toolchain-drift preflight · **#41** verify
the escalation ladder's triggers are reachable. Keep #58, routing and steward
features for interactive work — they touch shared seams.

### Two design questions, answered — don't relitigate

**Should the manager resolve quick wins at startup?** No. It breaks the boundary
that makes the manager safe unattended (it supervises deployments *while* they
run, so a harness edit hot-reloads the daemons under it), skips the only quality
path the harness has (the manager is the one component with no reviewer), and
corrupts the batch readout the Direction section requires. The good version of
the instinct: at startup the manager **feeds** the self-deployment — triage the
backlog and label 1–2 quick wins `ai-ready`. Labelling is additive and
reversible; the fix still goes through resolve → review → CI → merge. Operator
said "leave it" for now; it belongs on #11 when someone picks that up.

**How does verification show up in the dashboard once the harness fixes itself?**
Not as a field on issues. An issue closes when code lands; a ledger row resolves
when a *deployment* proves it — PR #59's six fixes are closed and all six rows
are pending. Encoding verification as issue state would mean keeping issues
open, and an open `ai-ready` issue gets re-dispatched by the fixer. The real gap
is that the dashboard is journal-only and can't see the ledger. Two steps:
**#58** (ledger as append-only event log, markdown derived) unlocks it, then the
dashboard renders a verification panel — and can **auto-tick** rows, since every
evidence spec already names a journal observable. Not filed; file it against #58
if you pick this up.

---

## 5. Operational notes worth carrying

- **Windows runners hung ~1.5h *inside* "Run tests"** on keifu PRs #150 and #152
  while #153's identical job passed in 3m48s — flake, not a deterministic hang.
  Fix: push an empty commit (`git commit-tree "$head^{tree}" -p "$head"`) to
  retrigger. Both passed in <3 min after. **zsh gotcha:** `"$new:refs/heads/$br"`
  gets eaten as a `:r` modifier — brace it as `"${new}:refs/..."`.
- That retrigger **verified #23 patch-id re-review skip** for the first time —
  `review_skip` on both PRs, no paid re-review, because the patch-id was
  unchanged. A clean no-conflict rebase equivalent had eluded seven deployments.
- **`gh pr merge --auto` is refused** ("Pull request is in unstable status")
  when checks have already completed with a red advisory check. The daemon
  withholds its marker and retries next poll — self-heals in one tick. Don't
  intervene.
- **`CI_PENDING_MAX_SECONDS` fired for the first time ever** (verified) on the
  hung Windows job. Its reversal path was what misbehaved (#54).
- A parallel session left this checkout **on the PR #59 branch**, so the live
  daemons were hot-reloading from that branch, not `main`. Content was identical
  and the tree was clean, so I flipped back and fast-forwarded. **Check
  `git status -sb` in the harness checkout at session start** — it is the tree
  every running daemon reloads from.
- Ledger verdicts this session: **VERIFIED** #37 dashboard (caveat: healthy-idle
  daemons read `STALE`, since a quiet poll loop journals nothing), #23 patch-id
  skip, `CI_PENDING` stall escalation. Rows added for #48/#1/#37 and the six
  #59 changes — all still **pending**.

## 6. Loose ends for the operator

- **#135's grey draft-PR styling** had its live visual check deferred to you —
  open keifu and eyeball a draft PR.
- **Stale local `fix/128…133` branches** in `~/Repos/keifu` from the previous
  batch. Harmless; yours to prune.
- **Six daemons still running** (keifu + tauri), nothing queued. Stop via the
  `point-at` foreground shells or leave them.
- **Two things to file** if you agree: the `make test-domain` under-declaration
  (§2), and dashboard-renders-the-ledger against #58 (§4).
