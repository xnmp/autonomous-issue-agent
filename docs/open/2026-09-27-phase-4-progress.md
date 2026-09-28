# Phase 4 — continuation implementation record

Phase 3 is accepted at `a7214bf`; ADR/plan acceptance is recorded in `5abacab`.
Phase 4 follows the [review execution design](2026-09-27-phase-4-review-execution-design.md),
[conventions](2026-09-27-phase-4-conventions.md) and
[persistence map](2026-09-27-phase-4-persistence-map.md).
The [post-stage handover](2026-09-27-phase-4-post-stage-handover.md)
records the next transaction boundary and standing restrictions.
The latest [post-productive-settlement handover](2026-09-28-phase-4-post-productive-settlement-handover.md)
records the current boundary and remaining work.
The [generation-boundary finding](2026-09-28-phase-4-generation-boundary.md)
records a reproduced active-execution rearm blocker and its reviewed design.
Next-checkpoint preparation is recorded in the
[outer-round finalization design](2026-09-27-phase-4-round-finalization-design.md)
and [execution provenance map](2026-09-27-phase-4-execution-provenance.md).
No real activation, target change, service restart or paid run is authorized.

## Checkpoint 1 — pure execution projection (accepted)

`domain/review_execution.py` defines validated immutable execution bindings,
round/child/launch identities, runtime-tagged session provenance, explicit
session/display updates, finalized outer outcomes and ordered projection.
Projection is independent of delivery order and compacts only a contiguous
finalized prefix. Free copied verdicts reset failures/pin streak while preserving
session/display state. Deterministic death saturates the captured ceiling;
multiple child failures produce one outer debit. Old-generation outcomes are
audit only. Conflicting duplicate outcomes are refused.

Independent review reproduced policy drift between full replay and replay from
a compacted prefix. The checkpoint now retains its episode's pin policy;
projection has no policy override. Independent re-review found no remaining
blocker in this checkpoint's scope.

Verification: 88 new behavioural tests include all 120 delivery permutations
of an ordered history with incremental compaction, explicit expected allowance
and continuation outcomes, conflicting replay, gap retention, policy capture,
stale generation and malformed/bounded inputs. Final `make test` passed with
2,957 domain, 2,401 adapter and 2,861 app tests, plus hooks, governor,
configuration/environment/parity, all four import contracts and both Pyrefly
passes. Ten pre-existing strict-xfail residuals remained expected. Local log:
`/tmp/phase4-execution-domain-final-full.log` (ephemeral). The first full run
caught a missing architecture-manifest entry; the corrected rerun was green.

This checkpoint does not activate execution authority or alter runtime
dispatch. Schema remains v24. It does not yet classify open rounds, uncertain
submissions or returned child evidence. The next boundary must validate actual
receipt provenance before minting these finalized values, including eligible
reviewer returns for session changes; launch alone does not establish eligibility.

## Checkpoint 2 — pure round finalization (accepted)

`domain/review_rounds.py` defines immutable stage and
submission plans, explicit launch/return/refusal/release/no-launch-settlement
facts, planning seals and abandoned-parent fences, and pure finalization.
Independent review reproduced loss of configuration evidence in mixed paid
and unlaunched rounds; the correction retains bounded preparation follow-up
alongside the one outer outcome. Independent re-review accepted the corrected
classifier and its tests. All 180 focused projection/finalization tests pass.
Final `make test` passed with 3,049 domain, 2,401 adapter and 2,861 app tests,
plus hooks, governor, configuration/environment/parity, all four import
contracts and both Pyrefly passes. Ten pre-existing strict-xfail residuals
remained expected. Local log: `/tmp/phase4-review-rounds-full.log` (ephemeral).

This checkpoint remains pure construction: schema v24 and existing runtime
execution authority are unchanged. The next persistence checkpoint must re-derive outcomes from retained
closure facts, never accept a caller-constructed outcome as authority.

## Checkpoint 3 — canonical persistence (accepted)

The checkpoint adds an explicit inactive/active execution component to the
review snapshot, a tagged PR episode envelope, schema v25 component/round/run
indices and durable dispatch-claim occurrence identities. Existing resolve
episode encoding remains unchanged. A frozen v24 observation fixture protects
historical review receipt replay through the new snapshot tag.

The active PR loader joins the canonical envelope, execution activation and
component index. It validates ordered round suffix outcomes against retained
closure facts, refuses missing parents and unresolved compacted prefixes, and
requires an exact retained compaction receipt for a noninitial checkpoint.
Dispatch claim creation, release and reclaim retain occurrence-scoped receipts;
the external liveness probe is revalidated under the write transaction.

Independent review reproduced two corruption paths: an absent parent with
retained component/round rows, and a checkpoint moved over an open round.
Both now refuse, with targeted regressions. The reviewer found no remaining
blocker in the current migration/loader/claim-lifecycle scope. Final `make test`
passed with 3,085 domain, 2,481 adapter and 2,861 app tests, plus hooks,
governor, configuration/environment/parity, four import contracts and both
Pyrefly passes. Ten pre-existing strict-xfail residuals remained expected.
Local log: `/tmp/phase4-persistence-final-full.log` (ephemeral). Runtime round admission,
settlement and real execution activation are later checkpoints; no execution
authority has been activated.

## Checkpoint 4 — unpaid round preflight (accepted)

`domain/review_preflight.py` selects one bounded round from the active dual
binding, complete fresh PR observation, captured failure ceiling, session
capability and current review holds. `adapters/review_execution_control.py`
reads the PR and linked issue outside SQLite's write lock, proves the exact
live parent claim occurrence and creation receipt, then rechecks current
binding, aggregate version, linked issue SQL holds, unresolved submissions and
release projection in one transaction. That transaction allocates one round
sequence, stores its initial plan and planning state, advances the aggregate
version, and records an exact replay receipt. No run, lease or budget
reservation is created. Exact replay remains inert after later child-stage
planning or claim release.

The existing dispatch and the new preflight use one transaction-local source
for unresolved-submission and release holds. Independent review reproduced
uppercase production claim-leg mismatch, missing lifecycle holds, a dead
parent process accepted from a retained claim row, and replay conflict after
stage planning. Each has a behavioral regression; re-review found no remaining
blocker in this unpaid boundary. Final `make test` passed with 3,093 domain,
2,521 adapter and 2,863 app tests, plus hooks, governor, gate parity,
four import contracts and both Pyrefly passes. Ten pre-existing strict xfails
remained expected. Local log: `/tmp/phase4-preflight-final-full.log`
(ephemeral). The first two full runs caught missing architecture-manifest and
test-tier layout entries; the corrected run was green.

This checkpoint does not register child stages, admit paid work, prove launch
or return, seal rounds, activate the execution family or cut over the daemon.

