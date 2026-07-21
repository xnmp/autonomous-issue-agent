# Reviewer spawns a fresh CLI process every round instead of holding one session alive

**Labels:** enhancement, reviewer, performance, reliability

## Problem

Each review dispatch (`review`, `re_review`, and every multi/delegated leg) goes through
`AgentRunner.run`, which calls the SDK's **one-shot `query()`** — that spawns a brand-new
`claude` CLI subprocess per call, boots the `claude_code` system-prompt preset, and
re-hydrates state from scratch. There is no long-lived client kept across rounds or polls.

- `src/issue_agent/adapters/agent_runner.py:247` — `query(prompt=…, options=…).__aiter__()`
  (one query = one subprocess = one review turn, then teardown).
- `src/issue_agent/app/planners/tasks.py:465` — `re_review` *does* pass `resume=session_id`,
  so in **SINGLE** mode the prior transcript is replayed by id rather than re-summarised by
  hand. But resume still: (a) spawns a fresh process, (b) re-bootstraps the preset, (c)
  re-loads and replays the whole transcript on the server side each round.
- **MULTI / DELEGATED** modes fan out fresh sessions per dimension/leg with no resume — they
  genuinely re-gather context (diff + prior review body) every round.

### Impact

1. **Cost + latency.** Every re-review round re-pays process startup, preset bootstrap, and
   (SINGLE) full transcript replay. On a PR that takes N rounds to converge this is N× the
   fixed overhead.
2. **Fragility at result-delivery.** Observed 2026-07-21 (Myapp #352/#354): reviews
   frequently return `is_error=True, cost=$0.00, "no ResultMessage from query()"`
   (`agent_runner.py`), so the whole review is computed then lost and the daemon
   retries from a cold process. **NOTE — root cause corrected:** this was initially
   blamed here on a gateway "Stream closed" transport drop; that was WRONG. It's the
   SDK 0.2.110 background-Task stream never yielding a terminal `ResultMessage` —
   see **`review-no-resultmessage-background-tasks.md`** for the evidence and fix.
   A persistent warm session helps the *cost* of retrying, but the actual result-loss
   bug is fixed there, not here.

## Proposed fix

Hold a persistent `ClaudeSDKClient` (streaming/interactive client) per PR review thread for
the life of the review, instead of a fresh one-shot `query()` per round:

- Open the client once when a PR first enters review; send round-1 as the first message.
- On new commits, send the incremental diff as the **next message on the same live client** —
  no new process, no preset re-boot, no transcript replay (the context is already resident).
- Close the client when the PR merges / closes / escalates to `needs-human-review`, or after
  an idle TTL.

This is an `AgentRunner`/runtime change (add a streaming path alongside the one-shot `run`),
plus review-workflow wiring to keep the client handle in review state rather than only a
`session_id` string. `AgentRuntime` is already a port (hexagonal), so the one-shot vs.
persistent choice can live behind it.

### Interim mitigation (separate, smaller)

Independent of the persistent-session work: add a **bounded immediate retry** on the
`no-ResultMessage` transport-drop in the review workflow (2–3 tries with short backoff before
giving up the poll), mirroring the codex 429 retry pattern already in
`adapters/codex_runner.py`. Cheap; stops burning a full cold review per transport drop.

## Acceptance criteria

- [ ] A multi-round review of one PR uses a **single** long-lived CLI session (verify: one
      process for the review's lifetime, not one-per-round).
- [ ] Re-review after new commits sends only the incremental diff on the live session; no
      full transcript replay / preset re-bootstrap per round.
- [ ] A transport drop at result-delivery is recoverable **without** recomputing the whole
      review from cold (retry resumes the warm session, or the bounded-retry mitigation
      lands).
- [ ] Session is torn down on merge / close / escalation / idle-TTL (no leaked processes).
- [ ] MULTI / DELEGATED modes reconsidered: either share a warm session or document why
      per-leg fresh sessions are intended.

## Evidence (2026-07-21, example-agent)

Triangulated by ~8 controlled repros against PR #354:

| Task variant | Result |
|---|---|
| "reply OK" (no tools) | ✓ $0.20 |
| real review prompt + tools, **no schema** | ✓ $0.94 |
| short prompt **+ schema** | ✓ $0.21 |
| full review (real prompt + tools + schema) | ✗ $0.00, "no ResultMessage" (5/5, ~3–4 min then drop) |
| 2-tick debug **daemon** on #354 | ✓ posted CHANGES_REQUESTED @ 10:06 AEST |

Ruled out (each by experiment, not assumption): `CLAUDE_CODE_*` env leak from the launching
interactive session, the governor hook (`hook_0`), budget cap, and cwd/`setting_sources`.
The failure correlates with **long session + schema-forced final tool roundtrip through the
model gateway on CLI 2.1.191**, dropping at result-delivery — which a warm persistent
session directly reduces.
