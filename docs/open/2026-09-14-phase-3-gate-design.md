# Phase 3 — Proposed shared seams and bounded acceptance

Design companion to [proposed ADR 0015](../archive/adr/0015-generation-fenced-gate-rearm.md).
The ADR number is reserved here during design: repository contracts require
documents in `docs/adr/` to name governed source with corresponding source
references. Move it there with those implementation seams after release.
The parent owns production core types, port signatures, schema and admission/
effect seams. These are exact API proposals for review, not implemented APIs.
No phase 3 production changes are authorized until phases 1–2 are accepted.
Phases 1–2 were accepted at `1703195`; the parent released the first resolve
slice on 2026-09-14. Shared seams still require parent application. The
[first-slice core proposal](2026-09-14-phase-3-resolve-core-proposal.md) excludes
the review-only values below from that initial patch.

## Core values

Use one workflow per typed target. Family activation and episode values attach
to it; they do not each own a workflow transition loop. Existing `RunId`,
`CommandId`, runtime/session values and validated review values should be reused.
The declarations below use existing `CommandId`/`RunId` and ordinary immutable
Python aliases for brevity. New IDs are opaque nonempty bounded strings;
positive IDs/counters reject booleans, negatives, non-integers and overflow.
Repository names are canonicalized at the existing boundary.