## Checkpoint 5 — immutable planned stages (accepted)

`SQLiteReviewExecutionControl.register_stage` appends one validated stage and
its planned children to an open round under the exact preflight receipt, live
parent claim occurrence, active dual binding and current aggregate revision.
It advances the shared version once, with no paid run or reservation. Exact
replay is inert; changed children, sealed planning and stale authority refuse.
The append and an immutable `review-round-stage` receipt commit together.

The active loader and stage writer both require an exact two-way match between
the bounded stage list and its receipt set. Receipt keys use an indexed
round-scoped range, so one active round does not scan every repository stage.
Independent review reproduced missing prior receipts and orphan receipts that
otherwise permitted an append over altered history; both now refuse. The
reviewer found no remaining blocker in this stage-registration scope. Final
`make test` passed with 3,093 domain, 2,547 adapter and 2,863 app tests, plus
hooks, governor, gate parity, four import contracts and both Pyrefly passes.
Ten pre-existing strict xfails remained expected. Local log:
`/tmp/phase4-stage-final-full.log` (ephemeral).

This checkpoint does not admit or launch any child. The next boundary must
join a planned child and exact parent occurrence to run/budget reservation in
one transaction; reservation alone cannot prove launch.

## Checkpoint 6 — paid planned-child admission (accepted)

`ReservationRequest` now captures a typed planned review child alongside its
rejection binding. Active review execution requires that child; inactive
execution retains the earlier bound-review path. The reservation writer probes
the selected parent process outside SQLite, then rechecks its exact claim
occurrence, creation and terminal receipts, current dual binding, open plan,
stage receipts, linked issue holds, submission/release holds, failure ceiling
and reviewed-head baseline inside the budget transaction. Retained workflow
authority is checked before admitting any unbound legacy review run.

One transaction now attaches the round and child to the run, reserves money,
records the parent claim/run link, advances the aggregate revision and mints an
immutable `review-execution-admission` receipt. Exhaustion and late insertion
failures roll back every effect. Exact replay compares the captured child,
round, binding, receipt and canonical revision; changed work conflicts.
Independent review reproduced and verified fixes for current allowance and
baseline changes, forged future admission revision, and missing activation
rows despite retained execution authority. Reservation records admission
only; lease registration and launch evidence remain the next boundary.
Final `make test` passed with 3,093 domain, 2,577 adapter and 2,863 app
tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly passes. Ten strict xfails remained expected. Local log:
`/tmp/phase4-admission-verified-full.log` (ephemeral).

## Checkpoint 7 — guarded review child launch (accepted)

`register_process_lease` now joins attached review child admission, active
reservation, open round, current dual binding, exact parent claim/run link,
stage receipts and current SQL holds before writing a launch receipt. It
probes the parent and supplied child PID/start ticks and process group outside
SQLite under deployment effect authority, then revalidates the captured
authority in the lease transaction. One transaction retains immutable
`review-execution-launch` evidence with the lease, running run state and
running recovery policy. The receipt names the exact admission and process.

Exact existing-lease replay validates the receipt and is inert even after the
parent claim releases or the registered child process exits. A missing or
changed receipt refuses; a cleared lease cannot mint a second launch.
`register_lease` cannot bypass the guarded path for an attached child, and
release-before-registration wins over late launch. Independent review
reproduced a fabricated child PID accepted at receipt minting; the child OS
identity probe closes it. A real-clock subprocess test caught receipt/lease
timestamp precision drift on replay; registration now uses the persisted
millisecond timestamp for every copy.

The receipt proves guarded process registration and possible provider
execution. It does not prove provider exec, tokens billed, a returned session
or a productive review. Those facts belong to later settlement and submission
receipts. No real execution authority or daemon cutover has been activated.
Final `make test` passed with 3,093 domain, 2,605 adapter and 2,863 app
tests, plus hooks, governor, gate parity, four import contracts and both
Pyrefly passes. Ten strict xfails remained expected. Local log:
`/tmp/phase4-launch-final-full.log` (ephemeral).

## Checkpoint 8 — raw review return and accounting capture (accepted)

`domain/review_return.py` defines a bounded, unclassified child result. A
review attachment now flows through journal and accounting context into paid
admission. Admission checks the planned runtime before the child runs.
`AccountingRuntime` retains the raw result with either the actual settled
charge or an active reservation whose accounting remains uncertain. The
review settlement transaction commits charge and immutable
`review-execution-return` receipt together; exact replay is inert and changed
output or money conflicts. A reconciler that settles first can still accept
the exact raw return against its stored charge. An accounting calculation
failure retains raw output and the reservation instead of dropping both.

Return capture validates exact child admission, historical stage/preflight
runtime and launch receipt independently, including after parent release,
generation change or lease cleanup. Raw output does not classify review
success, failure, session reuse or outer-round outcome. The remaining
no-launch branch needs its own positive preflight-stop proof before it can
authorize `ReviewChildSettledWithoutLaunch`.

Independent adversarial review reproduced and verified fixes for
reconciler-first output loss, corrupt-runtime replay, runtime drift at
admission, missing launch after cleanup, invalid accounting calculation and
contradictory live lease/launch evidence. Behavioral regressions cover each.
Final `make test` passed with 3,093 domain, 2,641 adapter and 2,863 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
passes. Ten strict xfails remained expected. Local log:
`/tmp/phase4-return-full.log` (ephemeral). No live activation occurred.

## Checkpoint 9 — verified runtime stop before launch (accepted)

`CodexRunner` records a typed `review-execution-preflight-stop` receipt only
on its local preflight-failure branch, before any supervised child is spawned.
The receipt binds the exact paid admission and complete normalized failure
envelope. Its transaction requires a reserved active run, spawn-pending
recovery, the planned Codex runtime, and no process, launch, charge or raw
return. Receipt creation changes no money. Guarded registration and every
reservation release path refuse a stopped child; exact stop replay is inert.

Accounting settles a matching stop as a synthetic zero charge with canonical
raw output in one transaction. It verifies the stop, no launch, reserved
state and actual charge identity, including on replay. Generic settlement
cannot settle a stopped child. A crash after stop receipt can be completed by
the regular Codex spend reconciler from the retained harness envelope; a
pending stop retains its active reservation and is never reclassified as an
unlaunched release. This positive stop and exact zero charge can later support
`ReviewChildSettledWithoutLaunch`, while a missing launch receipt alone cannot.
The receipt does not classify the outer round or activate live execution.

Independent adversarial review reproduced and verified fixes for receipt
lookups that scanned historical stop rows and a terminal replay that accepted
a contradictory run provider receipt. Fresh review found no remaining blocker
in this scope. Final `make test` passed with 3,093 domain, 2,673 adapter and
2,863 app tests, hooks, governor, gate parity, four import contracts and both
Pyrefly passes; ten strict xfails remained expected. Local log:
`/tmp/phase4-stop-full.log` (ephemeral).

