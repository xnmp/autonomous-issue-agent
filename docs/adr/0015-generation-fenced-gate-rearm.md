# ADR 0015 — Generation-fenced gate re-arm authority

Status: Proposed (2026-09-14). Phases 1–2 accepted at `1703195`; resolve
foundations accepted at `05aa54a`, with the vertical slice still under implementation. No phase 3 slice
is accepted and no live workflow family has been activated.

Governs: src/issue_agent/domain/workflow.py src/issue_agent/domain/workflow_policy.py src/issue_agent/adapters/workflow_control.py src/issue_agent/adapters/control_store.py

Proposed authority change: partially supersedes ADR 0009 only for the families
accepted below. Refines the proposed ADR 0013; neither document's status is
changed by this proposal. ADRs 0001–0003, 0008, 0010 and 0012 remain applicable.

## Context and scope

The [architecture review](../open/2026-09-14-architecture-review.md) and
[critique](../open/2026-09-14-architecture-overhaul-critique.md) identify a
re-arm distributed across counter files, review dismissal, guidance, labels
and telemetry. An individually durable remote effect does not preserve the
operator's entire intent. A restart can leave renewed allowance without the
required remediation, or remove the final review without restoring review
eligibility. Labels also cannot distinguish successive exhausted episodes.

Implement and accept the issue resolve-ceiling flow first. It exercises
allowance renewal, stale completion, held-state projection and remote receipt
recovery without requiring review dismissal. Then implement and accept the PR
review-rejection-ceiling flow, including its review baseline. The second flow
is distinct from the separate unproductive-review-round counter. Do not migrate
other counter/continuation families merely because they share a cleanup list.

The metric is fewer mechanical interventions and fewer repeated failed paid
legs at comparable cost per merge. Spend caps, first-pass approval quality and
trusted gate evidence remain guardrails. The next evidence is composed fault
replay followed by the separately authorized live window in phase 5.

## Decision

GitHub owns remote lifecycle, reviews, replies, linkage and revisions. SQLite
owns the selected workflow's generation, exhausted-episode cause, durable
operator intent, required remediation and completion decision. Worktrees own
content. The selected no-PR resolve flow's reusable session and authored split
checkpoint move with its episode as specified below. Other runtime sessions,
consumed replies and branch continuity retain ADR 0009's ownership until a later
accepted migration. JSONL remains telemetry.

The stable workflow key is `(canonical_repository, typed_target)`, with positive
issue and PR identities represented separately. Resolve is issue-owned;
review rejection is PR-owned regardless of changing issue linkage. Deployment
owner identity authorizes every read/write/effect under ADR 0008; it is not a
second workflow key that grants a fresh allowance on repoint. Existing claim,
run and spend identities stay intact, with an explicit conversion at their
existing integer boundary.

Family episodes are children of that workflow, not separate state machines.
Each family activation has an immutable epoch. Each workflow has a monotonic
generation and compare-and-swap version. Ordinary progress changes version;
an accepted operator re-arm advances generation exactly once. A duplicate
operator request returns its existing intent. A different request against an
old generation conflicts. A re-arm renews only its selected family's allowance;
other family allowances and unresolved gates survive the workflow generation
change. Creating a new request identifier alone cannot
purchase another allowance while the first request is pending.

Gates are typed values carrying a cause and evidence, remediation already
attempted (or an explicit cause-specific declaration that none is authorized),
and permitted typed recovery operations. Multiple unresolved gates can coexist.
Opening is deduplicated by workflow, generation and cause identity, never by
label presence. A generation advance carries every unresolved blocker forward;
it does not make old rows invisible. An explicit human hold needs its own
authorized release and is not discharged by re-arming an unrelated ceiling.

### Resolve slice

Transfer `resolve-retries`, its last-failure explanation and new re-arm guidance
to a typed resolve episode. Failed-result accounting is idempotent by the
existing paid attempt/run identity and captured generation. Preserve the
current policy: only a resolve-family result that ran and requeues without a
PR consumes this ceiling; admission refusal, infra death and budget exhaustion
retain their existing classification. An old-generation result still settles
spend and keeps its receipt but cannot charge or relabel the new episode.