```python
@dataclass(frozen=True)
class IssueRef:
    number: int

@dataclass(frozen=True)
class PullRequestRef:
    number: int

WorkflowTarget = IssueRef | PullRequestRef

@dataclass(frozen=True)
class WorkflowKey:
    repository: str
    target: WorkflowTarget

class WorkflowFamily(str, Enum):
    RESOLVE = "resolve"
    REVIEW_REJECTIONS = "review_rejections"

@dataclass(frozen=True)
class WorkflowFence:
    key: WorkflowKey
    generation: int

@dataclass(frozen=True)
class FamilyAuthority:
    family: WorkflowFamily
    epoch: int

@dataclass(frozen=True)
class WorkflowRevision:
    fence: WorkflowFence
    version: int

@dataclass(frozen=True)
class ResolveEpisode:
    episode_id: str
    failures: int
    last_failure: str
    guidance: str

@dataclass(frozen=True)
class FreshReviewRequired:
    episode_id: str
    guidance: str

@dataclass(frozen=True)
class ReviewedBaseline:
    episode_id: str
    head_sha: str
    patch_id: str
    guidance: str

ReviewContinuation = FreshReviewRequired | ReviewedBaseline

@dataclass(frozen=True)
class ResolveCeiling:
    episode_id: str
    failures: int
    ceiling: int
    last_failure: str

@dataclass(frozen=True)
class ReviewRejectionCeiling:
    head_sha: str
    review_bot: str
    counted_reviews: tuple[SubmittedReview, ...]
    ceiling: int

@dataclass(frozen=True)
class RemoteOutcomeUncertain:
    command_id: CommandId
    unresolved_since: datetime
    failed_reads: int
    last_error: str

@dataclass(frozen=True)
class EvidenceUnavailable:
    required_fact: str
    unresolved_since: datetime
    failed_reads: int
    last_error: str

@dataclass(frozen=True)
class RearmNeedsDecision:
    stopped_request_id: str
    obligations: tuple[CommandId, ...]
    reason: str

GateCause = ResolveCeiling | ReviewRejectionCeiling | RemoteOutcomeUncertain | EvidenceUnavailable | RearmNeedsDecision

@dataclass(frozen=True)
class NoAuthorizedRemediation:
    reason: str

@dataclass(frozen=True)
class RemediationAttempted:
    operation: str
    receipt_ids: tuple[str, ...]
    outcome: str

Remediation = NoAuthorizedRemediation | RemediationAttempted

@dataclass(frozen=True)
class OpenGate:
    gate_id: str
    opened_generation: int
    cause: GateCause
    remediation: Remediation

@dataclass(frozen=True)
class RearmingGate:
    gate: OpenGate
    request_id: str

UnresolvedGate = OpenGate | RearmingGate

@dataclass(frozen=True)
class KeepGuidance: pass

@dataclass(frozen=True)
class ClearGuidance: pass

@dataclass(frozen=True)
class SetGuidance:
    text: str

GuidanceEdit = KeepGuidance | ClearGuidance | SetGuidance

@dataclass(frozen=True)
class RequestRearm:
    request_id: str
    expected: WorkflowRevision
    gate_id: str
    guidance: GuidanceEdit

@dataclass(frozen=True)
class ReconcileGate:
    expected: WorkflowRevision
    gate_id: str

@dataclass(frozen=True)
class AuthorizeAbsentRetry:
    authorization_id: str
    expected: WorkflowRevision
    command_id: CommandId
    gate_id: str
    evidence_id: str

@dataclass(frozen=True)
class CancelRearm:
    cancellation_id: str
    expected: WorkflowRevision
    request_id: str
    reason: str

@dataclass(frozen=True)
class ContinueGrantedRearm:
    request_id: str
    expected: WorkflowRevision
    stopped_request_id: str

@dataclass(frozen=True)
class RenewResolveAllowance:
    episode_id: str

@dataclass(frozen=True)
class RequireFreshReview:
    episode_id: str

@dataclass(frozen=True)
class DismissCountedReview:
    review: SubmittedReview
    head_sha: str

@dataclass(frozen=True)
class EnsureLifecycleProjection:
    target: WorkflowTarget

RearmOperation = RenewResolveAllowance | RequireFreshReview | DismissCountedReview | EnsureLifecycleProjection

@dataclass(frozen=True)
class PendingRearm:
    request_id: str
    gate_id: str
    from_generation: int
    to_generation: int
    operations: tuple[RearmOperation, ...]
    required_commands: tuple[CommandId, ...]

@dataclass(frozen=True)
class CompletedRearm:
    request_id: str
    gate_id: str
    to_generation: int
    verified_commands: tuple[CommandId, ...]

class SupersessionReason(str, Enum):
    TARGET_TERMINAL = "target_terminal"
    DECISION_CHANGED = "decision_changed"
    REQUIRED_EFFECT_REJECTED = "required_effect_rejected"

@dataclass(frozen=True)
class SupersededRearm:
    request_id: str
    gate_id: str
    to_generation: int
    reason: SupersessionReason
    evidence_id: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]

@dataclass(frozen=True)
class CancelledRearm:
    request_id: str
    gate_id: str
    to_generation: int
    cancellation_id: str
    reason: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]

RearmIntent = PendingRearm | CompletedRearm | SupersededRearm | CancelledRearm

@dataclass(frozen=True)
class CommandAbsenceEvidence:
    evidence_id: str
    command_id: CommandId
    fence: WorkflowFence
    observed_at: datetime
    remote_revision: str

@dataclass(frozen=True)
class CommandRetryApplicability:
    evidence_id: str
    command_id: CommandId
    binding: WorkflowBinding
    observed_at: datetime
    decision_guard_digest: str

@dataclass(frozen=True)
class StandaloneCommand: pass

@dataclass(frozen=True)
class RearmObligation:
    request_id: str

CommandAssociation = StandaloneCommand | RearmObligation

@dataclass(frozen=True)
class UnconsumedRetryAuthorization:
    authorization_id: str
    command_id: CommandId
    gate_id: str
    evidence_id: str
    fence: WorkflowFence
    association: CommandAssociation

@dataclass(frozen=True)
class ConsumedRetryAuthorization:
    authorization: UnconsumedRetryAuthorization
    command_attempt: int

RetryAuthorization = UnconsumedRetryAuthorization | ConsumedRetryAuthorization
```

`GateCause` is intentionally bounded to this phase. Existing explicit human
holds remain in the fresh lifecycle eligibility input; no proposal permits a
ceiling re-arm to release them implicitly. Later gate families extend the union
with their own runtime-validated evidence and permitted operations.
`RemediationAttempted.operation/outcome` must become bounded enums for actual
operations introduced in each slice; arbitrary input strings cannot select an
executor. `NoAuthorizedRemediation` is valid for a spent operator-granted
allowance; it is not a bypass around an existing free remediation.