## Checkpoint 10 — final planned-child budget refusal (accepted)

`AccountingRuntime` records a pre-reservation refusal only after the final
`EXHAUSTED` result, following the bounded reconciliation attempt and retry.
`SQLiteControlStore.record_review_admission_refusal` rechecks the live parent,
open planned child, current dual binding, holds and every cap balance in one
write transaction. It advances the aggregate revision and stores a child-keyed
`ReviewChildAdmissionRefusal` receipt naming the exact request, claim occurrence,
limiting scope and period, committed amount, requested maximum and cap. It
creates no run, reservation, charge, claim attachment or lease. If headroom or
authority changes before the refusal transaction, accounting returns a
nonterminal authority refusal rather than an outdated budget verdict.

Admission and refusal use the same child key under SQLite's write lock. A
child-keyed admission index remains alongside the run-keyed admission receipt,
so retained admission evidence still fences a refusal if its run row is lost.
Exact refusal replay validates the request, preflight claim, cap scope and
amount, revision bound and content-addressed receipt ID; exact paid admission
replay and launch cross-check the child index against the run receipt. The
digest detects ordinary row corruption within the trusted SQLite authority;
it is not an independent tamper seal.

Behavioral tests cover final refusal without paid state, exact and changed
replay, cap/parent drift, admission/refusal exclusion, secondary limiting
scope, transactional rollback, corrupt receipts, no provider launch and a
first trip followed by successful reconciliation admission. Independent Sol
review found and verified fixes for refusal replay validation and child-index
consistency, then found no further material issue. Final `make test` passed
with 3,094 domain, 2,689 adapter and 2,863 app tests, hooks, governor, gate
parity, four import contracts and both Pyrefly passes. Ten strict xfails
remained expected. Local log: `/tmp/phase4-refusal-full.log` (ephemeral).
No live activation, target mutation, service restart or paid harness run occurred.

## Checkpoint 11 — parent-owned normal planning seal (accepted)

`SQLiteReviewExecutionControl.seal_completed_planning` now closes a round's
registered stage/submission list under the exact preflight, live parent claim
occurrence, active dual binding and current aggregate revision. The parent
process is probed outside SQLite and rechecked in the write transaction.
The same transaction advances the aggregate revision, stores
`ReviewPlanningSealed(ReviewPlanningComplete)` and an immutable
`review-round-seal` receipt. Exact replay is inert after claim release;
changed facts, stale revisions, replaced parents and partial receipt writes
refuse without changing the round.

The seal stops new stages. It still permits admission and guarded launch of
children already in the plan, including when the parent sealed before paying
them. `workflow_rows` validates the receipt for every completed sealed round
in the canonical suffix, rejecting missing, conflicting and orphan receipts
and future captured revisions. Admission and launch verify the same seal.
The seal records planning completion only; it does not classify a child,
submission or outer outcome. Other planning conclusions and abandoned-parent
fences remain later work.

Behavioral tests cover sealing before/after child admission, post-seal launch,
late-stage refusal, exact replay, missing/changed receipts, stale and replaced
parent claims, replacement during the OS probe, transaction rollback and
canonical loader corruption. Independent Sol review found no material issue
after re-review. Final `make test` passed with 3,094 domain, 2,706 adapter and
2,863 app tests, hooks, governor, parity, four import contracts and both
Pyrefly checks; ten strict xfails remained expected. Local log:
`/tmp/phase4-seal-final-full.log` (ephemeral). No live activation occurred.

## Checkpoint 12 — receipt-backed non-launch child evidence (accepted)

`SQLiteControlStore.read_review_child_evidence` reads one canonical planned
round under one SQLite transaction. It joins the child-keyed refusal or
admission index to the exact preflight, owner, claim occurrence, round plan,
run and budget rows. It returns typed pending, refused, released or
settled-without-launch child facts. It rejects conflicting admission/refusal,
future receipt revisions, missing admission indices, orphan paid children,
incomplete unlaunched proof, and contradictory stop, launch, return or money
evidence. A pending Codex preflight stop remains pending until its exact
synthetic zero charge and raw return are committed together.

For an unlaunched review run, `_release` now atomically records a typed
`ReviewUnlaunchedRelease` alongside the budget/run transition. Its bounded
receipt includes the exact admission, release time and a SHA-256 digest of
the original release reason; the reason remains in the budget row. This
preserves the public release API's acceptance of long and multiline reasons.
The reader requires both that immutable receipt and
`proven_unlaunched_run`; absent lease or released money alone is insufficient.

Launched children deliberately remain `ReviewChildPending` here, even after a
validated raw return: this checkpoint does not yet classify result,
deterministic death, cancellation or reusable session eligibility. It does
not construct `ReviewRoundFacts`, finalize outcomes or activate live review
execution. Positive abandonment, submission and closure writers remain open.

