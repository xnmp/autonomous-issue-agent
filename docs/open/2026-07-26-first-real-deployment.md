# Handover — first real deployment (xnmp/tauri-explorer), 2026-07-26

Supersedes the *predictions* in [2026-07-26-first-run-handover.md](2026-07-26-first-run-handover.md).
That document said what we expected the first run to do. This one says what it
actually did. Where they disagree, this one is right.

**Headline: the loop closed end to end, unsupervised.** Issue #504 went
`ai-ready → branch → TDD red/green → PR → CI → independent review → approval →
squash-merge into dev → issue auto-closed` with no human in the loop after the
label. That is the first time the harness has done that, and it had never made a
real `gh` write before this session.

It also cost **~$28 for one merged PR, one approved-pending PR, and one
abandoned issue**, and every single defect below was invisible to `make test`
and to `doctor`. Read §3 before running it unattended.

---

## 1. What ran, and what it produced

| Issue | Outcome | Cost | Notes |
|---|---|---|---|
| **#504** tab title with one tab + window controls | **MERGED** (PR #532) → `dev` @ `0feb8a95`, issue auto-closed | $3.83 | full loop, unsupervised |
| **#494** hide auxiliary info in preview pane | PR **#533 open, APPROVED, not merged** | $7.66 | see §3.4 — the fixer never got a free tick |
| **#501** Alt+L/P/;/' directional pane focus | **abandoned**, `ai-ready` removed | $12.03 | 2 attempts, no PR. §3.1 + §4 |
| **#506** git-repo folder icon in tab bar | in flight at handover time | — | first run with the new forensics |

Baseline for the target repo: `bunx vitest run` → 1253 tests / 117 files green
in ~5s. Deliberately NOT `bun run test`, which also runs the perf tier — perf
assertions flake under agent CPU load and would send the harness chasing phantom
failures. `TEST_CMD` is pinned in the overlay.

### Quality of the merged work

Higher than expected, and worth knowing because it sets the bar for judging
regressions. On #504 the agent found the root cause by git archaeology — the
clause had been fixed inline in `WindowTabBar.svelte` (5ca49d76) then silently
dropped when #140 extracted the rules into a module — hit the test-immutability
gate mid-run, and **self-corrected without intervention**: it kept
`showWindowTabBar`'s two-arg contract intact and added a composed `showTabStrip`
in a NEW test file. Its PR body carried a revert check (source reverted → AC-1
red, AC-2/AC-3 green, as over-fix guards should be). The reviewer independently
re-verified by simulated revert rather than rubber-stamping.

---

## 2. Fixed this session (4 commits)

| Commit | What |
|---|---|
| `bf90246` | singleton guard, reviewer identity fallback, doctor write-access probe |
| `7b2c994` | cost an aborted run instead of ledgering $0.00 |
| `13cf985` | auto-merge confined to harness-authored branches |
| `16e06ef` | stop reporting a failed run as a success; log why it failed |

Four of these were **blockers that no test could have caught**, because each one
only exists in the presence of a real target repo, a real config file, or a real
GitHub identity.

**`bf90246` — the singleton guard blocked every start.** `$(...)`, pipelines and
`&&` lists all fork bash subshells that inherit the parent's argv, so
`pgrep -f "point-at.sh <name>"` matched the launcher's OWN command-substitution
subshell: a pid that is neither `$$` nor `$PPID`, and already reaped by the time
an operator could `ps` it. It fired on the very first invocation. **This is why
the harness had never made a real write.** Now filtered by parentage.

**`bf90246` — the reviewer could never authenticate.** `_app_identity` chose the
GitHub App branch whenever the three `GH_APP_*` vars were non-empty, and
`shared.example.env` ships them as literal `<your-app-id>` placeholders which
PATH A documents leaving alone. Every poll died on ENOENT for
`<your-app>/private-key.pem`, surfacing as a line that reads like a network blip.
Now gated on the key being readable, falling through **loudly**.

**`7b2c994` — aborted runs ledgered $0.00.** Dollars only arrive on the final
`ResultMessage`, so a wall-cap/idle/dropped-result abort reported `cost_usd=0.0`
AND `usage=None`. The first #504 attempt ran 906s, made four commits, and wrote
no spend line — so repeated timeouts accumulate money both caps are blind to.
The runner now keeps a running token total and the journal prices it at a rate
*learned from completed runs* (self-calibrating; returns "no estimate" until
there is evidence, so a fresh install never invents a number). Validated: it
prices that 906s run at $3.83, matching the equivalent completed run.

