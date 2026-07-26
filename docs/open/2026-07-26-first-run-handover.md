# Handover — the first real run (2026-07-26 00:15Z)

Audience: whoever points this harness at a real GitHub repo for the first time.
Prerequisite read: [residuals.md](residuals.md) §1. Everything below assumes the
code state at commit `9435a4f`.

> **The short version.** The code is ready; the deployment is not configured
> yet, and that is about ten minutes of work (§1.1 — no GitHub App needed).
> Do the first run in `IDENTITY_MODE=solo` with `ROLES="fix"` and
> `ISSUE_AGENT_MAX_TICKS=1`: in solo the review daemon is inert and auto-merge
> is *structurally* impossible, so the harness can open a PR but cannot merge,
> approve, or close anything (§2). Know before you start that your spend caps
> are probably inert — they sum a ledger that stays empty on plans reporting
> `$0.00`, so the wall cap and the tick limit are your real bounds (§5.1). And
> expect the first failures to be shape mismatches between real `gh` output and
> the test fakes, not logic errors — nothing here has ever made a real `gh`
> write.

## 1. Are we ready?

**The code is. The deployment is not yet configured, and that is the only thing
standing between here and a first run.**

| | Status |
|---|---|
| Correctness | ✅ Every P0/P1 from two adversarial reviews closed (`44fbd16` → `1968888`) |
| Test suite | ✅ ~1,500 tests, gate parity across both enforcement stacks, convergence as an oracle, import-linter 2/2 |
| `gh` auth | ✅ logged in as `xnmp` |
| `config/shared.env` | ❌ **does not exist** — only `shared.example.env` |
| `config/repos/*.env` | ❌ **no overlay has ever been created** |
| Real gh writes ever performed | ❌ **zero** |

That last row is the honest risk and no amount of further code work reduces it:
every test fakes the GitHub boundary, so the first run tests the *fidelity of
the fakes* as much as it tests the harness. Expect the first failures to be
shape mismatches between what `gh` really returns and what `FakeGateway`
returns — not logic errors.

### 1.1 Closing the config gap (~10 minutes, no GitHub App)

`config/shared.env` is gitignored and does not exist; only
`config/shared.example.env` ships. That template assumes `IDENTITY_MODE=three`
and therefore leads with `GH_APP_ID` / `GH_APP_INSTALL_ID` / `GH_APP_KEY_FILE` —
**none of which solo mode needs.** Do not go set up a GitHub App for the first
run.

`issue-agent start` will create this file interactively if it is absent, but
writing it by hand is faster and you can see exactly what you agreed to:

```bash
cat > config/shared.env <<'EOF'
IDENTITY_MODE="solo"      # review daemon inert, auto-merge impossible — see §2
FIX_BOT="xnmp"            # your own gh login; solo needs no second identity
FIXER_RUNTIME="claude"
STEWARD_TRIAGE="0"        # not on a first run
JIRA_SYNC="0"
EOF
chmod 600 config/shared.env
```

The per-repo overlay (`config/repos/<name>.env`, holding `REPO`, `TARGET_DIR`,
`BASE_BRANCH`, `TEST_CMD` and the isolated `*_STATE_DIR`s) is synthesized for you
by `issue-agent start` from the cwd — let it, rather than hand-writing one.

Verified against `HarnessConfig.from_env`: `REPO` + `FIX_BOT` +
`IDENTITY_MODE=solo` + `TEST_CMD` + `TARGET_DIR` yields **zero validation
errors**. `AUTO_MERGE` stays `True` by default and that is fine — solo ignores
it structurally, and `doctor` says so explicitly rather than leaving you to
wonder.

Then, before anything else: `issue-agent doctor`, and read every line. It now
checks state-dir collisions across overlays, normalises bot logins (so
`fix-bot` vs `fix-bot[bot]` is caught before GitHub 422s every approval),
asserts the push gate's `gh pr list` probe actually works, and flags an
`.active.env` snapshot missing a variable your config assigns.