Behavioral tests cover mixed multi-child rounds, pending-to-terminal
transitions, corrupt/future receipts, release receipt write rollback, long
and multiline release reasons, exact synthetic settlement, missing money,
forged launch with a stop, and orphan admission indices. Independent Sol
review found and verified a fix for the release reason compatibility issue;
its fresh pass found no remaining material issue. Final `make test` passed
with 3,094 domain, 2,720 adapter and 2,863 app tests, hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-child-evidence-final-full.log` (ephemeral). Commit `7e407d0`.
No live activation, target mutation, service restart or paid harness run
occurred.

## Checkpoint 13 — settled normal child returns (accepted)

`read_review_child_evidence` now constructs `ReviewChildReturned` only after
the exact guarded launch, indexed admission, immutable raw return, settled
budget and matching actual charge agree. A charge that arrives before its raw
return, or a raw return while spend remains active, stays pending. The reader
accepts the documented reconciled-spend order once both sources are present,
without treating a terminal run status alone as proof. Contradictory money or
source rows refuse.

Pure `classify_normal_review_return` derives successful normal output and
ordinary agent errors from the typed runtime envelope. A reusable session
comes only from a successful normal reviewer on a runtime that can resume
while preserving the review sandbox, and its source is the exact launched
child. Auxiliary outputs and invalid handles cannot become continuation.
A settled shutdown cancellation requires the exact same-run persisted
`run_cancellation_intents` row, matching dispatch/kind and the harness
shutdown reason. Operator cancellation without that proof stays pending.

Sol review found that external kills, runtime unavailability and budget
exhaustion were initially headed toward an ordinary strike. The classifier
now keeps those and transport death pending until their specific
non-striking or deterministic disposition is supported. The fresh Sol pass
found no further material fault. Tests cover settled success/failure,
reconciliation order, absent return/charge, corrupt money, session
eligibility, cancellation intent and all four non-striking exit reasons.
Final `make test` passed with 3,107 domain, 2,732 adapter and 2,863 app
tests, hooks, governor, parity, four import contracts and both Pyrefly
checks. Log: `/tmp/phase4-launched-return-full.log` (ephemeral). Commit
`8ac97be`. No live activation, target mutation, service restart or paid
harness run occurred.

## Checkpoint 14 — non-striking infrastructure result (accepted)

A settled launched child with a raw `KILLED_EXTERNALLY` or
`RUNTIME_UNAVAILABLE` result now produces typed
`ReviewChildInfrastructureFailure`. A sealed round whose launched children
include that failure, with no ordinary failed child or refused paid
submission, produces `ReviewInfrastructureDeath`. Its ordered projection
preserves the failure allowance, pin streak, session and displayed round
while retaining the actual launched children and their separately settled
charges. An ordinary failed sibling or definitively refused paid submission
still charges one outer failure; confirmed productive submission,
operator/shutdown cancellation and pin deferral keep their existing higher
precedence. `ReviewChildFailed` rejects infrastructure and budget reasons,
and ordinary transport death cannot be represented without a positive
deterministic-classification receipt.

Sol review found and verified the refused-submission precedence fix and a
raw-string reason validation fix. Its fresh pass found no further material
issue. Behavioral tests cover infra-only non-charging projection, mixed
ordinary/infra sibling precedence, refused paid submission, malformed
typed reasons and adapter source-backed infrastructure returns. Final
`make test` passed with 3,118 domain, 2,732 adapter and 2,863 app tests,
hooks, governor, parity, four import contracts and both Pyrefly checks.
Log: `/tmp/phase4-infra-outcome-full.log` (ephemeral). Commit `6f02589`.
No live activation, target mutation, service restart or paid harness run
occurred.

## Checkpoint 15 — settled budget-exhausted review result (accepted)

A settled, guarded launched child whose raw return reports an error with
`BUDGET_EXHAUSTED` now produces typed `ReviewChildBudgetExhausted` evidence.
The reader still requires its exact admission, launch, raw return and actual
settled charge. Pure finalization selects `ReviewBudgetExhausted` for a
nonproductive round with such a child, retaining all launched children and
preserving the quality allowance, pin streak, session and displayed round.
Confirmed submission, cancellation, pin deferral, ordinary failed children
and refused paid submissions retain their higher precedence. The distinct
outer disposition is encoded in the canonical review codec and re-derived
from stored closure facts on authoritative reload.

Behavioral tests cover source-backed terminal evidence, non-striking
projection, failed sibling and refused submission precedence, and persisted
closure/outcome reload. Independent Sol review found no material defect and
identified the latter two coverage cases, which were added before the final
gate. `make test` passed: 3,121 domain, 2,733 adapter and 2,863 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks.
Log: `/tmp/phase4-budget-outcome-final-full.log` (ephemeral). No live
activation, target mutation, service restart or paid harness run occurred.

## Checkpoint 16 — source-backed parent abandonment fence (accepted)

`seal_abandoned_planning` now moves an open round to `ReviewParentAbandoned`
only after the exact parent dispatch claim occurrence is absent and has one
matching creation and released/reclaimed terminal receipt. The fence digest
binds the original owner, occurrence, terminal event, claim facts and evolved
plan. The round update and immutable fence receipt commit together; exact
replay is inert. The canonical loader rechecks the source and refuses a
missing, orphan or altered fence. A successor review owner can seal its
predecessor's round through the existing durable owner lineage.

The abandoned planning state refuses later stage registration and child
admission, including a replacement parent claim. Active execution now
refuses bound `SUBMIT_REVIEW` at command enqueue and begin until command and
round registration can commit atomically. Unbound submissions remain fenced
by the existing active review-family rule. This checkpoint does not call the
abandonment writer from daemon recovery and does not lift the temporary
submission guard.

Behavioral tests cover live and merely missing claims, released and reclaimed
source receipts, successor ownership, replacement claims, late paid admission,
submission enqueue, receipt corruption, exact replay and write rollback.
The historical unresolved-submission hold tests now create their commands
before the offline activation fixture. Independent Sol review found the
successor and submission gaps; re-review found no supported write path around
the guard. Final `make test` passed: 3,121 domain, 2,747 adapter and 2,863
app tests; hooks, governor, parity, four import contracts and both Pyrefly
checks. Log: `/tmp/phase4-abandonment-final-full.log` (ephemeral). No live
activation, target mutation, service restart or paid harness run occurred.

## Checkpoint 17 — atomic review submission registration (accepted offline)

`SQLiteReviewExecutionControl.register_submission` now appends a copied or
paid submission to the immutable round plan, enqueues its exact bound
`SUBMIT_REVIEW` command, writes command and round-index receipts, and advances
the aggregate version in one transaction. The command is invisible to the
executor until that transaction commits. Enqueue, begin and retry require the
registration for active execution. Copied submissions require the canonical
same-generation baseline to cover their patch; paid submissions require a
planned stage. A second stage or submission waits for a prior submission's
terminal refusal receipt. Exact replay is inert even after the parent claim
has been released or a paid fallback has been planned.

The canonical loader joins the bounded submission index to its preflight,
owner claim, plan, command row, binding and payload digest. Tests cover
transaction rollback, replay, copy-to-paid fallback, pending-command holds,
unregistered commands, patch mismatch and missing or altered source rows.
Independent Sol review identified that the production verdict caller still
uses ordinary enqueue. The execution-family activation command and round
runtime cutover are not yet implemented, so this checkpoint is **offline
plumbing only**; the guard fails closed if an execution-active row is somehow
present. The production caller must use this registration API before real
activation. `make test` passed: 3,121 domain, 2,760 adapter and 2,863 app
tests; hooks, governor, parity, four import contracts and both Pyrefly checks.
Log: `/tmp/phase4-submission-final-full.log` (ephemeral). No live activation,
target mutation, service restart or paid harness run occurred.

## Checkpoint 18 — parent-abandonment recovery caller (accepted offline)

The composed review daemon now calls `recover_abandoned_planning` after each
boot, wake or periodic recovery pass. The sweep visits at most 64 rows from
the existing ordered `review_execution_rounds_open` index, then filters that
page for open planning, absent exact parent claim occurrence and unclosed
rounds. It advances its cursor across sealed-but-unfinalized rows, so a
growing backlog cannot make one recovery transaction scan the repository.
The existing abandonment writer still requires the exact creation and
released/reclaimed claim receipts before it writes the fence. A malformed or
missing terminal receipt is reported without inventing abandonment; failures
are logged at most once per 15 minutes.

Adapter tests cover live and released claims, missing terminal proof,
pagination and a backlog of already fenced but unfinalized rounds. A composed
daemon test calls production recovery and observes the durable abandonment
fence. It explicitly confirms that closure and outcome remain pending.
`EXPLAIN QUERY PLAN` selected `review_execution_rounds_open` and
`dispatch_claims_occurrence` for the bounded page. Independent Sol review
found the original unindexed planning filter; after the bounded-page fix,
re-review found no blocker for this offline fence-only checkpoint. This
checkpoint does **not** terminalize the outer round or advance the ordered
projection; both are required before real execution activation. Final
`make test` passed: 3,121 domain, 2,764 adapter and 2,864 app tests; hooks,
governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-abandonment-recovery-final-full.log` (ephemeral). No live
activation, target mutation, service restart or paid harness run occurred.