**`13cf985` — two safe defaults composed into an unsafe one.**
`REVIEW_ADOPT_UNLABELED` (on) makes the reviewer pull EVERY open PR;
`AUTO_MERGE` (on in three-identity mode) merges approved+green PRs. Together:
"approve and merge ANY open PR into dev". The reviewer adopted dependabot's #486
within one poll, on a repo with six dependency PRs queued. `should_automerge` and
`should_update_branch` now require `is_governed_branch(head_ref_name)`, keyed on
the branch rather than the linked issue **specifically because** a stranger can
populate `closing_issue` just by writing "Fixes #N" in a PR body.

**`16e06ef` — the log reported a failed run as a success.** See §3.1.

---

## 3. Open problems, in priority order

### 3.1 An agent can fail while claiming success (partially fixed)

#501's first attempt returned:

```json
{"status":"success","is_error":true,"exit_reason":"agent_error",
 "duration_s":290.8,"cost_usd":1.93}
```

290 seconds, $1.93, **zero commits**, and the daemon log said
`done (status=success, $1.93)`. `result.status` is `structured_output.status` —
the agent's *claim about itself*; `is_error` is the *observation*. The log line
preferred the claim.

`classify_outcome` was never fooled (error wins there), which is why the harness
correctly requeued the issue — the machinery was right, the **reporting** lied.

Fixed in `16e06ef`: `status_label` renders the envelope first and shows the claim
marked as a claim; `envelope_subtype` is captured unconditionally (it used to be
discarded whenever the agent declared a status — so precisely the runs that
misreport themselves also destroyed the evidence); and `error_forensics` logs
`errors`, `permission_denials`, `num_turns`, `stop_reason`, `terminal_reason`,
`api_error_status` and the result text at the moment of failure.

**Still open:** we never learned why that run errored, because the logging did
not exist yet. `permission_denials` is the field I most want — one denial is a
blocked command, forty is an agent wedged against the governor burning budget,
which is a plausible reading of 290s/zero-commits but **unverified**. If this
recurs, that line will now name the cause. Do not assume the cause is known.

### 3.2 The heartbeat cries wolf on every long dispatch

```
fix: ♥ woke after 1265s (expected ~20s) — likely suspended/stalled; catching up
```

The daemon was not stalled; it was working. `Daemon.run_forever` computes
`elapsed` across the whole iteration **including `self.tick()`**, and a tick that
dispatches an agent blocks for the length of that session. So every successful
long resolve reports itself as a stall, which trains operators to ignore the one
line that is supposed to mean "your laptop suspended".

**I attempted a fix and reverted it.** Re-stamping `last = now()` after the tick
looked obviously correct, and the line demonstrably executes (verified with a
print, 3/3 ticks) — yet the warning still fired and I could not explain why
before running out of session. `git revert`-clean; nothing is half-applied. The
diagnosis above is solid; the fix is not. **Do not re-apply the naive re-stamp
without reproducing first** — something about that loop's timing is not what it
appears, and I would start by printing `last`/`cur`/`elapsed` on every iteration
rather than reasoning about the source.

### 3.3 A single issue can burn $12+ with nothing to show

#501: attempt 1 errored at $1.93, attempt 2 hit `error_max_budget_usd` at $10.10
— the full per-run `FIX_BUDGET`. Retry cap is 3, so a third would have taken it
to ~$22 against the $25 per-issue cap. I stopped the daemons and removed
`ai-ready` rather than let it fire.

The caps did work — nothing exceeded a configured limit. But **`FIX_BUDGET=$10`
× `resolve` retry cap 3 = $30 of authorised spend per issue**, which is above the
$25 issue cap and reachable without ever producing a PR. Worth considering
whether a run that ends in `BUDGET_EXHAUSTED` should count differently against
the retry cap than one that ends in an infra death: retrying a task that
*ran out of room* at the same budget is close to a guaranteed repeat.

### 3.4 A 20-minute resolve delays a 1-second merge (cheap work should go first)

