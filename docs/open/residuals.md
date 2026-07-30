# What is still open — 2026-07-30

The single answer to "what's left?". Everything here is **deliberately** open:
known, assessed, and not fixed for a stated reason. If you find something open
that is *not* on this list, that is a gap in this document, not an accepted risk.

Sources consolidated here (all now historical — do not re-read them for status):
the 2026-07-25 adversarial review, its 2026-07-26 re-review §4, the remediation
plan §6b, the lean-core handover §5, and the improvement plan's §4 backlog.

---

## 1. Blocking anything — before you point this at a repo you care about

Nothing in the code. The P0/P1 findings from both reviews are closed
(`44fbd16` → `1968888`). The remaining restrictions are **operational**, and
the first two matter whenever inbound repository content is not trusted:

### 1.1 Publication credentials are still present in agent processes

SQLite reservations, commands, receipts, and process leases are authoritative,
and the governor now denies command shapes it cannot resolve. Supported agent
push/PR flows go through `issue-agent-publish`, with durable commands, remote
readback, and receipts. That does not make an agent-local hook an authorization
boundary. Claude/Codex/GLM children still run in the daemon's publication
environment; an agent that bypasses the advisory hook, or recovers credentials
through filesystem/local-socket access, can still publish directly.

**Consequence:** do not point this at a repository whose inbound issue, PR,
review, or CI text is supplied by people you do not trust. The trigger-gated
end state is a credential-free agent plus a separately privileged artifact
publisher; see ADR 0001 and the capability matrix.

### 1.2 Agent-written claims are not authorization evidence

The PR gate no longer reads `.harness/ac-posted`, `test-plan-posted`, or
`adversarial-verdict`. Acceptance criteria remain visible in the PR body,
test results come from the trusted executor, and adversarial review is process
discipline rather than self-attestation.

The remaining `.harness/visual-ac` claim can only make the gate stricter by
scheduling committed-screenshot verification; `.harness/test-cmd` selects a
command but never supplies its result. Neither can authorize publication.

### 1.3 The first real GitHub canary is complete

The 2026-07-31 release canary used a self-authored issue in
`xnmp/autonomous-issue-agent`. It exercised durable label effects, paid Codex
admission and settlement, an active SIGTERM shutdown, handover persistence,
process cleanup, and the dashboard projection. The original issue labels were
restored and the final control-store check found no live runs, reservations,
or leases. The immutable receipt is
[live-release-canary-evidence.json](../reference/live-release-canary-evidence.json).

This closes the first-run fidelity risk; it does not replace a bounded canary
when onboarding a different repository or credential topology. Start each new
deployment with `ISSUE_AGENT_MAX_TICKS=1` and a trivial self-authored issue.

---

## 2. Accepted divergences and known-imperfect behaviour

| Item | Why it's accepted | Where it bites |
|---|---|---|
| Push gate: bash denies on an unreadable PR state, Python runs the state checks and allows a clean push | P1R-12 was decided for the Python path only; changing the codex path's fail-closed stance needs its own call | codex mode only; asserted in `tests/test_gate_parity.py` |
| Commit-arm rule *order* differs between stacks (lint before ordering vs interleaved) | Verdicts are identical, only the reported reason differs | cosmetic; asserted as a parity divergence |
| `git -C /other/worktree push` is gated against *this* worktree's state | The destination branch is now correct (P1R-10); per-cwd context is a larger change | rare; documented in `_push_gate` |
| `resolve_test_cmd` prefers operator `TEST_CMD`; the agent's `.harness/test-cmd` is the fallback | P1R-18 — the gated subject must not choose the gate's teeth | a monorepo's scoped command is ignored when `TEST_CMD` is set |
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
| P3.2 governor tiers | before onboarding any repo with untrusted inbound issues (see §1.2) |
| Webhook push (`gh webhook forward`) + poll demoted to slow reconciler (#1) | GraphQL rate limiting recurs despite POLL_SECONDS=60 and the ETag pre-check (first hit 2026-07-26, review+steward on one token at 20s polls) |

---

## 4. Structural debt (P2) — gated on a week of real operation

Remaining structural work is limited to the trigger-gated credential-free
publisher and further decomposition driven by a week of real operation.

Detail in the 2026-07-25 review §6. The plan deliberately gates all of it on
operating experience: none of it is required for correctness, and the shape of
the right abstraction depends on which legs actually get added.

Two smaller ones worth doing whenever the files are next touched:

- `build_gate_context` / `main.py` should take `lint_cmd`/`typecheck_cmd`/`adr_dir`
  directly, so `governor.complete_gate_context` can be deleted (it fills fields
  the builder leaves at named inert sentinels, so removing it is a no-op).
- Historical schema-v1 JSONL readers remain only as a read-only dashboard/status
  converter. They are never queried for admission, deduplication, process
  ownership, or effect authorization.

---

## 5. Per-incident notes

`docs/open/issues/` holds three unresolved incident write-ups
(codex SIGKILL on e2e verify, persistent review sessions, review
`no ResultMessage` on background tasks). They predate the reviews above and
have not been re-validated against the current tree.
