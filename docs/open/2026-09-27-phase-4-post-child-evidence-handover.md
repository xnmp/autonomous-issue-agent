# Phase 4 handover after non-launch child evidence

Date: 2026-09-27. Checkpoint commit `7e407d0` on `dev`. The six-phase goal
remains active: phases 1–3 accepted, phase 4 in progress, phases 5–6 pending.
Read `CLAUDE.md`, `AGENTS.md`, [phase-4 progress](2026-09-27-phase-4-progress.md),
the [execution design](2026-09-27-phase-4-review-execution-design.md),
[round finalization design](2026-09-27-phase-4-round-finalization-design.md),
[persistence map](2026-09-27-phase-4-persistence-map.md), and
[execution provenance](2026-09-27-phase-4-execution-provenance.md).

## Accepted state

`SQLiteControlStore.read_review_child_evidence(repository, owner_id, round_id)`
now reads the canonical plan and transaction-local child sources by exact
round/child/run indices. It returns `ReviewChildPending`, `ReviewChildRefused`,
`ReviewChildReleased` or `ReviewChildSettledWithoutLaunch` where their full
source proof exists. It rejects conflicting or corrupt receipts and future
revisions. An unlaunched review release now stores
`ReviewUnlaunchedRelease` atomically with its run/budget transition; the
reader requires this receipt plus `proven_unlaunched_run`. The release reason
is hashed into the receipt to preserve the existing API's long/multiline
reason contract. A Codex preflight stop remains pending until the exact
synthetic zero charge and raw return are settled.

Launched children stay pending even if a validated raw return exists. This
reader is a coherent offline source boundary, not a complete
`ReviewRoundFacts` builder or outer-round closure. The live review daemon
does not call it. The planning-seal writer from checkpoint 11 remains an
offline Store API. No real activation, target mutation, service restart,
paid harness run, migration or push occurred.

Independent Sol review found and verified the release reason compatibility
fix; a fresh pass found no remaining material issue. Final `make test` passed:
3,094 domain, 2,720 adapter, 2,863 app tests; hooks, governor, parity, four
import contracts and both Pyrefly checks. Ten strict xfails remained
expected. Log: `/tmp/phase4-child-evidence-final-full.log` (ephemeral).

## Next boundary: launched child result evidence

Extend the same transaction-local reader to construct `ReviewChildReturned`
only from an exact guarded launch, matching raw return and actual settled
charge. Keep active/retained spend and absent raw returns pending. Map the
returned envelope to a justified typed success, ordinary/deterministic
failure or operator/shutdown cancellation. Do not infer deterministic death
from prose or a generic terminal run state; require its retained source
receipt. Cancellation requires matching `run_cancellation_intents` or another
positive source. For a successful normal reviewer, derive a reusable session
only when the captured runtime's capabilities and exact launched source make
it eligible. Gate, dimension and validator sessions cannot become the normal
reviewer continuation. Add tests for settlement/reconciliation order,
malformed charge or raw return, stale generation, process death, cancellation,
and multiple children with one pending sibling.

Then implement exact parent/claim abandonment and a late-admission/submission
fence. An unsealed live parent cannot finalize from terminal children; an
absent claim by itself does not establish safe abandonment. Registration of
submission command-to-round relationships and reconciliation of uncertain
submissions are still absent. Only a complete source-backed round may be
passed to `finalize_review_round` and stored with its outcome/projection in
one transaction. Resolve the Codex `_exec_once` multiple supervised-attempt
identity before runtime cutover.

The standing restrictions in the original
[review-slice handover](2026-09-27-review-slice-handover.md) still apply:
no live service restart, target checkout mutation, paid harness run, real
activation, publication/push or phase-5 live window without explicit
authorization. Run full `make test` before each coherent commit. Use Sol or
Luna for independent reviews, as requested by the user.
