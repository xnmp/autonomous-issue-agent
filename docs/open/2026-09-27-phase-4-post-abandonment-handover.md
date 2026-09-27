# Phase 4 handover after budget and parent-abandonment checkpoints

Date: 2026-09-27. Branch `dev` at `4d05f64`. The persistent six-phase goal
remains active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending.
Read `CLAUDE.md`, `AGENTS.md`, the [phase-4 progress record](2026-09-27-phase-4-progress.md),
[execution design](2026-09-27-phase-4-review-execution-design.md),
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md) and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted work in this continuation

`d747b70` makes a settled, guarded launched `BUDGET_EXHAUSTED` child terminal
without charging a quality strike. `ReviewBudgetExhausted` is an explicit
outer outcome that keeps launch and spend provenance, survives canonical
codec/closure reload, and preserves allowance, pin streak, session and display.
Confirmed submission, cancellation, pin deferral, failed siblings and refused
paid submission keep their established precedence. `TRANSPORT_DEATH` remains
pending until exception/trace provenance and a classifier receipt exist.

`4d05f64` adds `seal_abandoned_planning`, sourced from the exact parent claim
creation and released/reclaimed terminal receipts. It writes the planning
state and immutable fence atomically; the canonical loader validates both and
rejects orphan or altered fences. A successor review owner can finish an old
round through the durable owner lineage. New stage registration and child
admission refuse the abandoned planning state, even after claim replacement.

Active execution `SUBMIT_REVIEW` commands now refuse at enqueue and begin
until command-to-round registration is atomic. This is a deliberate temporary
guard: it prevents a remote review from crossing an untracked round boundary.
The existing unbound active-family guard remains effective. Historical
unresolved-submission tests now create their command before the offline
activation fixture.

Independent Sol review found and verified the successor path and submission
guard. Its fresh pass found no supported remote-write bypass. Final
`make test` passed: 3,121 domain, 2,747 adapter and 2,863 app tests; hooks,
governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-abandonment-final-full.log` (ephemeral). Worktree was clean after
the code commits. No live activation, target mutation, service restart, paid
harness run, migration or push occurred.

## Next implementation boundary

1. Add atomic `review-round-submission` registration in the same SQLite
   transaction that appends the immutable `ReviewSubmissionPlan` and enqueues
   the lifecycle command. A post-enqueue registration is unsafe because the
   executor may begin the remote write first. `adapters/effects.py`
   `submit_bound_review` constructs and enqueues today; `app/review_runtime.py`
   `submit` supplies the verdict. Capture the exact round, owner claim,
   execution/rejection bindings, command kind (copied or paid), head and
   current aggregate revision before enqueue. Replay must require the same
   plan and command. A free copied-verdict refusal must permit an explicitly
   planned paid fallback, without treating the first refusal as closure.
2. Replace the temporary active-execution submission refusal with a
   transaction-local exact registration check at enqueue, begin and retry.
   Registration must refuse after abandoned planning. An already registered
   uncertain command must remain pending for reconciliation; abandonment
   does not turn it into a negative result.
3. Add the production recovery caller for open rounds whose exact parent
   occurrence has terminated. `seal_abandoned_planning` is currently an
   offline API called only by tests. Use indexed open-round scans and the
   successor-owner lineage; do not infer abandonment from an absent claim or
   terminal children alone.
4. Build transaction-local complete `ReviewRoundFacts`, including
   `ReviewChildNotAdmitted` only after the matching abandonment fence and
   command-specific submission evidence. Commit closure, outcome and ordered
   projection together. Productive reset must share the existing
   `record_review`/`open_ceiling` settlement transaction, before its later
   `settle_submission` marker.
5. Resolve transport-death exception/trace classification with durable
   deterministic versus ordinary infrastructure proof, and Codex `_exec_once`
   multiple supervised attempts. Complete phase-4 migration/recovery/
   acceptance, then phases 5–6.

The original [review-slice handover](2026-09-27-review-slice-handover.md)
continues to forbid real activation, live migration, service restart, target
mutation, paid runs, push/publication and phase-5 live windows without explicit
authorization. Run `make test` before every coherent commit. Use Sol or Luna
for subagents, as the user requested.