## Checkpoint 19 — transaction-local round facts reader (accepted offline)

`SQLiteReviewExecutionControl.read_round_facts` now reconstructs one round's
planning, child and submission evidence under one SQLite transaction. It
reuses the existing source-backed child reader through an internal
transaction-local entry point. The planning seal or exact parent-abandonment
fence is verified before an unadmitted planned child can become
`ReviewChildNotAdmitted`; an admitted or otherwise pending child remains
pending. Each submission joins its registered command to the effect receipt,
attempt owner, terminal status, remote review identity and recomputed receipt
digest. Confirmed/refused receipt identities are bounded hashes of the
command and receipt digest. Missing or changed source rows fail closed.

The reader returns validated `ReviewRoundFacts` with a deterministic closure
identity but does **not** persist closure or outcome. Pure finalization can
therefore distinguish an empty abandoned round from one still waiting on an
uncertain command. Focused tests cover open planning, abandoned unadmitted
children, pending/confirmed/refused submissions, absent remote identity and
receipt/owner/digest corruption. Sol review found and fixed two unreachable
terminal result branches; a fresh pass found no remaining material issue.
Final `make test` passed: 3,121 domain, 2,774 adapter and 2,864 app tests;
hooks, governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-round-facts-final-full.log` (ephemeral). No live activation,
target mutation, service restart or paid harness run occurred.

## Checkpoint 20 — source-backed abandoned-round outcomes (offline)

`SQLiteReviewExecutionControl.finalize_abandoned_round` now rereads the parent
fence, canonical child evidence and registered submission effect receipts in
one transaction. A terminal nonproductive result writes closure facts, the
purely derived outer outcome, an immutable round-outcome receipt and the
execution state under one aggregate revision. Exact replay is inert. Pending
children or submissions remain open. Productive submissions and preparation
deferrals fail closed until their joint settlement and bounded preparation
schedule exist. The canonical loader reconstructs sources for every finalized
abandoned round retained in the suffix, and refuses changed closure or outcome
receipts. Child evidence reconstruction is now transaction-local and shared
between this writer and canonical reads.

The composed review recovery sweeps up to 64 indexed open rows, fences newly
abandoned planning, then finalizes both new and previously fenced abandoned
rounds. A crash between those transactions leaves a valid fence for the next
pass. Unchanged pending facts and run/command progress are recorded with a
durable observation count capped at eight; a live leased child does not
advance the count. The ceiling is reported without repeated writes. Distinct
blocked reasons have a capped durable journal.
Tests cover empty and planned unadmitted abandonment, refused copy commands
and child admission, pending submission, out-of-order closure, exact replay,
source corruption, transaction rollback, recovery pagination, composed daemon
recovery, bounded waits and changed command progress. Independent Sol review
identified the recovery caller and terminal classification gaps, then the
observation fingerprint and block-journal gaps; all four were corrected.

The execution prefix is deliberately not compacted by this writer: keeping
abandoned outcomes in the bounded suffix allows every canonical read to
reconstruct their sources. This costs increasing read work and caps the
uncompacted episode at the existing 4,096-round limit. Prefix compaction and
its source-backed receipt need a separate scale/performance acceptance before
activation. Final `make test` passed: 3,121 domain, 2,788 adapter and 2,864
app tests, plus hooks, governor, parity, four import contracts and both
Pyrefly checks. Log: `/tmp/phase4-abandoned-outcomes-final-full.log`
(ephemeral). No live activation, target mutation, service restart or paid
harness run occurred.

## Checkpoint 21 — settled stochastic transport return (offline)

A settled review child return whose harness exit reason is
`TRANSPORT_DEATH` now becomes `ReviewChildInfrastructureFailure`. Its outer
round can therefore close as a non-striking infrastructure death after the
existing launch, raw-return and charge proofs have all settled. Previously the
child remained pending indefinitely even with complete source evidence. The
normal return does not claim deterministic failure; that strike still requires
separate positive harness provenance. Domain tests cover infrastructure-only
projection and reject an unproved transport strike. Adapter tests construct
the actual guarded launch, returned envelope and settled charge before reading
the child evidence. No live activation or paid harness run occurred. Final
`make test` passed: 3,122 domain, 2,788 adapter and 2,864 app tests, plus
hooks, governor, parity, four import contracts and both Pyrefly checks. Log:
`/tmp/phase4-transport-return-final-full.log` (ephemeral). Independent Sol
review found no blocker.

## Checkpoint 22 — one supervised Codex launch per attached review child (offline)

An attached review child now returns after its first Codex stream drop or
provider rate limit, even when the runner's ordinary retry budgets are
positive. The accounting run has one immutable launch receipt; a retry in the
same child would start another supervised process that cannot register against
that receipt. The returned `TRANSPORT_DEATH` result can be classified by the
non-striking child evidence path from checkpoint 21 after accounting
reconciliation. Unattached Codex legs keep their existing transport retry
policy. Fake-CLI tests assert one process invocation for each failure class;
composed tests also verify one guarded launch receipt, durable raw return and
retained reservation when trusted usage is absent. Independent Sol review found
no blocker. Full `make test` passed: 3,122 domain, 2,790 adapter and 2,866
app tests, plus hooks, governor, parity, four import contracts and both
Pyrefly checks. Log: `/tmp/phase4-codex-child-retry-full.log` (ephemeral).
No live activation or paid run occurred.

## Checkpoint 23 — source-backed completed planning outcomes (offline)

Sealed completed-planning rounds now have a nonproductive finalization entry
point. It reconstructs the exact parent seal, child launch and accounting,
and submission effect receipts in one SQLite transaction, then writes the
closure facts, immutable outcome receipt and ordered execution projection
together. Canonical suffix reads reconstruct completed-round sources before
accepting stored outcomes. The composed bounded round sweep now revisits
sealed completed planning and finalizes it after terminal child evidence;
pending evidence remains open with a bounded observation journal. Productive
submissions and preparation failures still require their separate joint
settlement paths.
Behavioral tests cover refusal, settled paid failure, pending child, confirmed
copy refusal by this writer, replay and source corruption. Earlier synthetic
history fixtures now test the pure projection contract; real writer tests
exercise canonical source-backed reload. No live activation, paid run,
migration or push occurred.

A sealed parent that disappeared before admitting a planned child lacked
terminal non-admission proof at this checkpoint; checkpoint 24 adds it.
Ordered and compacted synthetic suffix tests now
exercise the pure projection contract; composed canonical coverage belongs
with the productive and checkpoint writers.
Independent Sol review identified the missing composed caller and synthetic
fixture incompatibility; both were corrected. Its final pass found the
post-seal liveness gap addressed by checkpoint 24. Full `make test` passed: 3,122 domain, 2,795
adapter and 2,866 app tests, plus hooks, governor, parity, four import
contracts and both Pyrefly checks. Log:
`/tmp/phase4-completed-rounds-full.log` (ephemeral).

## Checkpoint 24 — post-seal parent abandonment proof (offline)

The abandoned-parent variant now has an explicit sealed subtype that carries
the prior completed planning seal. A terminated exact parent claim can fence
late admission after that seal; the fence receipt binds both the claim
lifecycle and the completed seal. Canonical reads verify each source before
constructing unadmitted child evidence. The bounded round sweep converts a
sealed round only after its parent claim is gone, and then finalizes a round
whose children were never admitted without a paid strike. Missing terminal
claim or seal receipts refuse the transition. Confirmed productive submissions
remain open until the joint rejection/execution settlement path exists.
Independent Sol review found no introduced blocker; focused domain and adapter
tests pass. Full `make test` passed: 3,123 domain, 2,797 adapter and 2,866
app tests, plus hooks, governor, parity, four import contracts and both
Pyrefly checks. Log: `/tmp/phase4-post-seal-abandonment-full.log` (ephemeral).
No live activation or paid run occurred.

## Required subsequent work

An offline checkpoint-compaction prototype was reviewed and withdrawn after
checkpoint 24. Checking only the newest proof failed to detect corruption in
older compacted source receipts; checking every proof/source on each canonical
load made hot reads grow with lifetime history (a 65-round focused case took
about 43 seconds including construction). The proposed chain also lacked an
authoritative anchor for imported sequence-zero state, and a recovery scan
revisited historical PRs every tick. No part of that prototype was committed.
Compaction needs an explicit trusted snapshot boundary backed by an immutable
activation/import source and a separate source-audit contract, so ordinary
reads stay bounded. Its performance remains an activation gate.

- Add source-specific preparation outcomes for revision diff and containment
  failures. The verified Codex runtime preflight path is checkpoint 25;
  preserve actual accounting and bind each remaining fault to a bounded hold.
- Pass an execution round through the production review workflow so copied
  and paid verdicts use atomic registration; terminalize fenced abandoned
  rounds from complete evidence before execution activation.
- Compact the ordered execution prefix with a source-backed checkpoint receipt,
  recover real multi-child execution, and capture free copied verdicts through
  the joint settlement path added in checkpoint 26.
- Separate verdict-release and execution-ceiling recovery under one aggregate
  intent, preserving rejection allowance and other holds.
- Complete offline import, shadow, rehearsal and activation; prove inherited
  failure/session provenance, fence old writers and retire selected Store APIs.
- Complete composed acceptance, dashboard authority and independent review
  before execution-family cutover. Other fix/steward continuation families follow.

Phase 5 offline measurement and phase 6 remain pending. Live phase-5 windows
still require explicit user authorization.

## Checkpoint 25 — bounded runtime preparation outcomes (offline)

A verified Codex preflight stop can now close a sealed completed or departed
review round without a paid failure strike. The outcome, exact preparation
obligation, execution projection, and a visible runtime preparation gate commit
in one SQLite transaction. A scheduled local preflight uses the existing review
hold reconciler and bounded evidence schedule to retry; success releases the
gate without creating a verdict. Composition requires a preflight reader.
Claude's configured reader treats a switch from Codex as a new runtime choice;
Codex without `TARGET_DIR` remains unready. Both the local probe and changed
head release compare the observed workflow binding and evidence schedule under
the write transaction, so a stale read cannot erase a newer hold.

Behavioral tests cover both parent branches, no-strike settlement, exact
replay, bounded failure and success, atomic rollback, missing reader, and two
interleaved newer failures. Independent Sol review found and verified the
reader and concurrency fixes. Full `make test` passed: 3,123 domain, 2,806
adapter and 2,866 app tests, plus hooks, governor, parity, four import
contracts and both Pyrefly checks. Log:
`/tmp/phase4-runtime-preparation-full.log` (ephemeral). Other preparation
kinds (`REVISION_DIFF`, `CONTAINMENT`) still need their own source-specific
reread paths. No live activation, paid run, migration or push occurred.

## Checkpoint 26 — joint productive review settlement (offline)

Confirmed paid and copied submissions now finalize their source-backed outer
round and execution projection in the rejection baseline or ceiling-gate
transaction. The later settled marker cannot leave a committed baseline or
gate with an open round.
Open planning waits for a parent seal or abandonment without marking the
submission settled; a bounded recovery sweep repairs older recorded-baseline
states with unfinalized rounds. Stale-generation confirmed verdicts finalize as
audit-only outcomes before their settled marker. A ceiling refused by a fresh
read still closes its confirmed productive round; the immutable decision
retains and rechecks that read. A superseded same-generation
escalated verdict closes under a fresh, immutable head/terminal observation
without creating a new baseline, and its confirmed productive work resets the
execution allowance. Canonical reload requires one exact authority receipt per
productive outcome and checks the supersession observation's age against the
settlement timestamp. A crash between outcome and marker replays inertly.

Behavioral adapter tests cover paid and copied outcomes, ceiling opening and
refusal, rollback, historical repair, stale generation, open planning,
supersession and expired observations. Independent Sol review found and
verified fixes for the split state, stale outcome, supersession, ceiling
decision replay and timestamp boundaries. No live activation,
paid run, migration or push occurred.
Full `make test` passed: 3,123 domain, 2,826 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten accepted strict xfails remained expected. Log:
`/tmp/review-productive-final-gate.log` (ephemeral).

## Checkpoint 27 — generation boundary model and codec (offline)

An active execution component cannot currently cross a rejection rearm or
ceiling release: all three advancing transitions retain its old binding, and
the public SQLite-control path raises during snapshot construction. A
temporary regression reproduced it. The [generation-boundary finding](2026-09-28-phase-4-generation-boundary.md)
explains why rebinding alone would lose finalized failures on replay or apply
late old-generation outcomes. No adapter path was changed in this checkpoint.

`domain/review_execution.py` now has a validated immutable generation
boundary, a pure carry that preserves allowance and clears the reviewer
session, and replay that applies only outcomes captured before each advance.
The closed workflow codec round-trips the boundary's exact revisions and
mutation and refuses missing fields.
It keeps a boundary distinct from the contiguous finalized checkpoint,
supports a verified checkpoint inside a boundary, and rejects foreign
allocation bindings and revision order. Tests cover old late outcomes,
multiple advances, session clearing, malformed boundary facts, and
full-versus-compacted replay. Independent Sol review found and verified fixes
for compacted replay, allocation binding validation, revision provenance and
an initially quadratic interval scan. Its final pass found no medium/high
defect in the pure model. Adapter minting, completeness of the captured source
set, exact transition receipts, canonical loading and generation-aware
compaction remain pending. The 4,096-round suffix limit remains an activation
gate; no live authority was activated. Full `make test` passed: 3,128 domain,
2,827 adapter and 2,866 app tests, plus hooks, governor, gate parity, four
import contracts and both Pyrefly checks. Ten accepted strict xfails remained
expected. Log: `/tmp/phase4-generation-boundary-final-gate.log` (ephemeral).

## Checkpoint 28 — active execution generation wiring (offline)

The three rejection generation advances now carry active execution through a
typed boundary. The SQLite writer captures the complete finalized suffix at
acceptance, commits the boundary with the accepted workflow transition, and
re-derives those facts when validating the proposal. Canonical reload verifies
each boundary receipt against that accepted transition and replays the round
suffix in allocation order. An older round that closes after rearm retains its
source and charge receipts but cannot change the carried allowance. The old
active-component codec tag has an explicit exact-field read rule; new writes
use the boundary-bearing tag.

Public-control tests cover request rearm, stopped continuation, ceiling
release, old open rounds, late paid failures, state-neutral outcomes captured
before rearm, idempotent replay, missing boundary/transition receipts, and
transaction rollback. An independent Sol review found no separate high
severity correctness defect. It identified quadratic stored and hot-read
history from accepted snapshots embedding all earlier boundaries; the
4,096-boundary limit also eventually stops repeated recovery with zero new
rounds. Source-backed boundary compaction and bounded canonical reads are
required before execution-family activation. No live authority was activated.
Full `make test` passed: 3,129 domain, 2,837 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-generation-adapter-gate.log` (ephemeral).