`EnsureLifecycleProjection` is a domain obligation, not executable arbitrary
label payload. Policy derives existing typed actions from fresh facts, current
gate set and explicit authorized hold ownership. It restores a selectable
trigger only when no other blocker forbids that operation. Required command IDs
are added transactionally as prerequisites become true; a remote command queue
row must not become executable merely because a future operation is listed.

No absent/unavailable conflation: fresh input is a union of observed value with
captured revision, observed absence with its queried identity, or unavailable
with reason/time. A missing linked-issue fetch yields unavailable; explicit
`NoLinkedIssue` is a different value. Do not use `Issue | None` for both. Review
input also proves pagination completeness; partial review lists cannot disprove
a rejection cause or authorize dismissal. Timestamps come from adapters, never
ambient domain clocks. Claims from paid children cannot mint these observations.

`CommandAbsenceEvidence` is minted only by the command-specific harness
reconciler after a complete read proves the current postcondition is not present.
This says nothing about historical execution or present permission to retry.
A retired command with changed guards may still obtain this evidence, resolving
current uncertainty and allowing safe continuation without refund or revival.
Separate `CommandRetryApplicability` proves that a nonretired original decision
still authorizes execution; only a retry consumes that proof. A failed read or missing row in incomplete
data cannot construct it. For dismissal, an existing counted review still in
the captured actionable state proves the dismissal absent; a vanished review
requires disposition/re-evaluation, not a fabricated absent-dismissal proof.
Bounded freshness and revision/guard equality are checked again before the
authorized execution. The command's original guard is never rewritten to the
evidence's revision without re-evaluating and replacing obsolete intent.

`AuthorizeAbsentRetry` belongs to the workflow binding, command and uncertainty
gate, with typed `StandaloneCommand` or `RearmObligation` association minted
from authoritative command linkage. A standalone completion/publication command
needs no fabricated re-arm; a re-arm obligation requires its original request
still pending, and can be retried while that request occupies the pending slot.
It is not `RequestRearm` and grants no allowance. Store its canonical payload and
trusted actor; duplicate exact authorization returns its current consumed or
unconsumed result, while changed-payload reuse conflicts. The same command ID
continues. Consuming the authorization and incrementing the existing command
attempt happens in the same `begin_command` transaction; no separate flag may
leave a retry token consumed while a command still appears safe to re-execute.
After consumption, crash/unknown outcome returns to read-only reconciliation.
Original unresolved age and expiry stay recorded; every subsequent write needs
a new explicit authorization and fresh absence proof. A persisted unconsumed
authorization can resume after restart at the next scheduled read, and
survives bounded transient read failures; see ADR 0015's fault rows for the
implemented dispositions and ceilings. This is a one-write grant, not an
unbounded retry-window reset.

Terminal `SupersededRearm`/`CancelledRearm` records free the pending slot while
retaining all prior effects and allowance accounting. Obligation retirement
removes authority to execute, not evidence that a write might have happened.
Unknown retired commands stay reconcilable and keep their uncertainty gate;
unexecuted retired commands never drain as ordinary pending work. No terminal
intent can resume on reopen. `ContinueGrantedRearm` refers to one stopped intent
and is idempotent by its new request ID; it advances workflow generation, keeps
the same selected episode/counter, and uses fresh policy to plan only remaining
scope. It cannot silently add new review IDs to the prior dismissal grant.
If changed facts require new IDs, reject the continuation with that exact
missing authorization. Conflicting uncertainty from the stopped intent blocks
replacement execution until resolved. `RearmNeedsDecision` and unrelated holds
survive until policy has verified the replacement's required outcomes.

## Transition and ports