## 2. Do the first run in solo mode. It is dramatically safer.

`IDENTITY_MODE=solo` is not a degraded mode — for a first run it is the correct
one, and it is verifiable from the code rather than a matter of taste:

- `domain/review.py`: `decide_review` returns `[]` under solo, so the **review
  daemon does nothing at all** — no review spend, no approvals.
- `domain/fix.py::should_automerge` requires `IdentityMode.THREE`, so
  **auto-merge is structurally impossible**, whatever `AUTO_MERGE` says.

Net blast radius: the harness can create a branch, commit, and open a PR. It
**cannot** merge, approve, or close anything. You review every PR by hand, which
is exactly what you want the first time you watch it work.

Solo also needs **no GitHub App** — the App exists to give review/steward an
identity distinct from the fixer (GitHub 422s self-approval). Skipping it
removes the single fiddliest setup step from the first run.

## 3. The recipe

```bash
# 1. A throwaway repo you do not care about. Not a real project.
#    It needs: a `dev` branch (point-at creates one if absent), and a test
#    command that exits (non-watch).
cd /path/to/scratch-repo

# 2. With config/shared.env written per §1.1, `start` auto-detects
#    REPO/TARGET_DIR from cwd, probes for a TEST_CMD with a cheap read-only
#    Haiku call, and synthesizes the per-repo overlay. Preflight first:
issue-agent doctor          # run this FIRST and read every line

# 3. One tick, fix daemon only, with the wall cap set deliberately.
ROLES="fix" ISSUE_AGENT_MAX_TICKS=1 AGENT_MAX_RUNTIME_SECONDS=900 \
  issue-agent start

# 4. In another shell: give it exactly one trivial, self-authored issue.
issue-agent ready <n>
```