## Checkpoint 29 — pure compaction frontier (offline)

The [source-backed compaction contract](2026-09-28-phase-4-compaction-contract.md)
now defines an activation or renewed-episode origin, a checkpoint frontier
that distinguishes same-sequence generation advances, bounded hot reads, and
a separate cyclic audit of archived sources. Independent Sol review tightened
the contract around lifetime SQL scans, large accepted snapshots, renewed
origins, audit freshness, and a late old-round cutoff.

The domain can now plan a compaction at a contiguous finalized prefix while
retaining a later boundary over an open round. It exposes all retired round
sources separately from the subset applied to the state, verifies that replay
from the proposed checkpoint matches the active projection, and carries the
last retired boundary cursor through later round-only checkpoints. Exact
allocated-sequence and duplicate validation bounds untrusted input before
range construction. The closed codec has an explicit old checkpoint read rule
and a new cursor-bearing write tag. Tests cover late outcomes, partial
compaction, repeated zero-round advances, cursor preservation, malformed
sources, and delivery-order independence. Sol's final pure-scope pass found no
remaining medium or high issue. This is a pure planner and codec checkpoint:
the adapter does not yet mint origin/compaction proofs, retire active suffixes,
or run the archived-source audit. No live authority was activated.
Full `make test` passed: 3,136 domain, 2,838 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-compaction-model-gate.log` (ephemeral).

## Checkpoint 30 — legacy execution file inventory (offline)

The read-only execution source reader records `pr-N.session`,
`pr-N.round`, and both `issue-N.infra-deaths` and
`issue--N.infra-deaths` across supplied state roots. It retains
raw runtime-tagged session bytes, absent versus unreadable files, malformed
names, and duplicate targets in different physical roots. It reuses the
bounded, no-follow file reader from resolve activation and makes no import or
eligibility decision. Typed records keep the signed legacy counter key until
issue-to-PR attribution can be proven from fresh linked-issue observations.

Tests cover normal and malformed names, UTF-8 and size failures, nonregular
files, symlinks, root aliases, cross-root duplicates and read-only behavior.
Independent Sol review found no medium or high defect in this file slice and
confirmed that SQL abandoned-run deaths, submission receipts, and verdict
hold provenance remain separate required activation evidence. Execution
authority is still inactive. Full `make test` passed: 3,149 domain, 2,843
adapter and 2,866 app tests, plus hooks, governor, gate parity, four import
contracts and both Pyrefly checks. Ten accepted strict xfails remained
expected. Log: `/tmp/phase4-execution-source-inventory-gate.log` (ephemeral).

## Checkpoint 31 — paged review owner run evidence (offline)

The execution activation reader now scans one review owner's `runs` history
under an explicit SQL snapshot in bounded pages. It retains terminal and
unfinished runs, dispatch IDs, outcome and receipt fields, workflow bindings,
episode keys, and execution round/child attachments without interpreting
their death or reset effect. Each valid row has a digest independent of its
SQLite page cursor; oversized, non-text or undecodable fields remain explicit
unreadable records. The first page includes SQLite's negative and zero
rowids. The reader makes no eligibility or import decision.

Independent Sol review reproduced a silent negative/zero-rowid omission and
an invalid-UTF-8 fetch failure; both were fixed and its final pass found no
remaining medium or high issue in this reader. Activation still needs
authorized predecessor-owner discovery, complete submission and hold
receipts, exact issue-counter attribution, and a source-backed origin.
Execution authority remains inactive. Full `make test` passed: 3,156 domain,
2,848 adapter and 2,866 app tests, plus hooks, governor, gate parity, four
import contracts and both Pyrefly checks. Ten accepted strict xfails remained
expected. Log: `/tmp/phase4-execution-run-inventory-gate.log` (ephemeral).

## Checkpoint 32 — abandoned review run attribution (offline)

A pure classifier now separates non-abandoned runs from attributable review
deaths and explicit unknown-attribution blockers. It parses the retained
dispatch identity without accepting duplicate or nonfinite JSON fields,
requires the run and dispatch kinds to agree, and retains the exact PR and
signed counter key. A historical positive issue key must match the producer's
`issue` field; a negative key must name that PR. The attributed variant
revalidates its own source, so callers cannot construct a trusted fact for a
different PR or counter. Classification does not import a debit, infer a
productive reset, or authorize a repository lineage; those decisions still
require complete submission and owner evidence.
Independent Sol review reproduced and verified fixes for unhashable dispatch
`kind`, a constructible source-inconsistent attributed variant, and a
historical positive key that disagreed with its recorded issue. Its final pass
found no remaining medium or high classifier defect. Full `make test` passed:
3,176 domain, 2,848 adapter and 2,866 app tests, plus hooks, governor, gate
parity, four import contracts and both Pyrefly checks. Ten accepted strict
xfails remained expected. Log: `/tmp/phase4-review-death-classifier-gate.log`
(ephemeral).

## Checkpoint 33 — paged review submission command and effect sources (offline)

The execution activation reader now scans canonical repository
`submit_review` lifecycle commands and their optional effect receipts in
bounded pages under an explicit SQL transaction. Typed source records retain
all current columns and a digest independent of the SQLite cursor. The first
page includes negative and zero rowids; malformed, invalid UTF-8 and
oversized fields become explicit unreadable records. Missing receipts stay
absent. This inventory does not equate command completion with a productive
review. Round submission indices and remote verdict hold provenance remain
separate required evidence before a reset can be imported.

Focused behavior tests and Pyrefly passed. Independent Sol review found no
medium or high issue in this bounded source reader. Execution authority
remains inactive. Full `make test` passed: 3,183 domain, 2,852 adapter and
2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-submission-source-gate.log` (ephemeral).