The parent can integrate these methods into the existing control adapter and
add a narrow domain port, rather than exposing raw connections to app code.
Bind that adapter to an explicit `WorkflowWriter(repository, role, owner_id)`
created from the existing configured deployment authority. Every mutation
asserts owner activity inside its transaction and validates that every workflow,
family and command belongs to that repository and the permitted role. All reads
are scoped to the bound repository; a mismatched key is rejected, not queried.
The operator submission service uses the configured target role's writer and
separate authenticated actor evidence. It cannot supply an arbitrary owner ID
in request JSON. A type conforming caller alone is not mutation authority.
`WorkflowSnapshot` consists of revision, active family authorities, typed family
episode values, unresolved gates and optional current re-arm intent. Omission
of a family means not activated, not an empty/renewed episode. A single pure
transition consumes this snapshot, validated observations and typed input,
returning the proposed next snapshot plus existing durable command intents and
telemetry. No generic pluggable workflow registry is needed for two slices.

```python
class WorkflowControl(Protocol):
    @property
    def writer(self) -> WorkflowWriter: ...

    def load_workflow(self, key: WorkflowKey) -> WorkflowSnapshot: ...

    def commit_workflow(
        self,
        expected: WorkflowRevision,
        authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowTransition,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def lookup_rearm(
        self, key: WorkflowKey, request_id: str
    ) -> RearmIntent | None: ...

    def held_projection(self, key: WorkflowKey) -> HeldProjection: ...
```

Corruption/unavailable authority raises the existing fail-closed control error;
it never returns `None` or an empty projection. `lookup_rearm` returns `None`
only for verified absence. Exact request replay compares the canonical request
payload before returning an existing intent; ID reuse with different guidance,
target or gate is a conflict. Trusted actor/channel metadata is minted at the
operator boundary and stored alongside the request, never taken from the
request's arbitrary JSON. API parsing rejects unknown variants/fields and
preserves current guidance validation.

The admission seam takes `WorkflowFence` and `FamilyAuthority` as captured
authorization alongside existing reservation inputs and validates them inside
the existing transaction. Result accounting takes that same fence plus existing
`RunId`/leg identity and a typed classification. It appends a unique result and
updates only the matching episode. The finalization receipt remains valid for
financial settlement even when workflow generation is stale.

Add an optional validated workflow fence to existing lifecycle commands for
unmigrated compatibility; it is mandatory for commands that mutate an activated
workflow. This requirement is checked at enqueue and execution, not merely by
callers. Old persisted commands must be explicitly reconciled/adopted or held
during activation; auto-attaching the newest generation would launder old
intent. `HeldProjection` gives all blockers and pending operation details;
adapters supply fresh remote holds using the existing eligibility policy. The
same composed projection is supplied to scheduler and dashboard transport.

The parent-owned `commit_workflow` transition union gains
`AuthorizeAbsentRetry`, `CancelRearm`, `ContinueGrantedRearm` and observed
supersession. All use the same CAS/outbox transaction. Add one narrow
transactional effect-store seam:

```python
def begin_authorized_retry(
    self,
    command_id: CommandId,
    authorization_id: str,
    expected: WorkflowRevision,
    evidence: CommandAbsenceEvidence,
    applicability: CommandRetryApplicability,
) -> RetryBegan | RetryAlreadyConsumed: ...
```

This validates actor-minted authorization, workflow/command/gate ownership,
the associated pending request only for a re-arm obligation,
family epoch, generation, unchanged guard, fresh minted evidence, nonretired
obligation and nonconsumption, then atomically consumes and begins exactly one
command attempt. Caller holds the existing shared effect barrier; it performs
no I/O inside the SQL transaction. A refusal after consumption keeps the
existing verified-refusal accounting and visible failed obligation. A repeat
call returns `RetryAlreadyConsumed` carrying the stored consumption and cannot
authorize another network invocation. Only `RetryBegan` carrying newly committed
consumption authorizes this invocation. Both variants are frozen dataclasses.

## Role ownership, credentials and drain wiring

