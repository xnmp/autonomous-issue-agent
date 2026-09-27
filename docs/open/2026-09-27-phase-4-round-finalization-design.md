# Phase 4 — review round finalization proposal

Date: 2026-09-27. **Proposed next pure-domain checkpoint.** Companion to the
[execution design](2026-09-27-phase-4-review-execution-design.md) and
[implementation conventions](2026-09-27-phase-4-conventions.md). This document
does not activate execution authority or authorize a runtime/schema cutover.
The parent owns shared values, signatures and implementation sequencing.

## Evidence and stage boundary

One durable preflight identity owns the outer round, including free work.
Record the selected mode and each stage's immutable planned child identities
before admitting its children. Adaptive validator stages are recorded before
launch. A successful gate may select a productive short circuit instead of
planning dimensions; absent downstream children are not evidence of failure.

Keep stage selection, child accounting/return evidence and the outer seal
distinct. Terminal observed children do not prove the parent finished planning
or selected a final verdict. A live, unsealed parent remains open. Recovery may
seal an abandoned round only after authoritative parent/claim abandonment and
fencing against late child admission or submission registration. Reuse the
existing claim, lease, run and effect machinery; introduce no scheduling queue.

Actual launch and abandonment provenance remains under investigation. A run
ID, reservation, arbitrary receipt ID, generic terminal status or synthesized
`AgentResult` is not sufficient proof that paid execution started. Adapters
must establish the applicable launch, return, release or abandonment facts
before constructing trusted domain evidence. Ambiguous spend remains held for
the existing recovery path; it never becomes a fabricated unlaunched release.

There is also a distinct **verified no-launch but settled** case: Codex runtime
preflight can return `RUNTIME_UNAVAILABLE` before spawning, while accounting
records a synthetic zero-cost result as completed/settled with a provider
receipt ID. Neither that receipt nor completed/settled status proves launch.
The existing `proven_unlaunched_run` describes released runs without charges
and cannot classify this case. Add explicit no-launch-settled provenance and
an appropriate non-charging outcome; preserve its actual accounting receipt
and never rewrite settled accounting as released to fit an existing variant.

Register a submission's immutable command-to-round relationship before a
remote write can occur. Recovery must distinguish no submitted command from
an unresolved command whose remote write may have landed. A free-copy refusal
does not seal the round: the existing workflow may fall back to a paid review.
Record that branch decision before admitting its children.

## Finalization precedence

1. Reject contradictory target, binding, stage or provenance facts. Stale
   generation returns settle money and remain audit without changing current
   execution authority.
2. Wait while relevant children are live, launch/accounting/return evidence is
   unresolved, or a submission is pending/unknown. A seal does not turn missing
   evidence into a negative result. A live unsealed parent also remains open.
3. A matching, verified productive submission selects productive progress.
   An incomplete multi-review can still submit real blocking findings; failed
   siblings do not add a second charge beside that productive round.
4. Without productive submission, explicit proven operator/shutdown
   cancellation is non-charging once the required terminal evidence is present.
5. Preserve current pin-before-sibling-death precedence: a pin-deferred round
   advances its one durable pin streak, even when earlier gate or sibling
   invocations were paid. Do not also charge their failures.
6. For other nonproductive paid rounds, a verified deterministic death selects
   saturation at its captured configured ceiling; ordinary failure adds one
   strike. Multiple failed children still produce one outer disposition.
7. With no actual launched child, use verified admission refusal, unlaunched
   reservation release, no-launch-settled evidence, unpaid abandonment or
   bounded preflight evidence.
   Never synthesize paid-failure evidence from a free refusal.

Verified preparation failures remain a separate bounded evidence obligation
alongside any nonproductive outcome, including paid failure, pin deferral or
cancellation. They do not add a second allowance strike. A mixed paid gate
and unlaunched runtime failure must retain both the outer-round classification
and its configuration evidence. Confirmed productive progress has precedence;
failed sibling evidence remains in the closure audit without opening a new
preparation schedule for that completed round.

Finalization and exact replay must be idempotent. Conflicting final outcomes
for one round refuse rather than replacing accepted history. A verified
productive receipt cannot be overwritten by a later cancellation notification.

## Classification table

The following rows assume consistent binding/provenance, settled required
children and a valid parent seal or proven abandonment. Otherwise the result
is awaiting evidence rather than one of the final dispositions.