PR #533 sat APPROVED, CI-green, on a governed branch, with `AUTO_MERGE=1` — and
did not merge, across two separate daemon lifetimes. Nothing was wrong with it:
`should_automerge` would have returned True. The fix daemon's tick was simply
blocked inside a resolve session both times (first #501's, then #506's), and
auto-merge, reaping and rebase checks all live on that same tick.

Verified not-the-cause: the `automerge_enabled` idempotence marker was never set
(no `issue-494.*` marker in the state dir), labels were `ai-pr-open`, branch was
`fix/494-…`, all 7 checks SUCCESS.

Dispatch being synchronous is by design. The sharper point is **ordering**: a
tick emits a list of actions and works through it, so a merge (one `gh` call,
~1s) queued behind a resolve (an agent session, up to the 40-minute wall cap)
waits for the whole session. Executing a tick's cheap, terminal actions —
merges, relabels, reaps — BEFORE dispatching any agent would have merged #533
instantly and cost nothing. As it stands, **the more work the harness has to do,
the longer finished work sits unmerged**, which is exactly backwards.

### 3.5 The reviewer adopts PRs it should not (mitigated twice)

Fixed at the merge boundary in `13cf985`, and `REVIEW_ADOPT_UNLABELED="0"` is set
in `config/repos/xnmp-tauri-explorer.env` with a comment. Note the code fix stops
the harness *merging* a foreign PR; it does not stop it *paying to review* one.
On a repo with a dependabot queue, keep the config knob too.

---

## 4. Choosing issues: what actually predicts an easy run

I mislabelled #501 "low-hanging fruit" and it cost $12. The signal is **not**
how small the feature sounds. It is:

1. **One importable seam.** #504 was a single pure function
   (`showWindowTabBar`); #494 was one additive setting. #501 touched domain AND
   state AND several components, and ended up 13 tests red at the budget ceiling.
2. **Additive, not behaviour-changing.** `check_tests_immutable` **denies
   modifying any existing test file**. So an issue whose fix changes behaviour
   that existing tests assert is a *deadlock*: the agent must change the
   behaviour, cannot edit the tests asserting the old one, and cannot push red.
   I rejected #507 (compact relative dates) for exactly this — `git.test.ts` and
   `git-graph.test.ts` hard-assert `"5 minutes ago"` / `"1 month ago"`.
   The escape hatch exists (add a new function alongside, as #504's `showTabStrip`
   did) but it is a coin flip whether the agent finds it.
3. **A regression or a new option** beats "change how X works".

Also: the harness ALWAYS branches `fix/{issue}-{slug}` (`fix.py:135`), so the
`feat/` "must add an e2e test" arm of `check_required_test` never fires. A
vitest file under `tests/` satisfies the `fix/` arm.

---

## 5. Operational notes

- **Wall cap:** 900s is too tight once screenshot capture is involved — #504's
  first attempt died 6s over, having done all the work but not opened the PR.
  **2400s** was comfortable for every subsequent run.
- **Reviewer token:** must be a **classic PAT with `repo` scope**. A fine-grained
  PAT cannot work here: it reaches resources owned by ONE account, and
  `tauri-explorer` is owned by `xnmp` while the bot is `xnmp-bot`, so the repo
  falls under "public repositories (read-only)" no matter which permission boxes
  are ticked. Symptom is every read succeeding and every write 403-ing with
  "Resource not accessible by personal access token". Verify with a round-trip
  probe (create a PENDING review, delete it) — `doctor`'s `.permissions.push`
  check reports the *collaborator role*, which was `true` even while writes 403'd.
- **`ps` in this environment is filtered by the `rtk` shell wrapper** and returned
  empty for live daemons. I twice concluded the daemons were dead when they were
  not. Check the daemon's own log file mtime, or the launcher's output file
  growth, not `pgrep`. Note a review session writes NOTHING until it finishes, so
  a stale log is not evidence of death either.
- Restarting the daemons is how new harness code takes effect; a running daemon
  has already imported its modules.

## 6. Config state at handover

`config/repos/xnmp-tauri-explorer.env`: `TEST_CMD="bunx vitest run"`,
`REVIEW_ADOPT_UNLABELED="0"`. `config/shared.env` (gitignored):
`IDENTITY_MODE=three`, `FIX_BOT=xnmp`, `REVIEW_BOT=xnmp-bot`, **`AUTO_MERGE="1"`**
(flipped this session — approved + green now merges to `dev` with no human),
`STEWARD_TRIAGE=0`, `FIXER_RUNTIME=claude`.

Caps: `FIX_BUDGET=$10`, `REVIEW_BUDGET=$8`, issue `$25`, daily `$50`.
Spend on 2026-07-26: **~$28**.

## 7. Suggested next steps

1. Merge or close PR #533 (approved, green, waiting only on a fixer tick).
2. Reproduce §3.2 properly before touching `loop.py` again.
3. Decide the §3.3 policy question: should `BUDGET_EXHAUSTED` consume a retry?
4. Re-run #501 only if you want it — it is a genuinely harder issue than its
   title suggests, and the branch `fix/501-feat-altlp-shortcuts-to-move-focus-betwe`
   still has 5 commits of real work in the worktree if you want to salvage it.
5. Keep pointing it at *additive, single-seam* issues until §3.1 has produced a
   real forensics line, so we learn what the agent-error class actually is.
