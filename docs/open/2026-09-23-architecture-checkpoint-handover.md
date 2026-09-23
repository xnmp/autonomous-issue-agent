# Architecture checkpoint handover — 2026-09-23

Historical stop snapshot. The user subsequently resumed phase 3; see the
[current integration handover](2026-09-23-phase-3-paid-return-handover.md) and
[overall plan](2026-09-14-architecture-implementation.md). Live operations remain
paused; the implementation stop below no longer governs the resumed work.

## Stop condition

The user superseded “continue until the rest of the phases are complete” with
“actually can we just go to the next checkpoint and then write a handover doc”.
This is a **diagnostic working-tree checkpoint**, not a green implementation
checkpoint or completed phase. Stop here; do not resume agents, implementation,
deployment or live migration without new user direction.

- Checkout: `/home/chong/Repos/autonomous-issue-agent`, branch `dev`.
- HEAD: `ee7de45` (`persist ordered resolve continuations from executed admissions`).
- Phases 1–2 accepted; phase 3 incomplete; phases 4–6 not started.
- Existing integration and new test helpers remain uncommitted and must be preserved.
- This resumption added no production or test changes. It reproduced the known
  failure and refined the repair design; only checkpoint documentation changed.
- The resumed Astra `phase_3_gates` agent made no file changes, paused its phase
  goal and stopped. No new phase or implementation agents were started.
- Parent architecture goal is paused, not complete.

Read `CLAUDE.md` first. The [phase plan](2026-09-14-architecture-implementation.md)
defines acceptance; [the September 14 handover](2026-09-14-phase-3-integration-handover.md)
contains the full dirty-tree inventory and prior verification. This document
supersedes that handover's operational status, not its implementation inventory.
Authority design remains [proposed ADR 0015](../adr/0015-generation-fenced-gate-rearm.md).

## Fresh verification

Command:

```sh
UV_CACHE_DIR=/tmp/phase3-uv-cache uv run --offline --no-sync python -m pytest tests/app/test_dispatch.py::test_resolve_mid_dispatch_failure_persists_the_session_before_the_gh_read_fails -q --tb=short
```

Result: **1 failed**, exit 1. At `tests/app/test_dispatch.py:976`, SQL episode
continuation has no session after a paid result returns and a subsequent GitHub
read fails. Log: `/tmp/phase3-checkpoint-2026-09-23-repro.log`.
`git diff --check` passed. No full-suite rerun was performed this resumption.

Historical evidence, not a fresh green result: September 14's `make -k test`
passed 1,364 domain and 1,460 adapter tests; application tests had 2,394 passed
and this one failure. Other targets, import contracts and both Pyrefly passes
passed. Log: `/tmp/phase3-checkpoint-full-test.log`. The last green complete
suite belongs to HEAD, not the dirty tree. **Do not commit until `make test`
is green and independent adversarial review accepts the integrated changes.**

Read-only service inspection on September 23 confirmed all four units are
`inactive/dead`, `disabled`, `MainPID=0`:

- `issue-agent-fix-watcher-xnmp-tauri-explorer.service`
- `issue-agent-review-watcher-xnmp-tauri-explorer.service`
- `issue-agent-steward-xnmp-tauri-explorer.service`
- `issue-agent-dashboard.service`

No service state was changed. No target checkout edits, remote GitHub writes,
paid harness runs, live database migrations or activation occurred. The canonical
live DB remains `/home/chong/.local/state/issue-agent-control/control.db`.
Do not use the global `~/.local/bin/issue-agent` shim: it was previously found
to reference another checkout.

## Next bounded implementation, only after explicit resume

The failure is a dual-write boundary: accounting settles before post-run issue/PR
observation, while workflow continuation is recorded afterward. Do not weaken the
regression, treat failed reads as absence, or pay another run to recover its session.
The following design is **proposed, not implemented or accepted**:

1. Persist an immutable normalized returned-outcome receipt with actual admission,
   stage, supplied issue inputs and runtime/model/session provenance. Reuse existing
   receipt storage and open an exact-run observation gate atomically. Keep the raw
   session in the receipt; do not advance the episode continuation watermark yet.
2. Use complete, fresh typed issue/linkage observations and one pure stage-aware
   completion decision for both immediate handling and bounded recovery.
3. Atomically finalize classification, continuation and strike once, remove only
   the matching gate, and enqueue canonical follow-up commands in the existing
   outbox. Explicitly retire superseded receipts without effects or new allowance.

Concrete unresolved seams for the parent to settle before delegating:

- `SQLiteWorkflowControl._persist` currently assumes every command is linked to
  a pending re-arm. Separate snapshot persistence from re-arm-only linking so
  result finalization can reuse existing transactional command insertion.
- Existing comment-then-relabel sequences refresh remote revision between effects.
  Pre-enqueuing every leaf against one revision is unsafe. Choose a durable,
  revision-aware projection/continuation using existing effect infrastructure;
  no new action shape or command schema was selected this resumption.
- Capture after runtime return does not cover process death between settlement
  and capture. Decide the required atomicity and document the boundary honestly.
- Unsettled or never-launched runs cannot mint normal returned continuation.
- Closed-without-PR cleanup contains local actions; define idempotent replay and
  evidence, not only durable remote commands. Successful terminal cleanup should
  not count as a requeue strike.
- ADJUDICATE needs verdict/rationale and objection/reply/trail/uphold context;
  a stage receipt alone is not complete split-workflow recovery.

Preserve verified-PR precedence, including IMPLEMENT `contract_change`; infra and
budget exclusions; AUTHOR checkpoints; changed-input requeues; and distinct
productive/blocked RESUME semantics. Pending capture must not prevent finalized
blocked RESUME from clearing that same run's session. Validate canonical commands
against the pure decision and real settled admission, not caller-supplied claims.

Acceptance tests must cover each post-run read failing, restart without another
paid leg, single classification/strike/command set, blocked RESUME session clearing,
contract-change with an actual PR, unrelated gates surviving, superseded-generation
retirement, and unsettled results remaining held. Operator re-arm/retry, activation,
dashboard, legacy retirement and the second selected family remain phase-3 work.

Parent owns shared types, ports, codec/schema and cross-cutting signatures. Reuse
SQL admission, gates, receipts and outbox; domain stays pure, adapters own I/O,
app composes. Give every implementer the phase plan's conventions brief. Use
isolated databases, `uv`, Pyrefly and `apply_patch`; obtain independent review.
Do not start phases 4–6 before their documented dependencies are accepted.

## Preserved target decisions

- #696 stays blocked pending access to a real interactive Mac.
- #523 gets visible branch-status badges.
- #514 Gravatar is explicit opt-in, off by default with privacy disclosure.
- #722 concerns pasting an image copied from another app, not copying image files.
- #723's unsupported `trash:///` screenshot and shared attachment-fetch gap remain
  recorded in the earlier handover; no repair or new remote posting was performed.

Exceptional Astra/Fable implementer routing was already committed in `eaa1cc5`.
Keep these choices; they do not authorize restarting the paused harness.