| Observed facts | Outer disposition and allowance effect |
| --- | --- |
| Confirmed matching submission; at least one actually launched child | Paid productive progress; reset applicable preceding failure debit and pin streak once. Failed siblings do not add strikes. |
| Confirmed copied verdict; no paid child | Copied productive progress; same ordered reset, without inventing an admission. Preserve session and displayed round. |
| Explicit operator/shutdown cancellation; no confirmed submission | Non-charging cancellation; actual spend still settles. |
| Pin deferral at gate, dimension or validator; no productive submission or overriding cancellation | One pin-deferred outcome. First five deferrals after applicable progress are free under current policy; later deferrals add one strike each. Earlier paid gate/sibling spend is retained. |
| Nonproductive round with an actually launched child and verified deterministic death | One saturation debit: `max(existing failures, captured ceiling)`, not addition of the ceiling. Current disabled/zero ceiling behavior is an ordinary single strike. |
| Other paid error, incomplete review without usable verdict, or definitively refused submission; at least one launched child | One ordinary failure strike for the outer round. |
| Successful paid gate followed by admission refusals/releases that prevent a productive verdict | Paid nonproductive round: actual launched work determines paid status; an aggregate budget/refusal status cannot make the whole round free. |
| All relevant children refused before reservation, with no launch | Admission-refused outcome; no strike. Retain each applicable refusal's evidence. |
| Relevant reservations proven never launched and released, with no launch | Unlaunched release outcome; no strike. Retain all applicable release evidence. |
| Verified runtime preflight stopped before spawn, but accounting completed/settled a synthetic result | Distinct no-launch-settled outcome; no strike. Preserve the settled accounting receipt; a provider receipt ID is not launch proof. |
| Mixed pre-admission refusals, unlaunched releases and verified no-launch-settled results, with no launched child | Non-charging outcome with the complete child evidence set; do not arbitrarily select one child's receipt as proof for every child. Exact value shape remains to be designed. |
| Proven abandoned parent with no paid child, no unresolved submission and no more specific pin/refusal result | Unpaid abandonment; no strike. |
| Unreadable revision-bound diff or unavailable containment before launch | Bounded evidence/configuration hold through the existing evidence schedule/gate; no fabricated paid failure. Exact typed control/outcome extension remains to be settled. |

## Continuation and displayed round

Only a confirmed productive **eligible normal reviewer** updates the reusable
reviewer session. A verified compatible handle replaces it; a successful
eligible invocation returning no handle clears an older handle, as required by
the reviewed execution design. A runtime that cannot preserve the review
sandbox requires fresh continuation.

Auxiliary gate, dimension and validator sessions never become the reviewer
continuation. Paid gate/multi productive progress advances the displayed round
but preserves the real reviewer session. A copied verdict preserves both.
Failure, pin deferral, admission refusal/release and cancellation preserve both.
In particular, a normal reviewer handle returned before a refused POST does
not become authoritative continuation without productive settlement.

Session and displayed-round updates retain independent applicability
watermarks. `Preserve` does not advance the session replacement watermark.
Use durable round sequence, not delivery order, timestamps or run-ID sorting.
Late evidence cannot resurrect a session cleared by a newer applicable result
or erase failures from rounds after an older productive result.

## Required next-checkpoint tests

- A live/unsealed parent and unresolved submission cannot finalize as failure.
- A paid gate completing before parent death does not prove unplanned fan-out
  complete; proven abandonment fences late admission/submission before sealing.
- A confirmed blocking review wins over incomplete/failed siblings and charges
  no additional failure; a late cancellation cannot undo it.
- Multiple ordinary/deterministic deaths charge once; deterministic saturation
  preserves a larger count rather than adding the configured ceiling.
- Pin plus ordinary/deterministic sibling failure preserves pin precedence,
  including paid gate/siblings and the sixth-deferral boundary after restart.
- Mixed paid-success/refused siblings is paid no-progress; all-unlaunched mixed
  refusals/releases is non-charging with complete evidence.
- An unreadable diff or containment failure produces bounded evidence/config
  control without invented launch or paid-failure proof.
- A verified pre-spawn `RUNTIME_UNAVAILABLE` result remains non-charging even
  with completed/settled accounting and a synthetic provider receipt; it is
  neither a paid failure nor a released reservation. Repeated unavailable
  runtime/configuration/preflight evidence reaches the existing bounded hold
  path before cutover.
- Free-copy refusal remains open until fallback selection; confirmed free copy
  resets applicable prior debit while preserving session/display state.
- Productive normal reviewer Replace/Clear differs from auxiliary or failed
  producers; reordered results preserve the independent watermarks.
- Exact seal/outcome replay is inert; conflicting replay and malformed,
  cross-round or incomplete child evidence refuse.

Real launch, process-loss, accounting and barrier acceptance belongs to the
later composed checkpoint; pure classification tests cannot prove those facts.

## Current code references

`ReviewWorkflow._review_multi` selects pin deferral before examining sibling
errors, and can synthesize a blocking verdict from an incomplete review.
`_gather_legs` currently flattens raised child exceptions, so classification
must recover verified provenance from durable child evidence. `_progress` and
the session/round writes follow confirmed submission. `AccountingRuntime.run`
can refuse before reservation, while `_recover_or_release` distinguishes safe
unlaunched release from retained uncertain spend. `decide_reservation_recovery`
preserves live or unchecked authority. `Dispatcher.dispatch` saturates the
existing counter for deterministic death and excludes explicit shutdown.