Current `GitHubOperatorEffects` constructs `UnfencedEffectAuthority` and the
dashboard selects `repository:dashboard[:review:*]` executor identities.
`GitHubEffectExecutor.drain_pending_commands` filters its own exact identity
and currently does not own `DISMISS_REVIEW`. Reusing those operator methods
unchanged would not satisfy this design. The following is required wiring:

| Obligation | Existing role owner / credential | Periodic executor |
| --- | --- | --- |
| Resolve issue projection | `_owner_identity(cfg, "fix")`; existing fix gateway with FIX_BOT token/ambient contract from `app/main.py` | Fix `GitHubEffectExecutor`, existing relabel command |
| Counted review dismissal | `_owner_identity(cfg, "review")`; existing review gateway built with `review_identity`, validating configured bot and required permission | Review `GitHubEffectExecutor`, extend owned vocabulary/decoder with typed `DISMISS_REVIEW` and gateway read/dismiss/verify methods |
| Review PR projection | Same review owner and gateway | Review executor, existing PR relabel command |

Persist these exact configured active owners on command rows at intent commit;
dashboard actor/channel lives only on the intent/audit event. Resolve and review
intent progression is driven by its named role; both inspect receipts through
the same workflow authority. The review slice does not silently mutate the
linked issue's hold. Failure to read that issue blocks applicable progression.
Do not instantiate an operator effects adapter, append `:dashboard` or
`:rearm` to executor identity, or create a new publisher for these obligations.
Actor permission to submit a request does not select the network credential.

In `roles._effects`, compose bounded workflow reconciliation before and after
the existing owned command drain. Wire that same combined drain into normal
`Executor.replay_pending` and startup/wake `EffectRecovery`; the normal daemon
already calls replay before observation. A normal poll must therefore progress
the intent after restart without another HTTP request and even if paid work is
blocked or world observation is skipped. The callback reads the active role's
scoped workflow rows, materializes newly eligible commands, drains due work and
evaluates verified completion; it is not a second queue. No independent loop
or thread is added. Test observation failure/probe skip and spend-cap paths
against this actual wiring. Missing configured executor role rejects intake;
a stopped configured daemon leaves explicit pending/executor-unavailable state.

Every migrated effect runs through the existing executor's
`DurableEffectRunner(DeploymentAuthority(...))`. Acceptance, cancellation and
supersession take repository barriers in fixed `fix`, `review`, `steward` order,
outside SQL; refresh through read-only gateways, validate owner fences, commit,
then release all exclusive locks. Only afterward may a role runner acquire its
ordinary shared barrier and execute. Never call `runner.authorized`,
`runner.execute`, `resolve_unknown` or an operator effect inside the exclusive
section. This avoids shared-lock reacquisition on a distinct file descriptor.
Conflicting active paid work must drain/recover as already specified. Legacy
dashboard-owned pending commands are separately inventoried/reconciled during
activation; they are never assumed to be owned by this newly extended drain.

## Additive schema proposal

These are logical columns/constraints for the parent's migration, not an
independently owned schema implementation. Reuse existing transaction helpers,
run rows, lifecycle commands, receipts, dedupe and event outbox.