## Checkpoint 34 — paged review submission registration and index sources (offline)

The execution activation reader now inventories the three durable
`dedupe_keys` namespaces for review command registration, round submission
index and recorded-review settlement under an explicit SQL snapshot. It
retains all six current row columns, including nullable payload and reuse
fields, in bounded pages with a rowid-independent digest. Negative and zero
rowids are included; malformed, invalid UTF-8 and oversized fields become
explicit unreadable records. A recorded-review row by itself is not a
productive reset; the importer must join the index and registration to its
round, command/effect and verdict sources. Execution authority remains
inactive. Independent Sol review found no medium or high issue in this
source reader. Full `make test` passed: 3,189 domain, 2,855 adapter and
2,866 app tests, plus hooks, governor, gate parity, four import contracts
and both Pyrefly checks. Ten accepted strict xfails remained expected. Log:
`/tmp/phase4-submission-index-source-gate.log` (ephemeral).

## Checkpoint 35 — review owner lineage source (offline)

The execution activation reader now resolves a bounded review-owner closure
under one explicit SQL snapshot. It includes the current owner, the implicit
`repository:review` predecessor adopted at daemon boot, exact configured
predecessors and their transitive `retired_owners` links. It does not call
adoption or rewrite any row. Raw retirement links retain source digests
independent of SQLite rowids. A retired current owner, foreign role or
repository, cycle, unreadable row, malformed SQL type, oversize field,
conflicting configured retirement or over-bound closure returns an explicit
blocker. The caller must use this same snapshot for every paged owner-run
scan and revalidate under activation barriers before commit. Execution
authority remains inactive. Independent Sol review reproduced two missing
owner cases: the implicit legacy owner and a BLOB successor alias. Both were
fixed, as was a configured owner retired elsewhere; its second pass found no
remaining medium or high issue. Full `make test` passed: 3,191 domain, 2,868
adapter and 2,866 app tests, plus hooks, governor, gate parity, four import
contracts and both Pyrefly checks. Ten accepted strict xfails remained
expected. Log: `/tmp/phase4-owner-lineage-gate.log` (ephemeral).