Resolve continuation belongs to that same episode: `FreshResolve` has no reusable
authored split checkpoint; `AuthoredResolve` retains the exact successful author
attempt. Either may contain the latest verified runtime-tagged session. The
session carries its source attempt and selected model; an unreported actual model
is not invented. A new allowance grant starts with a fresh continuation even if
old phase/session files remain on disk as evidence. `ContinueGrantedRearm` keeps
the same episode and continuation. This prevents new guidance from being bypassed
by an unrelated old `authored` marker. Worktree content is retained under existing
identity checks, never deleted as an unrecorded re-arm side effect.

Paid admission allocates a workflow revision and records its ordered admission
receipt in the existing SQLite transaction. Neither UUID order nor timestamps
establish invocation order. Result recording uses that receipt and the existing
run/provider evidence: each genuine failed result still consumes its strike once,
but an older delivery cannot overwrite a newer continuation or resurrect a cleared
session. Router, infrastructure and admission/budget failures do not replace a
resolve checkpoint. A newer legitimate sessionless result clears the prior handle,
and a blocked resume discards even its returned handle, preserving current
fresh-recovery behavior. A successful author establishes the authored checkpoint;
other stages may update its session but cannot fabricate that checkpoint.
Author-checkpoint provenance and session ordering advance independently: a late
successful author still establishes its checkpoint without restoring an older
handle, and a newer author proof supersedes an older one regardless of delivery
order. Successful progression requires normal executed settlement evidence even
when no session handle was returned. A released, never-launched reservation can
be audited as an infrastructure/admission failure but cannot establish a checkpoint.

The adjacent split adjudication allowance is checked in the same paid-admission
transaction by counting existing admitted `ADJUDICATE` runs for the workflow,
epoch and episode. An admitted run is the charge, including a crashed run.
Continuation across generations does not renew it; a genuinely new episode does.
This does not introduce a second counter or a pending-stage execution engine.
The v20 indexed projection keeps the episode key on the existing admitted run,
excluding generation. Counts saturate above the largest supported ceiling.
Only a transactionally proven never-launched release is excluded; unknown,
crashed or generally released executions continue consuming allowance.

Accepting a valid request records guidance, advances generation, creates the
new bounded episode and retains a rearming gate, all in one transaction.
Continuation lookup reads the guidance from this authority before dispatch;
there is no required second file write. The required remote operation restores
the appropriate issue trigger and removes only the ceiling-owned hold that
the request authorized. The workflow stays held until the existing executor
has verified the required projection and fresh facts allow resolve. If another
gate remains, completion of the selected re-arm can report `completed, held by
<other cause>` only after verifying the hold-preserving projection; it cannot
report restored eligibility or remove a shared attention label. A PR discovered in
the meantime or a terminal issue prevents blindly restoring `ai-ready`.

### Review slice (only after resolve acceptance)

The rejection cause remains calculated from fresh, complete GitHub reviews
using the existing counted-review policy and trusted review-bot identity. Save
the exact counted review IDs and relevant head when the gate opens. The
operator request authorizes dismissal only of those counted bot reviews,
bounded by the configured ceiling; later reviews and human reviews are not
implicitly added to that authorization. Re-observe before every dismissal.

Transfer the PR's reviewed-head/patch baseline and operator guidance as one
typed continuation value; the generation records when a fresh review is owed.
The new generation invalidates the old baseline. Old review completion cannot
repopulate it, and unchanged-patch reuse cannot discharge the fresh review
obligation. Session identity remains runtime-tagged under the existing Store.
Completion requires verified dismissals, durable guidance, the required
review trigger, and fresh evaluation showing that the selected rejection cause
is false. Other gates still block. No successful response may hide a failed
dismissal or guidance write behind an `ok — but` result.

### Transaction and effect boundary

Use the existing periodic reconciliation loop, lifecycle command queue,
DurableEffectRunner, receipts and event outbox. A re-arm record is domain intent
with a finite list of required command identities, not a separate task queue.
Pure transition policy proposes changes; a short `BEGIN IMMEDIATE` validates
epoch, owner, generation and version and atomically persists workflow/gates,
intent and eligible lifecycle commands plus telemetry outbox entries.

