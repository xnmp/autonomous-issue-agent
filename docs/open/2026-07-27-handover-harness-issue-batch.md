# Handover — harness issue batch (#16 landed, four issues left)

Written 2026-07-27, end of the session that followed
[the fourth-deployment handover](2026-07-27-handover-fourth-deployment.md).
That handover's watch-list now has field data (see §3); its deployment batch on
`xnmp/tauri-explorer` ran and its issues resolved. **It has not been graduated to
`history/`** — whoever confirms its per-item verifications are all discharged
should move it.

This session resolved harness issues only. No target-repo work.

---

## 1. Read this first — the repo is in a state that will confuse you

**`origin/main` is 5 commits BEHIND local `main`.** The following exist only in
the working checkout at `/home/chong/Repos/autonomous-issue-agent`:

```
c995e55 feat(codex): pin reasoning effort (default medium)
27565d9 fix(codex): ledger per-leg usage deltas, not the resumed cumulative counter
2c187c0 feat(resilience): survive big file reads, stop retrying deaths that can't win (#13)
7f0fbf6 fix(config): the AGENT_ENV file, not the wrapper's env, is the knob authority (#12)
2bcb99d feat(gate): block oversized evidence images at commit time (#13)
```

Consequences you will trip over:

- **PR #18 shows 9 commits, not 4.** Its own work is the top four; the five
  above are inherited base. This is not a mistake in the PR — #16's codex
  rendering builds directly on c995e55's `reasoning_effort` plumbing, so it
  genuinely needs that base. Merging #18 lands all nine.
- **Issue #12 was closed** citing 7f0fbf6, which is accurate for local `main`
  and for PR #18, but a reader looking at `origin/main` will not find it.
