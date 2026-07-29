# What is still open — 2026-07-26

The single answer to "what's left?". Everything here is **deliberately** open:
known, assessed, and not fixed for a stated reason. If you find something open
that is *not* on this list, that is a gap in this document, not an accepted risk.

Sources consolidated here (all now historical — do not re-read them for status):
the 2026-07-25 adversarial review, its 2026-07-26 re-review §4, the remediation
plan §6b, the lean-core handover §5, and the improvement plan's §4 backlog.

---

## 1. Blocking anything — before you point this at a repo you care about

Nothing in the code. The P0/P1 findings from both reviews are closed
(`44fbd16` → `1968888`). What remains is **operational**, and all three matter
on a first real run:

### 1.1 Your spend caps may be structurally inert
`ISSUE_SPEND_CAP_USD` and `DAILY_SPEND_CAP_USD` sum the cost ledger, and the
ledger only gets a line when a run reports `cost_usd > 0`. On a subscription
plan the SDK reports `0.00` — **the caps can never fire, for any loop shape**.
Codex is no longer in that bucket by default: `CODEX_COST_PER_MTOK_IN/OUT`
default to gpt-5.6-sol list price (harness #4), so codex runs ledger synthetic
dollars and the caps bound codex token volume — but explicitly setting both to
`0` re-blinds them. The harness emits one loud warning when settled runs keep
costing $0 while a cap is armed (review P1R-5) — believe the warning, not the
`$0.00` on `issue-agent status`.

*What actually bounds spend on those plans:* `AGENT_MAX_RUNTIME_SECONDS` (the
wall cap, per session), the per-leg retry breakers, and `MAX_INFRA_DEATHS` /
`MAX_UNPRODUCTIVE_REVIEW_ROUNDS`. A priced codex deployment also enforces
`MAX_BUDGET_USD` in the harness by killing a leg when its rollout counters cross
the threshold; zeroing either token price or setting `CODEX_ENFORCE_BUDGET=0`
disables that dollar bound and emits a startup warning. Set the wall cap
deliberately rather than inheriting 2700s.

### 1.2 The governor has an open indirect-execution bypass
`sh -c '…'`, `bash -c`, `python -c`, `eval` and `xargs` classify as *nothing*,
so a payload inside them skips every gate — including the push gate. The fixer
runs `permission_mode=bypassPermissions`, where the governor plus
`disallowed_tools` are the only policy left.

**Consequence:** do not point this at a repo whose issues or PR comments come
from people you do not trust. Self-authored issues are fine.

Recorded as five `xfail(strict=True)` tests in `tests/test_governor.py` (they
flip to failures the day it is fixed). Root cause and the two viable fixes are
in the re-review §4 (`docs/history/2026-07-26-p0-rereview.md`); the work is
backlog P3.2.

### 1.3 It has never run against real GitHub
Every one of the ~1,500 tests fakes the gh boundary. The first real run tests
the fidelity of those fakes as much as it tests the harness. Start with a
throwaway repo, `ISSUE_AGENT_MAX_TICKS=1`, and one trivial self-authored issue.

---

## 2. Accepted divergences and known-imperfect behaviour

| Item | Why it's accepted | Where it bites |
|---|---|---|
| Push gate: bash denies on an unreadable PR state, Python runs the state checks and allows a clean push | P1R-12 was decided for the Python path only; changing the codex path's fail-closed stance needs its own call | codex mode only; asserted in `tests/test_gate_parity.py` |
| Commit-arm rule *order* differs between stacks (lint before ordering vs interleaved) | Verdicts are identical, only the reported reason differs | cosmetic; asserted as a parity divergence |
| `git -C /other/worktree push` is gated against *this* worktree's state | The destination branch is now correct (P1R-10); per-cwd context is a larger change | rare; documented in `_push_gate` |
| Process death is bounded but **unbilled** | Counting it needs a pre-await store write on the hot path; the journal reconstruction (P0R-4) bounds it without one | ledger under-reports after SIGKILL/OOM |
| `resolve_test_cmd` prefers operator `TEST_CMD`; the agent's `.harness/test-cmd` is the fallback | P1R-18 — the gated subject must not choose the gate's teeth | a monorepo's scoped command is ignored when `TEST_CMD` is set |
| Journals/ledger never rotate | Fine for months at current volume | add rotation when a file is annoying |
| launchd installer still has the /tmp-logs and colliding-unit-names bugs its systemd twin fixed | No mac deployment yet | macOS only |
| A human closing a PR unmerged leaves `resolve-retries` standing, which now also pins every later resolve to a pricier tier (#16) | `clear_issue_resume_state` keeps the counter deliberately — "fewer retries after a human closed the PR" — and the escalation ladder reads the same counter. The evidence is honest (an attempt did complete and did not ship); the cost consequence was decided for the retry budget, not for routing | an issue a human bounced resolves at a higher tier/effort until it merges; clear `<STATE_DIR>/issue-N.resolve-retries` to reset |

---

## 3. Trigger-gated backlog

Do **not** build these without their trigger — that rule is why the lean core
stayed lean. Full table in `docs/history/2026-07-25-improvement-plan.md` §4.

| Work | Trigger |
|---|---|
| P4.9 cross-leg prompt-cache sharing | `agent_run.usage.cache_read_input_tokens` ≈ 0 across real runs |
| P4.1 model routing (`build_router_task` is dead code kept for this) | median cost of `trivial`-labelled issues ≳ $1 |
| P4.2/P4.3 review cost work | review legs are a top-2 ledger line |
| P2.1–2.3 reconciliation / leases | a stranded-label or state-drift incident recurs *despite* systemd + reaper |
| P3.2 governor tiers | before onboarding any repo with untrusted inbound issues (see §1.2) |
| Ledger rotation + cached daily total | dispatch latency, or ledger > ~50MB |
| Webhook push (`gh webhook forward`) + poll demoted to slow reconciler (#1) | GraphQL rate limiting recurs despite POLL_SECONDS=60 and the ETag pre-check (first hit 2026-07-26, review+steward on one token at 20s polls) |
| P6.1 legacy deletion (`agent/ review/ steward/`, `bin/bootstrap.sh`, bash suites) | opportunistic — **note the partition:** `plugin/hooks/checks/` is NOT deletable, it is live for codex mode and now covered by the parity suite |

---

## 4. Structural debt (P2) — gated on a week of real operation

`S1` lifecycle transitions · `S2` FixLeg descriptor · `M18` runner extraction ·
`S3`/`S4` module splits · `S5` executor wiring · `S6` selector consolidation ·
`S7` leftovers · `S8` AGENT.md/prompt duplication.

Detail in the 2026-07-25 review §6. The plan deliberately gates all of it on
operating experience: none of it is required for correctness, and the shape of
the right abstraction depends on which legs actually get added.

Two smaller ones worth doing whenever the files are next touched:

- `build_gate_context` / `main.py` should take `lint_cmd`/`typecheck_cmd`/`adr_dir`
  directly, so `governor.complete_gate_context` can be deleted (it fills fields
  the builder leaves at named inert sentinels, so removing it is a no-op).
- `cli.journal_path` is a compat shim beside `journal_paths`; migrate the last
  caller and drop it.

---

## 5. Per-incident notes

`docs/open/issues/` holds three unresolved incident write-ups
(codex SIGKILL on e2e verify, persistent review sessions, review
`no ResultMessage` on background tasks). They predate the reviews above and
have not been re-validated against the current tree.
