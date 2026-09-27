# Phase 4 handover after no-launch preflight proof

Date: 2026-09-27. Branch `dev`. The six-phase goal remains active: phases
1–3 accepted, phase 4 in progress, phases 5–6 pending. Read `CLAUDE.md`,
`AGENTS.md`, [phase-4 progress](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md), and
[post-return handover](2026-09-27-phase-4-post-return-handover.md).

## Accepted through raw return

Checkpoint 8 is committed at `8903af4`: exact child return output is captured
with settled money or an uncertain active reservation. Journal/accounting
context forwards only explicitly planned children. Full `make test` passed:
3,093 domain, 2,641 adapter and 2,863 app tests, plus hooks, governor,
parity, import contracts and Pyrefly. No live execution activation occurred.

## Current no-launch stop checkpoint

The CodexRunner local preflight-failure branch now stores typed
`review-execution-preflight-stop` evidence before any spawn. The receipt
binds the exact admission and complete normalized failure. Minting requires
the planned Codex runtime, reserved run, active reservation, spawn-pending
recovery and absence of launch, lease, charge, start time and raw return.
Launch and all release paths are fenced after the stop wins.

A matching stop settles synthetic zero charge and canonical raw output in
one transaction. Generic settlement and `complete_run` cannot bypass it.
Codex spend recovery can complete a pending stop after the caller dies,
using the immutable harness result. Terminal replay verifies the actual
charge and run provider receipt, and remains inert. The independent reviewer
reproduced/fixed historical-receipt scan and terminal provider-receipt
contradiction, then found no remaining blocker in this checkpoint. Focused
tests and Pyrefly passed. Final `make test` passed with 3,093 domain, 2,673
adapter and 2,863 app tests, hooks, governor, parity, four import contracts,
two Pyrefly passes and ten expected strict xfails. Local log:
`/tmp/phase4-stop-full.log` (ephemeral).

The stop receipt plus exact synthetic charge is positive no-launch evidence.
It has not yet been consumed into outer-round classification. A missing
launch receipt by itself remains unclassified. No real migration, daemon
cutover, paid run or phase-5 live window occurred.

## Next work

1. Persist a pre-reservation refusal receipt for a planned child without
   fabricating a paid run. Join it to exact parent/round/child authority and
   fence duplicate or changed refusal.
2. Classify child evidence from immutable stop, launch, return, charge,
   cancellation, release, refusal and parent-abandonment sources. Seal only
   after every planned child has a terminal proof; no terminal run state or
   absent receipt is sufficient alone.
3. Finalize the outer round in one transaction against the canonical v25
   execution component and actual submission outcome. Preserve failure
   ceiling, session and display distinctions from the pure classifier.
4. Resolve Codex transport retries before runtime cutover: one admitted
   child/run currently permits one guarded launch, while `_exec_once` may
   spawn multiple attempts under one run ID. Offline supervised retry
   acceptance must exercise the chosen typed attempt or per-run scheme.

Run full `make test` before any coherent commit. No live service restart,
target checkout mutation, paid harness run, real activation or migration,
publication/push, or phase-5 live measurement is authorized.