There is no network I/O or nested top-level transaction inside that boundary.
Required commands carry a workflow generation fence in addition to applicable
remote revision guards. Re-arm acceptance and remote execution are ordered
through the existing deployment effect barriers for affected roles, acquired
in a fixed order. Acceptance waits for an already executing effect to finish
or become durable uncertainty, then re-observes before committing. A mere
generation check before releasing a SQL transaction is insufficient to fence
the subsequent remote call. Do not wait for barriers while holding SQL locks.
Acceptance takes exclusive barriers in `fix`, `review`, `steward` order for
the repository, uses read-only gateways to refresh facts, validates active
owners and commits local intent. It releases every exclusive barrier before
invoking any effect runner. Calling a runner's shared authority lock inside
this exclusive section would deadlock; acceptance never performs remote writes.

Resolve obligations execute as the existing fix daemon's `_owner_identity`
with its `GitHubGateway` and FIX_BOT credential contract. Review dismissal and
PR projection obligations execute as the existing review daemon's owner with
its gateway, using `review_identity` (App, configured review token, then the
existing ambient mode). Validate that this identity can perform the recorded
bot-review operation; lack of permission remains a visible refusal, not an
invitation to switch credentials. The dashboard records its authenticated actor
on the intent but does not mint `repository:dashboard[:review:*]` command
owners or call `GitHubOperatorEffects` for these migrated flows.

Extend the existing `GitHubEffectExecutor` owned command set and typed command
decoder with `DISMISS_REVIEW`, including a read/verify operation on the role's
gateway. It uses the executor's existing `DurableEffectRunner` with
`DeploymentAuthority`, never `UnfencedEffectAuthority`. Resolve labels remain
fix-owned; review re-arm changes only its PR projection, preserving independent
issue holds. The daemon's existing `Executor.replay_pending` before observation
and `EffectRecovery` startup/wake path drive bounded workflow reconciliation
around the same command drain. Both paths advance pending obligations and
receipt-based completion even when the dashboard has exited, the world probe
skips a full observation, or paid admission is capped. Disabled/unconfigured
required roles reject new intents; a configured but offline role leaves a
visible pending executor-unavailable reason. Repoint transfers command ownership
through the existing audited adoption/retirement contract, not a new alias.

Receipts are still owned by the existing effect machinery. The reconciliation
transaction joins those receipts with fresh validated observations before it
marks an intent complete and closes the selected gate. A process crash after
remote success but before its receipt remains unknown until reconciliation
proves success. Required commands have stable IDs derived from request and
operation identity. Do not create a fresh occurrence on each drain.

Paid admission checks the captured family epoch/generation and all applicable
gates in the existing admission transaction. It records the generation beside
the existing run/reservation; re-arm does not create a second admission path.
An admitted old-generation run must be durably recovered or drained before
another generation dispatches. Its settlement is always allowed; its workflow
effects require current generation and fresh lifecycle authorization.

Remote read-before-write revision checks are not server-side compare-and-swap.
Reconciliation retains this residual: a concurrent external change between
verification and mutation can still race. The re-arm's own verified label
changes can advance its revision guard only after re-evaluating authorization;
an unrelated human change cannot be adopted as the new expected revision.

### Uncertainty and adjacent bounded loops

For commands in the migrated slice, persist the first unresolved time, next
reconciliation time and unresolved-read count. Use bounded exponential
backoff and a configured age/read ceiling separate from verified refusals.
At that ceiling open `RemoteOutcomeUncertain(command_id, evidence)` and switch
that command to reconciliation-only behavior. Preserve its original state,
identity, receipts and unknown outcome. Timeout is not evidence of absence,
does not refund money and cannot authorize another remote write. A later
verified result may resolve the gate; explicit operator reconciliation is
read-only. An observation proving absence after expiry still requires a
current authorization before execution can resume. Use
`AuthorizeAbsentRetry(command_id, gate_id, evidence_id, authorization_id,
expected_revision)`: an authenticated operator authorizes exactly one execution
of the existing command under its workflow generation and episode. Association
is typed: a standalone workflow command needs no re-arm request; a required
re-arm obligation additionally requires its original request still pending.
This does not renew allowance, fabricate a re-arm intent, or erase unresolved
history. `evidence_id` resolves to a harness-owned,
fresh, complete observation proving that specific current postcondition is absent;
the API cannot supply its own proof. An unreadable target, missing review from
a partial list or bare 404 is not that proof.

