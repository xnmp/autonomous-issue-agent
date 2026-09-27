# Phase 4 handover after normal round-planning seal

Date: 2026-09-27. Checkpoint commit `8d707a7` on `dev`. The six-phase goal
remains active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending.
Read `CLAUDE.md`, `AGENTS.md`, [phase-4 progress](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md), and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted state

Checkpoint 10 (`17752f0`) retains final planned-child budget refusals without
allocating paid runs. Checkpoint 11 (`8d707a7`) adds the parent-owned normal
planning seal. `SQLiteReviewExecutionControl.seal_completed_planning` requires
the exact preflight, registered work, live parent PID/start ticks and claim
occurrence, current dual binding and aggregate revision. It probes process
identity outside SQLite and rechecks it in the write transaction. That
transaction stores `ReviewPlanningSealed(ReviewPlanningComplete)`, the
`review-round-seal` receipt and the new aggregate version atomically.

Exact seal replay is inert after claim release. The seal stops stage extension
but allows already planned children to be admitted and launched while the
same parent claim remains live. `verified_review_completed_seal` is shared by
the seal writer and admission/launch. The canonical suffix loader requires its
receipt and rejects orphan, mismatched and future-revision seals. The seal
does not classify children or finalize an outer outcome. It is currently an
offline Store API; the live review daemon does not call it.

Independent Sol review found no material issue, including after loader
hardening and extra post-admission tests. Focused stage, admission, launch,
return, runtime and persistence tests passed. The final `make test` passed:
3,094 domain, 2,706 adapter, 2,863 app tests; hooks, governor, parity, four
import contracts and both Pyrefly checks. Ten strict xfails remained expected.
Final log: `/tmp/phase4-seal-final-full.log` (ephemeral). An earlier full run
also passed but started before the last revision-bound edit; use the final log
as the gate evidence. No real activation, target mutation, service restart,
paid harness run, migration or push occurred.

## Next boundary: receipt-backed terminal evidence

The pure `ReviewRoundFacts`/`finalize_review_round` classifier exists but no
production adapter constructs its child facts or stores a completed closure.
Build one transaction-local, round-bounded reader that derives each child
variant from positive source receipts and exact indexed identities:

- `ReviewChildRefused`: the child-keyed budget refusal with matching full
  child, claim occurrence, owner and current round plan; no paid admission.
- `ReviewChildReleased`: the exact admitted run, positive
  `proven_unlaunched_run` predicate and retained release transition/receipt;
  absence of a lease alone is insufficient.
- `ReviewChildSettledWithoutLaunch`: the Codex preflight-stop receipt joined
  to its exact synthetic zero charge, settled reservation and raw return;
  the stop itself is insufficient until accounting completes.
- `ReviewChildReturned`: exact guarded launch, typed raw return and actual
  settled charge, then a justified success/failure/cancellation and eligible
  session source. A terminal run state or output text cannot mint launch or
  productive review evidence.
- `ReviewChildPending`: any unresolved process, spend, return or submission
  evidence. Keep uncertainty pending rather than flattening it to failure.
- `ReviewChildNotAdmitted`: only after a proven parent-abandonment fence for
  the exact claim occurrence, with no child admission/refusal.

The reader must validate cross-row agreement before constructing domain facts
and use round/run/child indices rather than JSON scans or owner-lifetime
history. Preserve contradictory evidence as an error, not as a fallback
variant. Add behavioral tests for mixed multi-child rounds, absent/corrupt
receipts, retained spend, stale generation, parent death and concurrent
admission/abandonment.

An open round cannot be finalized just because its current children are
terminal: a live parent may still append validators. The next writer must
prove and retain exact parent/claim abandonment and fence late admission or
submission registration in the same transaction. Normal planning seal is
already available for the live-parent path. Registration of a submission's
command-to-round relationship and reconciliation of uncertain submissions
are still absent. Only complete receipt-backed closure facts may be passed to
the pure finalizer and persisted with the selected outcome/projection.

Codex `_exec_once` may retry supervised processes while the current planned
child/run admits one guarded launch. Resolve this typed attempt identity
before runtime cutover and exercise it offline.

Run full `make test` before each coherent commit. Medium/high-risk changes
need a fresh independent reviewer; use Sol or Luna as requested by the user.
The standing restrictions in the original
[review-slice handover](2026-09-27-review-slice-handover.md) still apply:
no live service restart, target checkout mutation, paid harness run, real
activation, publication/push or phase-5 live window without explicit
authorization.
