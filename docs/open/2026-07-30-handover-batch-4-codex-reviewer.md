# Handover — batch 4, the first codex-reviewer batch (2026-07-29 22:40Z → 2026-07-30 00:30Z)

Batch 4 is **still running** when this was written — this is a mid-batch handover,
not a post-mortem. Daemons are UP and working; the queue below is live.

The one-line summary: **the first real codex review round exposed a fixer-jamming
defect, and the batch turned into a discovery run.** That is tuition
(CLAUDE.md: "first-of-class deployments spike discovery"), not regression — but it
means the efficiency numbers from this batch are not comparable to batch 3.

## 1. Current state (00:30Z)

| # | State | Note |
|---|---|---|
| **#91** | **MERGED** (PR #111) | Reviewed and approved by codex/sol |
| #92 | `ai-pr-open` P2 | PR #122 open, awaiting review |
| #104 | `ai-working` P2 | in flight |
| #124 | `ai-ready` P1 | **gates #89** |
| #113 | `ai-ready` P1 | filed and fixed-in-part this session (§3) |
| #102 | `ai-ready` P3 | was deferred behind PR #111 |
| **#89** | **`needs-human-review`** P1 | parked, unlandable until #124 merges (§4) |

- **Daemons UP**, relaunched 23:11:36Z after an operator-requested restart.
  Reviewer is **codex `gpt-5.6-sol`**, `review_budget=$32.00`.