A verified absence replaces that command's uncertainty cause with
`RetryAuthorizationRequired`, retaining the same gate and reconciliation-only
execution restriction. It neither falsely reports the current outcome as unknown
nor clears the last blocker on an ordinary command. Successful read-only checks
also advance the bounded polling schedule while retaining the original expiry
history; waiting for authorization must not create an immediately-due read loop.

Persist the authorization and its unique ID atomically. The daemon checks fresh
absence and separately proves the original decision's current applicability under effect
authority, then consumes the authorization atomically with `begin_command` for
one attempt of the existing command ID. Replayed authorization IDs cannot buy
another execution. If a crash or unknown result follows consumption, return to
reconciliation-only, retaining the original expiry; a later proven absence
requires a fresh explicit one-attempt authorization. A crash before consumption
leaves the persisted authorization available after restart. If the effect is
already present, reconcile success without consuming a write. Changed guards
retire/supersede the obligation as described below instead of replacing its
expected revision. Ordinary `ReconcileGate` remains read-only.

### Superseded and cancelled intent

An intent has pending, completed, superseded or cancelled variants. A fresh
terminal target observation, materially changed decision guard (including a
head change invalidating a recorded dismissal), or permanent stale-revision
refusal may supersede it. A request from the authenticated operator can cancel
it. The owning daemon makes the terminal transition under the same exclusive
barriers and CAS checks, without effect execution in that critical section.
Verified prior effects remain facts; unsatisfied obligations receive an audited
retirement disposition. Original commands, receipts, expiry and authorization
records remain intact. Retired unexecuted commands cannot execute; ambiguous
commands remain reconciliation-only and their uncertainty gate survives.
Read-only reconciliation can establish a retired command's current postcondition
is absent even when the original decision no longer applies. That retires its
current uncertainty hold and can permit safe continuation; it never asserts the
write historically did not happen, refunds spend, or revives execution authority.
Retry applicability is separate evidence and is always false for retired intent.

This transition frees the single pending-intent slot but neither refunds nor
renews allowance, rewinds generation, restores eligibility, nor labels the
selected cause disproven. A live target retains `RearmNeedsDecision` with the
stopped request and exact obsolete/rejected obligations; a terminal target is
displayed as terminal with retained hold history. Reopening cannot resurrect
the stopped request or its retired commands. Policy re-evaluates current facts
and preserves unresolved holds. An operator may submit `ContinueGrantedRearm`
referencing the stopped request and current revision: it creates a new pending
intent and advances the workflow generation for fencing, but retains the same
episode and allowance/counter values. It re-plans only the remaining authorized
scope against fresh facts; changed dismissal IDs require explicit new review
authorization. Already verified effects are not repeated. Unresolved prior
effects that could conflict with the replacement keep that replacement held
until reconciliation resolves them. A new allowance can be granted only by a
new valid ceiling re-arm after that allowance has actually exhausted.

Re-arm's required observation reads use the same bounded retry policy and
produce a visible unresolved-rearm reason. Review the adjacent exemption
`UNVERIFIABLE` read loop in the second slice: persist an evidence-unavailable
gate after the bounded interval and re-evaluate only on a scheduled/read
request. It cannot become approval or an exemption. The linked-issue-fetch
residual is an observation distinction, not a retry counter: a known link
whose read fails is unavailable and holds the applicable operation. Do not
infer an issueless PR. These changes are bounded to the selected workflows;
unrelated residual families remain explicitly recorded.

### Scheduler, operator and dashboard contract

One authoritative projection returns workflow key, epoch/generation/version,
all unresolved gates, cause/evidence, allowed recovery operations, pending
intent and next reconciliation time. Scheduler eligibility composes this with
fresh remote lifecycle eligibility. The dashboard reads the same projection;
event folding may add historical display but cannot replace held-state truth.
For a linked PR, also join its issue's current holds. A failed linked-issue read
is unavailable, distinct from an explicitly observed absence of linkage.
An unreadable projection displays unavailable control authority and prevents
admission, rather than returning an empty gate set.