Write the seed issue yourself, keep it small and mechanical ("add a `--version`
flag"), and give it a clear acceptance-criteria section — the PR gate requires
one, and a first run should not fail on prompt-shape problems you can avoid.

**Why `ROLES="fix"`:** the review daemon is inert in solo mode anyway, and the
steward polls for triage work you do not want on a first run. One daemon, one
issue, one tick is the smallest thing that exercises the whole loop.

## 4. What to watch, and when to abort

Logs: `${XDG_STATE_HOME:-~/.local/state}/issue-agent/logs/fix-<name>.log`.
Then `issue-agent status` for spend, the last runs, and loop warnings.

**Good signs, in order:** a `worktree #N: ready` line → an `agent #N: resolve —
running` line → per-commit `red:`/`green:` milestones from the governor hook →
`agent #N: resolve — done` → a PR on GitHub with the issue's acceptance criteria
in its body → the issue relabelled `ai-pr-open`.

**Abort immediately (Ctrl-C) if:**
- the same target is dispatched twice in one tick — that is a loop the breakers
  should have caught, and it is the single most expensive failure shape;
- `SPEND CAP:` lines appear alongside `$0.00` — see §5.1, your caps are blind;
- pushes are denied repeatedly with "could not verify PR state" — the push-gate
  probe cannot reach `gh`; fix auth before continuing (`doctor` now asserts
  `gh pr list` for this reason);
- anything is merged. Nothing should merge in solo mode. If something did, stop
  and report it — that would be a genuine finding.

Ctrl-C is safe and is now counted correctly: `KeyboardInterrupt` is re-raised
without burning an infra-death life (review P1R-3).

## 5. The three risks that are open by design

### 5.1 Your spend caps are probably inert
`ISSUE_SPEND_CAP_USD` / `DAILY_SPEND_CAP_USD` sum the cost ledger, and a run
only writes a ledger line when it reports `cost_usd > 0`. Subscription-plan
Claude reports `0.00`. **On such a plan the caps can never fire, for any loop
shape.** The harness now emits one loud warning when settled runs keep costing
$0 while a cap is armed — believe the warning, not the `$0.00` on `status`.

*Mitigation for the first run:* `AGENT_MAX_RUNTIME_SECONDS=900` (above) plus
`ISSUE_AGENT_MAX_TICKS=1` are your real bounds. Do not run unbounded until you
have seen a full loop complete.

### 5.2 The governor's indirect-execution bypass is open
`sh -c`, `bash -c`, `python -c`, `eval`, `xargs` classify as nothing, so a
payload inside them skips every gate. The fixer runs `bypassPermissions`.
*Mitigation:* only issues you wrote yourself; never a repo with untrusted
inbound issues. (Five `xfail(strict=True)` tests record this; root cause and the
two viable fixes are in `../history/2026-07-26-p0-rereview.md` §4.)

### 5.3 The fakes have never been checked against reality
*Mitigation:* read the first PR's body and diff carefully rather than trusting
that a green loop means a good result.

## 6. If it works, escalate in this order

Change **one** thing at a time; each step widens blast radius.

1. Raise `ISSUE_AGENT_MAX_TICKS`, then drop it entirely — confirm the daemon
   idles cheaply on a frozen world (`status` shows no new runs when there is no
   work; this is what `test_convergence.py` asserts in the abstract).
2. Add a second and third issue — exercises the single-fixer invariant, which is
   enforced by process topology (flock + synchronous dispatch), not by labels.
3. Turn on the **steward** (`ROLES="fix steward"`) for triage.
4. Set up the GitHub App, switch to `IDENTITY_MODE=three`, add the **review**
   daemon. Run `doctor` again — it now normalises logins, so `fix-bot` vs
   `fix-bot[bot]` is caught before GitHub 422s every approval.
5. Only then consider `AUTO_MERGE=1`, and only on the throwaway repo.
6. `issue-agent install-service` for systemd once you trust the loop. Note
   `Restart=always` + `RestartSec=30` with no `StartLimitBurst`: a crashlooping
   daemon restarts forever. Process death is now *bounded* (unsettled
   `agent_run_start` events are counted) but still *unbilled*.

## 7. What the first week should answer

These were deferred until the ledger could be trusted. It can now — with the
§5.1 caveat that on a $0-reporting plan you will get token counts but no dollars.

| Question | Where to look | What it unblocks |
|---|---|---|
| Is prompt caching working? | `agent_run.usage.cache_read_input_tokens` ≈ 0? | P4.9 cross-leg cache sharing jumps the queue, or drops |
| What does a trivial issue cost? | median cost of `trivial`-labelled issues ≳ $1? | P4.1 model routing (`build_router_task` is dead code kept for exactly this) |
| Are review legs a top-2 line? | ledger, once the review daemon is on | P4.2/P4.3 review-cost work |
| Do labels ever strand *despite* systemd + reaper? | `status` warnings, stray labels | P2.1–2.3 reconciliation/leases |

Full trigger table: [residuals.md](residuals.md) §3. **Do not build backlog
items without their trigger** — that discipline is why the core stayed lean.

## 8. If it breaks, read this first

- `issue-agent status` — merges all three role journals by timestamp, shows
  today's spend, the last runs, and loop warnings.
- `$STATE_DIR/journal.jsonl` — per-run events. A dispatch that died before
  returning leaves an `agent_run_start` with no settled twin; that is the
  signature of a process death.
- `~/.local/state/issue-agent/spend.jsonl` — the global ledger.
- [../reference/failure-modes.md](../reference/failure-modes.md) — 110 recorded
  modes. Check here before diagnosing from scratch; #107–110 are the classes the
  2026-07-26 reviews added.

**When you find something new, add it to failure-modes.md with the class, not
just the instance.** The three defects that survived longest in this codebase
were all *absence* bugs — a variable no longer emitted, a failure no longer
counted, a key no longer cleared — and mutation testing is structurally blind to
them. If your fix is "we now handle X", also write the test that removes the
handling and asserts a loud failure.

---

*When the first run is done, move this file to `../history/` with its findings
appended — that is the rule for `open/`: dated files live here while their work
is ahead of them, and graduate to `history/` when it closes.*