- **dev is ahead of main**; promotion still pending, operator-only.
- 17 paid legs since 22:00Z, **$57.10 ledgered ≈ $8.50 real** (see §5 — the
  ledger is ~6.7x inflated and #89 is parked, so this stays true next session).

## 2. Config changes landed this session (`config/repos/self.env`)

Both are live and both are **workarounds with named executioners**:

- `REVIEW_MODEL="gpt-5.6-sol"` — permanent. The `review_model` default is
  `gpt-5.6-terra` (`config.py:275`) and nothing overrode it, so #97's
  codex-default silently swapped batch 3's Opus reviewer for the *cheap* codex
  tier. Operator's call: the reviewer must sit at the same tier as the reviews
  it replaces.
- `REVIEW_BUDGET_USD="32"` — **WORKAROUND(#89)**, restore `"8"` when #89 merges.
  Same 4x headroom, same reason, as the existing `MAX_BUDGET_USD="80"`.

Both were applied via `bash bin/compose-env.sh self`; the daemons hot-reloaded
between ticks and picked them up with no session lost.

## 3. The incident: codex review jammed the fixer (#113)

**Timeline.** The first codex review leg installed its read-only governor hook at
`TARGET_DIR/.codex/hooks/gov.sh` (23:23:23Z, `readonly=1`). Fixer worktrees live
at `TARGET_DIR/.claude/worktrees/issue-N` — *inside* that cwd — so every fixer leg
dispatched afterwards executed the reviewer's hook and was denied `git status`,
tests, commit and push:

```
23:23:23  TARGET_DIR/.codex/hooks/gov.sh   readonly=1   <- REVIEW leg
23:23:45  worktrees/issue-89   -> #89  blocked
23:27:29  worktrees/issue-91   -> PR #111 blocked
23:28:47  worktrees/issue-92   -> #92  blocked
23:31:07  worktrees/issue-104  -> #104 blocked
```

Four items in eight minutes. Each worktree's *own* hook was correct
(`readonly=0`) — the blocked agent diagnosed that itself, which is what
identified the ancestor hook.

**What shipped** (`cf0781a`, on dev, mid-batch, at explicit operator instruction —
this crosses the manager's no-self-modification line and was an operator decision,
not a manager one): the dispatching runner states `ISSUE_AGENT_LEG_ROLE` in the
leg's own env, and `bash_governor.main` gives the role **precedence** over an
inherited `ISSUE_AGENT_REVIEW_READ_ONLY=1`. Unsetting the flag cannot work — the
hook re-exports it downstream of the fixer's env.

Fail direction is asymmetric on purpose: **absent role ⇒ still read-only**, so a
reviewer whose env did not propagate keeps its sandbox (fail closed on the
security boundary); a fixer whose env did not propagate is merely blocked (loud).

**Verified live**: PR #111's `PR_RESUME` leg committed normally after the reload.

**Still open, and #113 says so**: this is precedence, not removal. The reviewer
still writes a read-only hook that outlives its leg, so anything else resolving to
that file still inherits the sandbox. **The exact resolution rule was never
established** — parent-directory walk vs. a linked worktree resolving back to its
main repo root. `cf0781a` works under either; a real class fix depends on knowing
which. One `codex exec` run in a nested worktree with a marker hook at the parent
settles it — do that first.

**Operator mitigation if it recurs before #113 lands**: `rm -rf "$TARGET_DIR/.codex"`
(regenerated per leg). Note the assistant is blocked by the permission classifier
from editing or deleting governor hooks — correctly — so this needs the operator.

## 4. #89 is parked, and #124 is why

#89 (cache-blind pricing, impact:high) cannot land: `check_tests_immutable`
(`domain/gate.py:395`) denies any change to a test file present at the base ref,
and #89's fix *requires* rewriting assertions like
`assert agent_run["cost_usd"] == pytest.approx(8.0)` — those assertions **are** the
bug. A manager authorization comment is worthless here: the gate reads the diff,
not the thread. Three legs blocked on it (~$9.58 / ~$0.53 / ~$9.58 ledgered).

**#124** (filed, `ai-ready` P1) specifies the documented exception path: declared
in the change itself, per-file and per-issue scoped, reviewer-visible, failing
closed, with rewrite-vs-deletion discriminated.

**Sequencing: #124 must merge, then re-label #89 `ai-ready`.** #89's three TDD
commits are intact on `fix/89-codex-pricing-is-cache-blind-cached-inpu`.

⚠️ **Do not simply reply to #89.** A reply is what fires RESUME, so answering
dispatches another leg into the same wall. It was relabelled
`needs-human-review` *before* commenting, for exactly that reason.

## 5. Costs are ~6.7x inflated and will stay that way until #89 lands

The cleanest proof yet, #89's own RESUME leg (00:06:15Z):

```json
"cost_usd": 9.580034,
"usage": {"input_tokens": 4716391, "cache_read_input_tokens": 4533760, "output_tokens": 12271}
```

Terra is priced `2/12`, and the ledger matches full-rate arithmetic exactly —
`4.716×$2 + 0.0123×$12 = $9.580` — confirming cached reads are billed at the full
input rate. **96.1% of that input was cache reads.** Priced correctly:

```
0.183M × $2  +  4.534M × $0.20  +  0.012M × $12  =  $1.43 real   (6.7x)
```

The multiplier grows with turn count. Consequences for the next session:

- **Every ledgered dollar this batch is ~6-7x high.** $57.10 ≈ $8.50 real.
- **`MAX_BUDGET_USD="80"` is load-bearing** (~$12 real). Restoring `"20"` before
  #89 lands would resume killing legs at ~$3 of real spend.

## 6. Issues filed this session (10, all with journal evidence)

**Fixer / harness**
- **#112** — a resolve leg exiting clean with no terminal status (`ok`, no PR,
  `status=""`) burns a full fresh resolve instead of a cheap resume. 2 of 30 legs.
  Rejected alternative documented in-issue: a Ralph-loop stop hook forbidding a
  stop without a PR — it turns a legitimate `blocked` into a budget-death loop and
  pressures the agent to weaken tests when a gate refuses the PR.
- **#113** — the read-only hook leak (§3).
- **#124** — the missing gate exception path (§4).

**Dashboard** (dependency-ordered on purpose — build the derivation before the
presentation)
- **#115** — idle clock ignores human waits: `ai-blocked` time is billed as
  *working* time. Pair with #95/#96 as one transition-logging change.
- **#116** — a killed leg renders as running forever; reconciliation heals the
  label but never settles the journal record. `_process_deaths` already knows.
  Follow #106's append-only correction precedent.
- **#118** — surface rows waiting on a human (merge-ready / awaiting reply /
  escalated) with how long they have waited.