The authenticated operator endpoint submits expected generation/version and a
stable request ID, and returns `pending`, `completed`, `superseded`, `cancelled`,
or a typed conflict/rejection. Only completed reports completion; it can still
report other remaining holds. It may retain the existing
Unblock route as transport compatibility, but the selected family dispatches
one typed intent rather than calling cross-role file reset helpers. New
guidance preserves the existing keep/clear/set distinction and existing input
length validation. Agent output, labels, repository files and journal history
cannot mint an allowance renewal or authorize review dismissal.

## Migration and rollback

Schema changes are additive and versioned by the parent in the next available
control-store migrations (v19 installs workflow foundations; v20 indexes admitted
episode work and backfills only validated immutable historical bindings).
Family activation is scoped to canonical repository plus family and records
epoch, source digest, verified deployment inventory and imported targets.
Schema installation is not family activation. Live authority must not be
initialized or migrated during the deployment pause; tests use isolated DBs.

Before activation, inventory every writer and custom role-state path for the
selected repository/family, stop affected admission and drain or durably
recover runs, leases and commands. Shadow-read old authority into a comparison
artifact: absent is distinct from corrupt/unreadable, and all discovered keys
are checked, including unlabelled held targets. Compare cause, allowance and
eligibility under the same observations. Shadow data makes no decisions.
Corrupt, ambiguous duplicate or unmapped legacy keys block the affected import;
never select a convenient default. Record source bytes/digests and decisions.

Under the existing deployment barriers, revalidate sources and commit the
import plus activation epoch atomically. New code rejects selected file-family
reads/writes after activation. Old binaries already running cannot be made safe
by a new table they never read: verify they have stopped, retire prior writer
owners where required, and test that incompatible startup/schema history fails
closed. Keep stopped legacy data as evidence, never as an active fallback.
This operational old-writer fence is required in addition to epoch checks.

Before activation, rollback drops only disposable shadow artifacts. After
activation, rollback retains new SQLite authority and runs a compatible
previous implementation, or pauses affected work pending repair. Returning to
file authority requires a separately authorized, fully reconciled export with
all newer commands/receipts/generations preserved and new epochs fencing both
writers; it is outside this phase. A backup must never overwrite subsequent
charges, intent or receipts. Tests must demonstrate rollback with active and
uncertain work without replaying stale files.

At acceptance of each slice remove that family's old live reset/read/write
paths, migrated Store suffix entries and temporary migration flags. The new
runtime requires explicit activation/preflight for a selected family; an
inactive repository fails that family's admission with migration-required state
rather than falling back to files. Other, unselected families retain their
existing authority until phase 4. Legacy readers survive only in the explicit
offline importer. A temporary shadow-compare flag, if implementation needs one,
must name phase 3 slice acceptance as its executioner and be removed before
that slice's accepted commit. There is never a dual reader choosing whichever
gives a permissive result. During the protected window this runtime may remain
unstartable for live selected scopes; acceptance of implementation does not
authorize their activation.
Retained import evidence and epochs are permanent audit/fencing data, not
feature flags. Mark this ADR accepted only for implemented, reviewed slices,
and annotate ADR 0009 with the exact partial supersession then.

## Acceptance

The [phase 3 seam and fault specification](../open/2026-09-14-phase-3-gate-design.md)
defines the proposed API and bounded failure matrix. Each slice needs real
daemon + SQLite + durable executor restart tests, pure malformed-input tests,
independent adversarial review, and green `make test` before a parent commit.
Resolve acceptance precedes review implementation. Passing controlled tests
does not establish that a live cutover or unattended soak happened.

The design follows the existing reconciliation approach: observed facts drive
controllers toward explicit intent ([Kubernetes controller documentation](https://kubernetes.io/docs/concepts/architecture/controller/)).
Atomic local intent plus an idempotent consumer addresses dual-write failure
([AWS transactional outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)).
SQLite permits one writer at a time and does not nest `BEGIN` transactions,
which motivates short transactions and transaction-aware store helpers
([SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html)).