| Table | Key and relevant fields / constraints |
| --- | --- |
| `workflow_authority_epochs` | PK `(repository, family)`; epoch, activation timestamp, source digest, validated inventory digest; no downgrade of epoch |
| `workflows` | PK `(repository, target_kind, target_number)`; positive number; nonnegative generation/version; canonical kinds `issue`/`pr` |
| `workflow_episodes` | FK workflow; PK `(workflow, family)`; authority epoch, episode ID, typed versioned payload; reject unknown/contradictory variants on read |
| `workflow_gates` | FK workflow; gate ID; opened generation; typed cause/evidence/remediation; open/rearming/closed state validated against request/closure fields; unique `(workflow, opened_generation, cause_key)` |
| `workflow_rearms` | FK workflow; request ID + canonical payload digest + trusted actor/channel; gate ID, from/to generation, bounded typed operation plan; pending/completed/superseded/cancelled variants with required disposition evidence; unique pending intent per workflow and unique consumed generation; continuation references prior stopped intent and same episode |
| `workflow_command_links` | FK re-arm + existing command; operation identity/ordinal; unique command link, validated target/fence match; required or audited retired obligation; no independently advancing effect status |
| `workflow_attempt_results` | Existing run/leg identity + workflow/family/episode/generation, typed result; unique run/leg + family; insert and counter transition share one transaction |
| `command_reconciliation` | PK existing command ID; first unresolved time, next read time, read count, typed active/reconciliation-only/retired mode; validated absence evidence IDs; preserves existing lifecycle state/receipts/refusal count; retired unknowns remain read-only reconcilable |
| `command_retry_authorizations` | PK authorization ID; FK workflow/command/gate/evidence; typed standalone or re-arm association, with request FK required only for the latter; canonical payload digest, trusted actor, generation; unconsumed or consumed with exact existing command attempt; consume atomically with begin, never reset allowance/expiry |

The next schema version is assigned centrally. A SQL writer validates the
domain payloads and relational constraints together; storing JSON alone is not
runtime validation. If the existing dedupe table can carry attempt-result
uniqueness and the existing command table can carry reconciliation fields
without diluting their contracts, prefer those additive columns/keys over new
tables. `workflow_command_links` records obligations only; command state comes
exclusively from lifecycle commands/receipts. No second queue exists here.

## Migration evidence and deletion ledger

Shadow comparison records exact source identity, target mapping, absent versus
present/corrupt/unreadable, digest, old/new count/cause and eligibility. Include
targets discoverable only from files and custom state roots. Activation fails
for ambiguous aliases/owners; no `max`/`min` union silently chooses an allowance.
All affected owners and paused processes must be verified, not assumed from
one deployment's service status. A record for a different repository remains
inactive even after additive schema installation.

| Slice | Transfer / delete live paths when accepted | Deferred ownership |
| --- | --- | --- |
| Resolve | `resolve-retries`, `last-error` explanation; `_bump_resolve_retry` read-modify-write; `reset_resolve_episode`; selected unblock cross-role reset path and label/event escalation dedup; replace with episode result + gate transition |
| Review rejection | `last-sha`, `patch-id`, `operator-guidance`; selected `_rearm_rejections`/`_reset_review_baseline` choreography; selected review ceiling label-only dedup; generation-aware review baseline writes and copy-skip eligibility | `review-retries` unproductive rounds, infra deaths, consumed replies, runtime sessions and branch lineage remain for phase 4 |

The resolve row's deferred ownership is likewise infra deaths, other retries,
consumed replies, session/model/phase and branch lineage. Advisory error text
can be displayed from historical events, but the current episode explanation
has one owner. Legacy migration readers live only in a bounded import path;
they are never a live fallback after activation. At slice acceptance, new
runtime admission requires activation of the selected family; inactive scopes
report migration-required and never use the removed file authority. Other
unselected families are unaffected. The executioner of any temporary
shadow-compare flag is that slice's acceptance, before its accepted commit.
The live scope remains paused and unactivated until separately authorized.

Rollback before activation leaves original authority unchanged. After
activation retain SQLite records and use compatible code or keep work paused;
never re-enable old file reads. The acceptance artifact must show newer result
dedup, generations, pending/unknown commands and spend survive rollback.

## Bounded acceptance fault matrix

Run each resolve case through the real daemon, SQLite and durable executor,
then restart using the same isolated database and persistent GitHub double.
Extend the accepted matrix for review; do not begin its implementation before
resolve acceptance. Each row names an observable outcome rather than a private
state layout.