- **#119** — subtle live / waiting / stalled tiers. **Gated behind #116/#115/#96.**
- **#120** — toast notifications for transitions a human must see.
- **#121** — a blocked leg renders as the bare token `blocked`; journal the
  `needs` reason (it is composed for the GitHub comment and then dropped).
- **#123** — a blocked issue does not render *as blocked*: `stage_for` rule 1
  lets a running (often phantom, per #116) leg outrank `+ai-blocked`.
- **#125** — codex review legs journal `status=""`, so verdicts are invisible and
  the approval-rate guardrail is blind (§6a). **Highest-priority of this set.**

Plus live evidence comments on **#92** (the `fix/89-issue` fallback branch,
observed exactly as predicted) and **#24** (below).

## 6a. ⚠️ The approval-rate guardrail is currently BLIND on codex (#125)

Found at 00:35Z, after the rest of this document was written, and it changes how
much this batch's numbers can be trusted.

Codex review legs journal `status: ""` instead of their verdict:

```json
{"ts":"...17:23:14Z","kind":"REVIEW","status":"REQUEST_CHANGES","number":106,"model":""}          <- claude
{"ts":"...23:25:48Z","kind":"REVIEW","status":"",              "number":111,"model":"gpt-5.6-sol"} <- codex
{"ts":"...00:19:59Z","kind":"REVIEW","status":"",              "number":122,"model":"gpt-5.6-sol"} <- codex
```

The fields traded places: **#91 fixed `model` and `status` is now the empty one.**
The reviews themselves are fine — verdicts reach GitHub, the rejection counter
advances — but:

- The dashboard cannot render "changes requested" (this is how it was found: PR
  #122 reads `CHANGES_REQUESTED` on GitHub and blank on the page).
- **First-pass approval rate — CLAUDE.md's primary power guardrail, defined as
  "review `agent_run.status` APPROVE vs REQUEST_CHANGES" — reads zero of both on
  codex.**

So the runtime switch that could degrade review quality also disabled the metric
that would detect it. **Treat every quality claim about batch 4 as unverified
until #125 lands.** #125 also asks for a drift test between the two adapters'
journal writes, because fixing these one operator-complaint at a time is what
produced this pair.

## 7. Two findings worth carrying forward

**Serial dispatch delays OBSERVATION, not just work** (commented on #24). Dispatch
is synchronous inside `Daemon.tick`, so while any leg runs no world is built and
*no* lifecycle transition anywhere is seen. PR #111 merged at 00:07:02Z and the
harness still had not noticed at 00:09:15Z because the fixer was 12 minutes into a
leg. This covers CI results, human replies, escalations and operator relabels, for
the full duration of every leg (routinely 6-12 min; wall cap 3600s). It also sets a
**floor on dashboard freshness** that #118/#119 cannot beat — nobody should "fix"
that by polling harder.

**The escalation ladder fired for the first time** (#41 asks whether its triggers
are reachable — they are). `agent #91: resolve — escalating 1 notch(es)
(resolve_retries=1): effort medium→high`, and the escalated attempt opened the PR
that merged. #41 can be closed with this evidence.

## 8. Loose ends, ordered

1. **#125 first** — the approval-rate guardrail is blind until it lands (§6a).
2. **#124 → then re-label #89 `ai-ready`.** The pricing fix is the highest-value
   thing in the queue and everything cost-related is wrong until it lands.
3. **#113's real class fix**, starting with the config-resolution probe (§3).
4. Restore `MAX_BUDGET_USD="20"` and `REVIEW_BUDGET_USD="8"` when #89 merges.
5. dev→main promotion (operator).
6. Close **#41** with the ladder evidence (§7).
7. Still-owed live evidence from batch 3: #82 wind-down, #71 resume-a-killed-session,
   #79/#84 stderr line bounds, #62 ceiling=true.
8. #108 (launcher bash segfault) did not recur this session, but the daemons are
   still under the bash launcher — systemd units remain the standing recommendation.

## 9. Reviewer posture — decided, and worth keeping

`REVIEWER_RUNTIME=codex` with `REVIEW_MODEL=gpt-5.6-sol` **stays**. It reviewed
PR #111 for $1.30 ledgered (~$0.20 real), requested changes, then approved the
revision — it is not rubber-stamping. The jam it caused was a harness defect, now
mitigated and filed, not an argument against the runtime.

Two standing caveats:

- Fixer and reviewer are now the **same model on the same runtime**, which
  CLAUDE.md warns can hide the failure it creates. It did not bite this session
  (none of the batch touched codex inference), but **#89 touches codex pricing** —
  when it is unparked, consider `REVIEWER_RUNTIME=claude` for that PR's review.
- Codex review cannot render `evidence/` screenshots. Dashboard issues with visual
  acceptance criteria (#118/#119/#120 especially) want a claude reviewer, or
  acceptance criteria written to be assertable from the journal.

---

# UPDATE — 00:30Z → 02:30Z (§§1–9 above are the 00:30Z snapshot)

Everything above still reads true except the state table in §1, which is
superseded by §10 below. Three things happened in this window: the codex weekly
cap was hit and cleared, #124 was killed by the budget ceiling and then succeeded
on its escalated retry, and two more issues were filed.

## 10. State at 02:30Z

| # | State | Note |
|---|---|---|
| **#91** | **MERGED** (PR #111) | codex/sol reviewed: REQUEST_CHANGES → APPROVE |
| **#92** | **MERGED** (PR #122) | |
| **#104** | **MERGED** (PR #126) | **its own fix — see the pull caveat in §12** |
| #124 | `ai-pr-open` P1 | PR opened by the escalated retry; **gates #89 and #113** |
| #113 | `ai-pr-open` P1 | PR #128 CHANGED_REQUESTED, deadlocked on #124's gate |
| #125 | `ai-working` P1 | dispatched 02:17:54Z |
| #102 | `ai-ready` P3 | |
| #89 | `needs-human-review` P1 | parked behind #124 (§4) |

**3 harness merges** this batch (#91, #92, #104). Note PRs #110, #114 and #130 also
merged in this window but are on `codex/dashboard-*` branches — operator-side
sessions, not harness legs. Do not count them as batch throughput.

**Spend: 29 paid legs, $221.93 ledgered ≈ $30 real** (≈7x, §5). One leg is a third
of that: see §11.

## 11. The #124 budget kill — #107 verified live, #104's anti-pattern verified live

```
01:47:08  agent_run RESOLVE  status=error_max_budget_usd  exit_reason=budget_exhausted
          model=gpt-5.6-sol  turns=133  cost_usd=81.717435  duration_s=2476
          usage: input 16,097,127  cache_read 15,729,152 (97.7%)  output 41,060
01:47:08  handover_written
01:55:15  RESOLVE #124 running — sol, effort=high (escalated)
02:1x     PR opened; #124 now ai-pr-open
```

Three separate findings from this one leg:

**#107 works.** The handover was written (6167 bytes) and the next leg's prompt
opened with `<!-- issue-agent-handover role=fix issue=124 -->`. First live firing.

**#107 buys knowledge, not cost.** It is not a session resume — the killed
session's 15.7M tokens of warm context are discarded and the new leg re-reads from
scratch. That is deliberate (resuming a budget-killed session would hand it a fresh
allowance, since the sampler subtracts the resumed baseline), but the accounting
should be stated honestly: the agent knows what it tried, and pays full price to
rediscover where.

**#104's perverse loop, observed 78 seconds after its own fix merged.** A leg died
for *spending too much*, and the ladder escalated it to `effort=high` on the same
top tier — which spends more. PR #126 (the fix) merged at 01:47:36; the escalation
decision was made at 01:47:54 by a daemon that had not pulled it. See §12.

**It then succeeded.** The escalated retry opened the PR, so the escalation was not
wrong in outcome — only in reasoning. Worth keeping in mind before treating #104's
fix as purely a cost win.

## 12. ⚠️ `git pull` on dev before the next batch

`fix: route budget exhaustion outside the agent strike ladder` (#104, PR #126)
merged at 01:47:36Z. The running daemons work from the local checkout, which has
not pulled, so **#104's fix is not live** and budget kills still count as agent
strikes. At the current inflated pricing budget kills are routine, so this matters
more than its impact label suggests. Pull, let the daemons hot-reload between
ticks, and verify #124's retry counter afterwards.

## 13. The codex weekly cap: hit, endured, cleared

```
01:47:47  ERROR: You've hit your usage limit ... try again at Aug 5th, 2026 2:09 PM.
          codex: rate limited (429) — attempt 1/5; resuming ... in  71.1s
01:49:00  attempt 2/5 ... in 128.1s
01:51:12  attempt 3/5 ... in 190.9s
01:54:54  agent #124: route — done (status=ok, $0.04)     <- operator upgraded; recovered on 4/5
```

The per-leg retry policy behaved correctly throughout: matched as a rate limit (not
an agent failure), the six-day `Aug 5th` reset was **not** honoured as a sleep
(bounded, window-aligned backoff), and the session was resumed rather than
restarted. It recovered on attempt 4 of 5 — four more minutes and it would have
died `TRANSPORT_DEATH` and requeued.

This sharpens **#129**: the backoff is right for a cap that clears; the gap only
bites when the cap does not. The pause trigger should key on a stated reset
timestamp days out, **not** replace the backoff. Full trace is on #129.

No re-login was needed — the upgrade reached the CLI's token on its own.

## 14. New config workaround (`config/repos/self.env`)

- `MAX_INFRA_DEATHS="50"` — **WORKAROUND(#129)**, set 01:34Z, live from the 01:47:11Z
  daemon restart, i.e. 36 seconds before the cap hit. This is the only reason no
  issue was parked `needs-human-review` during the outage. **Trade-off while live:
  genuine infra deaths (OOM, external kill) no longer escalate at 5 either — watch
  the log, not the ceiling.** Restore to unset once the plan upgrade is settled or
  #129 lands.

## 15. Two more issues filed (12 total this session)

- **#129** — provider quota exhaustion is handled per-leg, so an outage escalates
  every queued issue. Carries the full live trace from §13, including the
  reset-timestamp-as-pause-trigger finding. impact:high.
- **#133** — a ceiling-ended leg preserves its handover but never pushes its
  commits. #124's kill left **9 commits, clean tree, nothing pushed** — ≈$11 real
  of work no reviewer could see. Explicitly *not* "commit the WIP" (that would have
  to bypass the commit gate) and *not* #82's agentic wind-down (whose reasons for
  rejection still hold). The draft/unlabelled constraint is contract: a draft PR the
  reviewer adopts would corrupt the very guardrail #125 is restoring.

Also worth knowing: **#124 duplicates #117**, which the *fixer itself* filed at
00:06Z while blocked (author `xnmp`, label `harness`). #124 is the better-specified
of the two; close #117 as a duplicate (operator-only — the classifier blocks
assistant closes).

## 16. Revised loose ends

1. **`git pull` dev** (§12) — has a live clock on it.
2. **#125** in flight — restores the approval-rate guardrail (§6a). Nothing about
   this batch's review quality is measurable until it lands.
3. **#124 → then re-label #89 `ai-ready`** — #124 now has a PR, so this is close.
   It also unblocks #113, whose PR is deadlocked between reviewer demands and the
   gate.
4. **#113's real class fix** for the hook leak, starting with the config-resolution
   probe (§3).
5. Restore `MAX_BUDGET_USD="20"`, `REVIEW_BUDGET_USD="8"` (when #89 merges) and
   `MAX_INFRA_DEATHS` (when #129 lands or the plan is settled).
6. Close **#117** as a duplicate of #124; close **#41** with the ladder evidence
   (§7 — it has now fired twice).
7. dev→main promotion (operator).
8. Batch-3 owed evidence: #71 resume-a-killed-session, #79/#84 stderr line bounds,
   #62 ceiling=true. **#82's half is now answered** — see §11 and #133.