- **Decide deliberately**: either push `main` (making #18 a clean 4-commit PR),
  or merge #18 and accept that it carries the five. Do NOT rebase #18 onto
  `origin/main` — it would drop the base its own change depends on.

---

## 2. What this session did

### #16 — escalate tier/effort on evidence (PR #18, OPEN, unmerged)

Branch `fix/16-escalate-on-evidence`. Not merged, not deployed.

`domain/effort.py` (new, pure) owns an ordered `Effort` ladder and a
`(tier, effort)` `Stance`. `EscalationSignals` counts evidence the harness
already records — `recurrence` label (#10), the resolve-retry counter, and
outstanding CHANGES_REQUESTED reviews on the PR — and `escalate` spends it
**effort first (cheap), tier second**, capped at `(complex, high)`.

- `AgentTask.effort` defaults to `None` = *say nothing*, so every leg the ladder
  never touches builds a byte-identical task. Rendered per runtime at the edge:
  codex `-c model_reasoning_effort=`, the Claude SDK's native `effort` option
  (dropped with a log on builds predating the field).
- The **resolve family** re-prices the model; **repair legs get effort only** —
  swapping the reasoner under a live transcript is a different bet.
- Knobs: `FIX_EFFORT` (runtime-agnostic base; blank = leave each runtime's own
  default alone, so `CODEX_REASONING_EFFORT` is never silently overridden) and
  `ESCALATE_ON_EVIDENCE=0` as the kill switch.
- Journals `route_escalation` — deliberately NOT `escalation`, which already
  means handed-to-a-human and feeds the escalation-rate guardrail.

**Open decision for the next session** (raised, not settled): the ladder is
**default-ON**. Per the repo's evidence-triggered discipline it arguably should
land dormant — `ESCALATE_ON_EVIDENCE=0` in the active overlay — until journal
data shows it moves recurrence rate without moving $/merge. §4 argues it should
go live instead. Make the call explicitly; do not let it default by inattention.

### #12 — closed, no code written

Already fixed by 7f0fbf6 (see §1 for the caveat). `from_env` re-resolves an env
carrying `AGENT_ENV` against that FILE, so a removed knob falls back to the
dataclass default instead of a stale inherited export. The bidirectional
guardrail the issue asked for is
`tests/adapters/test_config.py::test_removed_knob_falls_back_to_default_not_inherited_value`.

---

## 3. Field data — what the journals actually say

Pulled 2026-07-27 from the live `xnmp/tauri-explorer` deployment. **All three
daemons are RUNNING** (`pgrep -f "issue_agent[.]app[.]main"` → fix/review/steward).

Two caveats before trusting any number: costs on the codex runtime are
**synthetic dollars** (read them as a token meter), and 10 RESOLVE / 7 REBASE /
1 FIX_CI entries predate 066e570 and are ledgered at **$0**, so the totals
UNDERSTATE resolve and rebase specifically.

**Fix-side cost mix by leg** (`<state>/journal.jsonl`, `agent_run` events):

| leg | n | cost |
|---|---|---|
| RESOLVE | 27 | $247.55 (10 at $0) |
| ADDRESS_REVIEW | 3 | $203.08 |
| FIX_CI | 2 | $39.57 |
| REBASE | 12 | $8.82 (7 at $0) |

**Repair legs are ~49% of ledgered fix spend across five sessions.** CLAUDE.md
names this exact ratio as a target ("repair legs are pure overhead; their share
should shrink"). One ADDRESS_REVIEW on PR #552 cost **$146** on its own.

**Review side**: 22 REVIEW ($38.69) + 11 RE_REVIEW ($17.95). Verdicts: 7
APPROVE, 4 REQUEST_CHANGES, 2 raised. First-pass approval ≈64% — this is the
guardrail #16 must leave flat.

**Today (UTC) across all roles: $449.11.** Two `spend_block` events
(`daily:2026-07-26`, `daily:2026-07-27`) and one `escalation` (issue #541).

Reproduce:

```bash
J=~/.local/state/issue-agent-xnmp-tauri-explorer/journal.jsonl
jq -rc 'select(.event=="agent_run")|[.kind,(.cost_usd//0)]|@tsv' $J \
  | awk -F'\t' '{s[$1]+=$2;n[$1]++} END{for(k in s) printf "%-16s n=%-3d $%.2f\n",k,n[k],s[k]}'
```

---

## 4. Why the #16 evidence supports going live

The repair-leg share above IS the trigger #16 was built for. Two of the three
signals it consumes fired on this deployment: PR #552 took multiple
ADDRESS_REVIEW rounds at $146 apiece, and 4 of 13 reviews requested changes.
Under the ladder those second rounds run at HIGH effort on the same model —
the cheap lever — before anything re-prices a tier.

The counter-argument is that escalation raises cost on exactly the issues
already costing the most, so it could deepen the hole rather than close it. The
measurement that settles it: after one deployment day with the ladder on,
compare `route_escalation`-carrying legs against their own first attempt.
Rounds-per-PR should fall; if $/merge rises without rounds falling, switch it
off. **Do not stack another efficiency change until that reads out.**

---

## 5. Remaining harness issues — recommended order

| # | Title | Why this order |
|---|---|---|
| **#17** | Spend-cap escalation comments not deduped | **Do first.** Smallest, has a live reproduction (3 identical comments on PR #552 in 90s; unbounded overnight), and the dedup pattern already exists twice — `hold` (#6) and `recurrence` (#10) are both journal-keyed once. Mechanical. |
| **#15** | Steward issue grooming | Highest leverage on operator time. The prior handover's batch-picking section is manual triage that this automates. Advisory-only (zero auto-closes) so the blast radius is a label + comment. Shares the recurrence-triage substrate. |
| **#14** | Steward architecture-gardener | Same subsystem as #15 — do them adjacently or together; both add a bounded scheduled steward pass with cadence/budget knobs. #14's cheapest input is recurring REQUEST_CHANGES themes, which the review journal now carries (#7). |
| **#11** | Babysitter role | Large. Its own text names #1 as a prerequisite. Needs a diagnostics playbook first or tier 2 degenerates to "restart and hope". |
| **#1** | Webhook push | Explicitly trigger-gated: build when rate limiting recurs *despite* POLL_SECONDS=60 + the REST-ETag probe, or when a second target repo multiplies poll cost. Check whether that trigger has fired before touching it. |

---

## 6. Working notes for the next session

- **Adversarial review earned its keep and should be repeated.** Three rounds on
  #16 found two P1s. The first: an entire evidence class never fired, because it
  read the store's `review-retries` counter, which re-keys to 0 on every new
  review — it reads 1 forever. The second was in the *fix* for the first: repair
  legs got a placeholder tier to climb from, so the journal reported
  `to_tier=complex` on a leg that ran `sonnet`, corrupting the very
  group-by-tier split CLAUDE.md makes the routing guardrail.
- **The root cause of how the first P1 hid is a test-design lesson**: the test
  hand-set store state that the real loop never reaches. Tests for anything
  counter-driven must drive the actual workflow, not manufacture its state.
- **Hot reload watches the MAIN checkout's HEAD**, not a worktree's. Work in a
  worktree freely; the moment you merge and pull into
  `/home/chong/Repos/autonomous-issue-agent`, all three daemons restart between
  ticks. Merge #18 deliberately, not mid-queue.
- `make test` = 1131 tests + gate parity + import-linter. `uvx pyrefly check
  src/issue_agent` has **one pre-existing error** (`workflows/review.py:108`,
  `object is not awaitable`) — unrelated, unfixed, don't be alarmed by it.
- Carried forward from the prior handover and still true: `pgrep -f` needs a
  char-class breaker under the rtk hook; gh label search is stale for closed
  objects; codex OAuth binds the browser's live ChatGPT session; the daily spend
  window is per UTC day.
- Still unfiled: a `bin/metrics.sh` printing the per-day scoreboard. §3's jq is
  the third time that query has been hand-written. File it if you write it again.