## Checkpoint 36 — source-backed plain recorded review baseline (offline)

A read-only verifier now joins one inventoried successful `SUBMIT_REVIEW`
command and exact effect to its `review-recorded-submission` marker and
accepted `workflow-transition` receipt under an explicit SQL snapshot. It
checks the command payload and effect digests, paid attempt and attempt owner,
authorized owner lineage, exact accepted revision pair, typed review snapshot,
reviewed baseline and command/review evidence IDs. A verified variant exposes
only the baseline, revision pair and audit, with source digests for later
activation revalidation. The verifier rechecks the inventoried command rowid
and the producer's unique GitHub review identity rule. It does not certify the
full historical workflow snapshot, execution productivity, a reset, or
outer-round ordering. Missing or contradictory facts return an explicit
unverified variant; an unrecorded successful command does not prove the
absence of a reset. Escalated verdicts
remain blocked until their hold provenance is separately verified. Independent
Sol review found the initial reset/productivity overclaim, an exposed
unverifiable snapshot, a forged source cursor and a duplicate review receipt
gap; the proof was narrowed and regression cases were added. Full `make test`
passed: 3,191 domain, 2,878 adapter and 2,866 app tests, plus hooks, governor,
gate parity, four import contracts and both Pyrefly checks. Ten strict xfails
remained expected. Log: `/tmp/phase4-recorded-review-gate.log` (ephemeral).

## Checkpoint 37 — complete bounded owner-run inventory (offline)

The execution activation reader now scans `runs` once by rowid under an
explicit SQL snapshot and retains every source owned by the rechecked review
owner lineage, including the implicit legacy owner. It visits at most one
million total rows and retains at most 8,192 matching rows or 16 MiB of
source text; exceeding a bound returns a blocker. Matching BLOB owner IDs,
unreadable rows and changed lineage also block. A complete result contains
only readable, unique owner sources and a digest independent of SQLite page
cursors. A separate revalidation call rescans SQL and refuses a caller-held
partial or changed result. Activation must use that call under deployment
barriers before committing any import; this offline reader activates nothing.
Independent Sol review found the initial per-owner scan could repeatedly
traverse unrelated rows and that a caller could construct a partial complete
value. The one-pass bound and revalidation address those findings; its second
pass found no remaining medium or high issue. Full `make test` passed: 3,192
domain, 2,885 adapter and 2,866 app tests, plus hooks, governor, gate parity,
four import contracts and both Pyrefly checks. Ten strict xfails remained
expected. Log: `/tmp/phase4-complete-runs-gate.log` (ephemeral).

## Checkpoint 38 — registered review command-to-round link (offline)

A read-only verifier now joins one inventoried review submission command to
its exact registration, round submission index, preflight receipt, evolved
round plan and bounded stage receipts under one explicit SQL snapshot. It
rechecks owner lineage and the inventoried command/effect rows, validates the
registration's command ID, digest, idempotency key, configuration version and
owner, and checks the plan, binding, head and PR target. Changed, oversized,
missing or contradictory sources return an explicit unverified result. The
verified value exposes the round identity and source digests; registration
kind, productive settlement and execution reset remain unproved. A copied
submission can acquire later paid stages only after its definitive refusal.
This reader does not activate execution authority or mutate SQL. Independent
Sol review found and drove fixes for unbounded re-reads and an omitted exact
command comparison; its final pass found no remaining medium or high issue.
Full `make test` passed: 3,192 domain, 2,902 adapter and 2,866 app tests,
plus hooks, governor, gate parity, four import contracts and both Pyrefly
checks. Ten strict xfails remained expected. Log:
`/tmp/phase4-round-link-gate.log` (ephemeral).