| Fault/interleaving | Required behavior |
| --- | --- |
| Before re-arm local commit / after commit before HTTP response | First case changes nothing; second replays the same request, generation and command identity without another grant |
| Two same-generation requests, same/different IDs; reused ID changed payload | One accepted renewal; exact replay succeeds; stale or conflicting requests do not renew again |
| Old completion before/after re-arm acceptance; active paid run | Spend settles once, stale result does not charge new episode or restore old lifecycle; no overlapping new-generation dispatch |
| Failure at each remote mutation and receipt boundary, including success before receipt | Restart reconciles same command; gate remains until required result is verified; remote mutation does not duplicate |
| Add-trigger succeeds, remove-hold fails | Both labels may exist; scheduler remains held and dashboard gives same pending cause |
| Human hold/terminal issue/new linked PR appears during re-arm | Fresh policy preserves human disposition; no blind ready promotion or close; unrelated gate is not discharged |
| Two independent gates and linked issue holds | Renew only selected allowance; preserve other cause, labels and held explanation; no issue/PR identity collision |
| Unavailable linked issue/review pagination/remote revision | Visible unavailable input; never absent linkage, empty reviews, false cause or permission to mutate |
| Repeated unknown outcomes until age/read ceiling | One visible uncertainty gate; bounded scheduled reads; zero blind re-executions or refusal-budget consumption; later positive evidence resolves same command |
| Expiry → verified absence → authorized retry → restart before/after token consumption | Same pending request/generation/episode/command; one write per authorization; exact replay cannot renew grant; pre-consumption restart resumes token, post-consumption restart reconciles only; no renewed allowance or expiry reset |
| Dashboard exits after accepting review re-arm; restart review daemon with real owner filtering | Existing review-role drain discovers dismissal and PR projection; review credential and deployment barrier apply; eventually verified completion without another request |
| Exclusive acceptance overlaps an in-flight shared effect; paid cap / probe skip / observation error | No nested-lock deadlock; bounded periodic intent progress independent of paid admission and dashboard lifetime |
| Target closes/reopens or head changes; stale required effect permanently refuses | Audited superseded intent frees pending slot, never reports completion/refund/eligibility; reopened target retains holds and cannot replay retired command; continuation keeps allowance and requires fresh authorized scope |
| Operator cancels after partial success or unknown effect, then continues | Prior success/receipt remains; unknown stays gated and read-only; no conflicting replacement write; continuation cannot repeat verified effect or grant another allowance |
| Cancelled/superseded unknown → changed guard → verified current absence → continuation | Retired command's current uncertainty resolves without historical non-execution/refund claim or retry permission; fresh authorized continuation can advance |
| Ordinary completion/publication command → expiry → current absence → authorized retry | Workflow/command/gate owns one-write token without creating a re-arm request or renewing allowance; original guard and publication/run association remain mandatory |
| Timer/restart, clock skew, extremely large retry configuration | Persistent schedule does not become tight polling or reset its unresolved age; bounded validated parameters; backward time cannot manufacture expiry evidence |
| Review dismissal: all, some, none, unknown; new review arrives | Complete only after exact required bot IDs are verified dismissed; preserve human/new reviews; newly true rejection cause remains visible |
| Review guidance fails local commit; keep/clear/set input | No renewal if commit fails; otherwise next paid review receives committed selected guidance; no successful unscoped review |
| All counted reviews dismissed but head/patch unchanged | Fresh review obligation dispatches once; no unchanged-patch reuse of the exhausted generation; new result establishes new baseline |
| Re-escalation after a completed re-arm | New generation opens a new cause gate even if old attention label/event remains |
| Missing/malformed/unreadable legacy record; ambiguous owner/PR mapping | Distinguish absent import from failed import; no granted allowance from corruption; healthy unrelated scopes continue |
| Activation crash, stale source digest, old process/binary, rollback after remote write | Atomic scoped authority or fail closed; old writer cannot advance migrated family; rollback preserves effects/charges and cannot replay old files |
| Repeated review `UNVERIFIABLE` and later available evidence | Bounded visible evidence gate; never an approval/exemption; evidence recovery re-evaluates the original verification contract |

Acceptance evidence records which matrix rows ran, exact commands/logs, isolated
DB provenance, deletions, remaining scopes and independent reviewer findings.
Full `make test` is mandatory before any parent commit. Live migration/soak is
pending while the user's deployment pause remains in force.
