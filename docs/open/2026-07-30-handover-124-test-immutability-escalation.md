# Handover — #124 restarted on a new spec, harness PAUSED (2026-07-30 ~03:00Z → 05:00Z)

**Daemons are DOWN** (`issue-agent stop`, all three, 04:47Z). Nothing is running.

The one-line summary: **#124's first design let the fixer authorize its own
test edits. PR #134 is closed; #124 restarts on a reviewer-granted design
([spec here](https://github.com/xnmp/autonomous-issue-agent/issues/124#issuecomment-5126740365)).
The push-gate hardening that rode along with #134 lost four straight rounds to one
bug class and moves to #146.**

## 1. #124 starts afresh — what changed and why

`check_tests_immutable` stops the fixer weakening a test its change broke. But
sometimes the existing test is *itself* wrong: #89's fix must rewrite
`assert agent_run["cost_usd"] == pytest.approx(8.0)`, and that assertion **is**
the bug (it encodes cache-blind pricing). A hook reading the diff cannot tell
"the test was wrong" from "the agent is cheating" — they are byte-identical.

**PR #134's answer, now rejected:** a manifest the fixer writes for itself
(`<issue> | <path> | <reason> | <replacement>`). The fixer invents the reason and
commits it in the same change, so nothing in the loop is outside the guarded
party's control. That converts rule 15 in HARNESS.md from **Hook** (mechanical) to
**Prompt** (honour-system) — #134 edits that table row itself, which is the tell.

Every apparent constraint was cosmetic or model-adjudicated. Critically, the one
that looked mechanical is not: **`check_red_fails` is suite-level, not per-test.**
It reverts impl files and runs the whole `TEST_CMD`, denying only if the *suite*
passes. A change that weakens a declared test but also adds one genuine new test
still goes red, so it is allowed and the weakening is never examined. #134 also
moved declared tests *out* of the reverted set, so the weakened version is what runs.

**The new design (operator's):** the fixer **requests**, the reviewer **grants**.

1. **Request** — the declaration survives unchanged in shape. The gate still
   requires it for any existing-test edit, so the edit is explicit and visible.
   It authorizes nothing.
2. **Grant** — `test-immutability-exemption: granted issue=<n> file=<path> patch=<patch-id>`
   authored by `REVIEW_BOT`. Unforgeable: `IDENTITY_MODE=three`, `FIX_BOT=xnmp`
   (plain `gh`), `REVIEW_BOT=xnmp-bot` (GitHub App) — the fixer cannot author as
   the App without its private key.
3. **Enforced at MERGE, not commit.** The reviewer must *see* the change to judge
   it, so the change may exist in the PR; what must not happen is that it *lands*
   unexamined. Auto-merge is withheld until every declared file carries a matching
   grant. An ungranted PR simply never auto-merges and escalates normally.
4. **Grant bound to `patch-id`** (`review.patch_signature`, rebase-invariant) so a
   granted small correction cannot be swapped for a large one.

Fail-closed cases, reviewer-prompt requirements, acceptance criteria, regression
tests and the metric/guardrail pair are all in the issue comment. **Read it before
implementing** — in particular: if `IDENTITY_MODE` is not `three`, no exemption
path exists at all; do not degrade to self-authorization.

**Guardrail for whoever builds it:** rule 15 must stay **Hook** in HARNESS.md's
table. An implementation that cannot keep that classification honest is the wrong
implementation.

## 2. #146 owns push gating now — four rounds, four findings, one class

#134 also rewrote push gating (+271 lines). That was *not* scope creep — the
manifest's trust anchor is `git show HEAD:…` in the checkout, so a push of a
different commit must be refused. But the surface produced a finding every round:

| round | vector | outcome |
|---|---|---|
| 3 | `-c remote.*.push=…` | closed by name |
| 4 | `-c remote.*.mirror=true`, `-c push.default=matching`, `--config-env`, `GIT_CONFIG_*` | closed as "all command-local config" |
| 5 | `XDG_CONFIG_HOME=<dir> git push` (git reads `$XDG_CONFIG_HOME/git/config`) | open → **#146** |
| 6 | `git push git@github.com:owner/repo.git` — first positional containing `:` parsed as a *refspec*, not the repository | open → **#146** |

**The generator: the gate re-implements git's push resolution (argv + config +
env), and every divergence from git is a bypass.** The round-4 fix was itself an
enumeration and lost on round 5.

**The principled fix:** stop modelling git, ask it. `git push --dry-run --porcelain`
with identical argv and environment prints the exact ref updates git would perform,
resolving URLs, refspecs, `push.default`, `mirror`, `-c`, `XDG_CONFIG_HOME` and the
rest. Validate that output. Design for: it contacts the remote (latency +
credentials), must fail CLOSED on network failure, and narrows but does not
eliminate a TOCTOU window. **#146 still needs broadening to cover the argv half.**

**All four vectors exist on `dev` today** — verified — so #134 was never a
regression. It just kept discovering a pre-existing surface.

## 3. #89 is ready to unblock by hand — verified, no code needed

The fix is already written on `fix/89-codex-pricing-is-cache-blind-cached-inpu`
(8 commits). I rebased it onto `dev` in a scratch worktree and ran the full suite:

```
Rebasing (8/8) … Successfully rebased          # clean, no conflicts
✓ all suites passed                            # make test, incl. import-linter
```

It touches two **existing** test files (`test_codex_runner.py`,
`test_codex_budget_enforcement.py`) plus a new `test_codex_cache_pricing.py` —
which is exactly why the gate blocks the fixer, and exactly why a human can just
do it. No governor runs in an operator's shell, and `REVIEW_ADOPT_UNLABELED=0`
means the harness will not touch a hand-opened PR.

```bash
git switch fix/89-codex-pricing-is-cache-blind-cached-inpu
git rebase origin/dev            # verified clean at dev a350f8b
make test                        # verified green
git push --force-with-lease
gh pr create --base dev --title 'fix: price cached Codex input separately (#89)'
```

**Why a human is not blocked and the fixer is:** the gate is NOT a git hook.
`core.hooksPath` is unset and `.git/hooks` holds nothing executable — `point-at`
deliberately installs nothing into the target. `check_tests_immutable` is only
reachable through the governor, which the daemons inject in-process into *agent
sessions*. It binds the fixer and nothing else.

⚠️ **Carry this PR to merge BY HAND. Do not label it `ai-pr-open`.** The moment
the harness adopts it, a `REQUEST_CHANGES` verdict dispatches an `ADDRESS_REVIEW`
leg — and that leg *is* governed, so it hits the immutability wall on the very
same two test files and the deadlock returns. Review it yourself, or with a
reviewer whose findings a human applies.

**This is the highest-value thing outstanding.** Until it lands every ledgered
dollar reads ~7x high, which is what made `ISSUE_SPEND_CAP_USD` fire on #124 at
~$39 real, and it is why `MAX_BUDGET_USD=80` / `REVIEW_BUDGET_USD=32` /
`MAX_INFRA_DEATHS=50` are all still carrying `WORKAROUND(#89)`.

## 4. State at pause

| item | state |
|---|---|
| **daemons** | **DOWN** (all three, 04:47Z) |
| **PR #134** | **CLOSED** — superseded; branch kept deliberately (§5) |
| #124 | open, restarting on the new spec (§1) |
| #89 | `needs-human-review`; rebases clean, suite green, ready to hand-open (§3) |
| #113 / PR #128 | `ai-blocked`+`ai-pr-open` — **now unblocked**, its blocker was #134's gate |
| #146 | filed; needs broadening to the argv half (§2) |
| #143 | filed — dashboard step strip collapses repair rounds |
| PR #137 (#24) | **merged** (`c27ddcc`) |
| PR #142 (#118) | **merged** (`a350f8b`) |
| PRs #139/#140/#141/#145 | open — another agent is merging these |

**Spend today: ~$395 ledgered ≈ $56 real** (7x, #89). #124 alone is $272.

### Config restored at pause

Both knobs raised this session are back; composed env regenerated. Pre-existing
`WORKAROUND(#89)` entries untouched (`MAX_BUDGET_USD=80`, `REVIEW_BUDGET_USD=32`,
`MAX_INFRA_DEATHS=50`).

- `ISSUE_SPEND_CAP_USD` → `250`. It fired **correctly** on #124 — a real runaway.
- `REVIEW_MAX_REJECTIONS` → removed (default 4). Raised only for one more round
  on #134; that reason expired.

## 5. What is on the closed #134 branch, and what to lift

Do not delete the branch. Do not rebase it onto the new design — the core
mechanism is what changed.

- **Lift: the parity harness** (`tests/adapters/test_gate_exception_parity.py`).
  Real git repos, both stacks, covering Unicode digit spoofing in the issue field
  (`１３`), the working-tree override (commit a benign manifest, swap it on disk),
  and the recreated-deletion trick. Right shape for the new design too.
- **Lift: the declaration grammar and bounds** (`f271dfc`) — the request half
  survives verbatim: per-file, per-issue, reason required, deletion needs a named
  replacement, `MAX_TEST_IMMUTABILITY_EXCEPTIONS = 3`, `exists_at_head` defaulting
  fail-closed.
- **Do NOT lift `7f59562`** (command-local git config). It is an enumeration
  wearing a class fix's name; #146 supersedes it.

## 6. Findings worth carrying forward

**The world probe is blind to local state edits.** Deleting
`review-agent-self/pr-134.last-sha` to force a re-review did nothing for ~4
minutes: the probe skips a tick unless GitHub's newest `updated_at` changed, and
editing a state file bumps nothing on GitHub. Only the 300s TTL reconciliation
build notices. **Make the GitHub-side change LAST** when hand-driving the harness.

**Three mechanisms sat a layer below where they appeared to.** `pr-134.round` is a
display counter (the real ceiling is `count_prior_rejections`, recomputed from
GitHub live); relabelling to `ai-pr-open` does not trigger a re-review (the stored
head SHA gates it); clearing that SHA still waits for the probe TTL. Read the code
path before asserting harness behaviour.

**The codex reviewer was right 4/4 on real security findings**, at $2.25–4.92 a
round, and it is what stopped a self-authorizing bypass from landing. Weigh that
when batch-4 review quality is assessed — especially since #125 (which restored
the approval-rate metric) only landed mid-session.

**Shared-checkout hazard, still unfiled.** Interactive codex sessions work directly
in `~/Repos/autonomous-issue-agent`, which is also where the daemons execute from.
A session switched the checkout to `fix/144-…` mid-work and the fixer hot-reloaded
onto it twice (04:12:39, 04:14:22) — the harness briefly ran an unreviewed feature
branch. Restored to `dev`; that session's staged change is preserved in
`stash@{0}` ("wip #144 backlog LIFECYCLE_LABELS"). The `/tmp/autonomous-issue-agent-*`
worktree pattern the other operator PRs use avoids this and should be the rule.

## 7. Next session, in order

1. **Hand-open #89's PR** (§3) — verified ready, highest value, unblocks every
   cost number and four config workarounds.
2. **Implement #124 on the new spec** (§1). Read the issue comment first.
3. **Broaden #146** to the argv half and the `--dry-run --porcelain` approach (§2).
4. **#128 is no longer blocked** — its blocker was #134's gate.
5. Restore `MAX_BUDGET_USD=20`, `REVIEW_BUDGET_USD=8`, unset `MAX_INFRA_DEATHS`
   when #89 lands.
6. File the shared-checkout hazard (§6).
7. dev→main promotion (operator).
8. Still owed from batch 3: #71 resume-a-killed-session, #79/#84 stderr bounds,
   #62 ceiling=true.
