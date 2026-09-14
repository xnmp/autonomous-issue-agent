# Phase 3 — First resolve slice core patch proposal

Design only; parent applies shared seams after phases 1–2 acceptance. This
narrows the [whole-phase proposal](2026-09-14-phase-3-gate-design.md) to types
and persistence used by the first resolve slice. Review causes, dismissal
operations and reviewed-head/patch continuation are not implemented in this
patch. They enter only after resolve acceptance.

## Module and authority boundary

`domain/workflow_identity.py` contains `IssueRef`, `PullRequestRef`,
`WorkflowKey`, `WorkflowFence`, `WorkflowRevision`, `FamilyAuthority` and
`WorkflowBinding`. It imports no `contracts`, `ports`, workflow payloads or
adapters. Initially `WorkflowFamily` has only `RESOLVE`. These values validate
their own primitive invariants and canonical repository identity.

`contracts.LifecycleCommand` may import `WorkflowBinding` from this leaf module.
`domain/workflow.py` contains resolve episodes, gates, intents, transitions and
result classifications, importing both identity values and existing
`contracts.RunId`/`CommandId`. `ports` imports those domain types. This direction
avoids `contracts → workflow payload → contracts` cycles.

```python
@dataclass(frozen=True)
class WorkflowBinding:
    fence: WorkflowFence
    authority: FamilyAuthority
    episode_id: str

@dataclass(frozen=True)
class WorkflowWriter:
    repository: str
    role: Literal["fix", "review", "steward"]
    owner_id: str
```

The adapter is constructed as `SQLiteWorkflowControl(control, writer)` using
the existing active deployment's identity. First-slice resolve mutations
require role `fix`; repository/role matching is checked at construction and on
every mutation. Every transaction calls the existing `_assert_owner_active`
with the bound owner and rejects cross-repository keys, wrong family epochs,
wrong command executor identities and stale workflow revisions. Reads are
repository-scoped. The dashboard's authenticated operator actor is stored in
the request audit, distinct from the fix deployment owner. No endpoint accepts
owner identity or trusted absence evidence as caller-authored payload.

## First-slice domain and port surface

Take only these values from the whole-phase proposal: resolve episode/cause;
uncertain remote outcome and unavailable evidence causes; re-arm-needs-decision
cause; open/rearming gates; keep/clear/set guidance; request/reconcile/cancel/
continue/authorize-absent-retry commands; pending/completed/superseded/cancelled
intents; absence evidence and consumed/unconsumed retry authorization. Allowed
re-arm operations are only `RenewResolveAllowance` and
`EnsureLifecycleProjection`. Resolve results use the existing classification
policy, represented as counted requeue, infra failure, budget refusal, blocked
or PR opened; do not redefine those business outcomes.

The narrow `WorkflowControl` port remains the bound `writer`, `load_workflow`,
`commit_workflow(expected, authorities, transition)`, `lookup_rearm`, and
`held_projection` contract in the whole-phase proposal. `WorkflowTransition`
is an immutable typed proposal validated by pure resolve policy. Its commit
must not accept arbitrary SQL, unvalidated action dictionaries or a supplied
replacement generation. The store checks the transition's allowed version/
generation/episode changes, command ownership and audit provenance.

The effect-store seam adds `begin_authorized_retry(command_id,
authorization_id, expected, evidence, applicability) -> RetryBegan | RetryAlreadyConsumed`.
Both variants carry a consumed authorization; only `RetryBegan` permits a
network invocation. Owner/repository/family/intent/guard validation and token
consumption are inside the same transaction as command begin. The existing
`begin_command` also rejects a migrated reconciliation-only or retired command;
callers cannot bypass a required retry authorization through the old method.
The token belongs to workflow binding + command + uncertainty gate. It has a
typed standalone/re-arm-obligation association; only the latter requires an
existing pending re-arm request. Current-postcondition absence is separately
minted from execution applicability, so retired unknowns can resolve current
uncertainty without a historical non-execution claim or renewed permission.
Likewise `retry_reconciled_command` must reject expired or retired migrated
commands unless the authorized begin operation owns that transition; an old
helper cannot rewrite durable unknown state to ordinary retryable state first.

## Precise existing fields and hot-path changes

| Existing value / operation | Proposed additive field or check |
| --- | --- |
| `contracts.LifecycleCommand` | `workflow: WorkflowBinding | None = None`, validated against command repository and target; required at enqueue and begin for affected activated targets; included in identity/payload digest and serialization |
| `control_store.ReservationRequest` | `workflow: WorkflowBinding | None = None`; add to request equality/idempotency comparison; required for an activated resolve-family attempt |
| `SQLiteControlStore.reserve` | Check workflow generation, family epoch, selected episode and all applicable gates in the same existing reserve transaction; no new reservation path; idempotent replay of an already settled/admitted old binding does not create a new run |
| `runs` | `workflow_binding_json TEXT`; present only for bound migrated work and validated before use; reservation replay compares it; existing run/dispatch/claim/spend identities unchanged |
| `lifecycle_commands` | `workflow_binding_json TEXT`; same binding through enqueue, decode, begin, receipt, reconcile and recovery; captured old command cannot be rebound to current generation |
| Existing accounting runtime request construction | Thread the binding captured by the existing dispatch/paid-leg context into `ReservationRequest`, then persist on the existing run; no late workflow lookup substitutes newer authority |
| Resolve completion result | Pass captured binding + existing run/leg ID to one result-accounting transition; stale generation settles money but cannot increment new episode or emit lifecycle changes |
| `dedupe_keys` | Namespace `workflow.resolve_result`, scope canonical workflow identity, key existing run/leg identity; typed result + binding payload recorded atomically with episode update; changed-payload duplicate conflicts |
| `GitHubEffectExecutor` and normal replay | Resolve commands stay under existing fix owner/gateway/DeploymentAuthority; add typed obligation handling and bounded workflow reconciliation around current drain, not another executor identity |

Bindings on unaffected legacy work may be absent; absence on an activated
resolve target is rejected rather than treated as generation zero. `RunAgent`
and the existing dispatch reservation/context need to carry this binding once
from selection through admission and completion; the parent owns the exact
field placement in these hot signatures. Publication commands associated with
the captured resolve run also retain this fence, so a retired resolve intent
cannot publish through a separate old-generation path. Existing publication
revision/owner guards remain required. No change to paid-price quotation,
spending limits or immutable accounting receipts is intended.

## Additive DDL shape

The parent assigns the next schema version and uses existing immutable
migration machinery. The following is the first-slice DDL proposal; JSON
payload columns encode only the runtime-validated resolve union above. Foreign
keys below must be enabled under the existing connection contract.

```sql
CREATE TABLE workflow_authority_epochs (
    repository TEXT NOT NULL,
    family TEXT NOT NULL CHECK (length(family) BETWEEN 1 AND 64),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    source_digest TEXT NOT NULL,
    inventory_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (repository, family)
);
CREATE TABLE workflows (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    episode_json TEXT NOT NULL,
    PRIMARY KEY (repository, target_kind, target_number)
);
CREATE TABLE workflow_gates (
    gate_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    opened_generation INTEGER NOT NULL CHECK (opened_generation >= 0),
    cause_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'rearming', 'closed')),
    payload_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, opened_generation, cause_key),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_rearms (
    request_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    from_generation INTEGER NOT NULL CHECK (from_generation >= 0),
    to_generation INTEGER NOT NULL CHECK (to_generation = from_generation + 1),
    state TEXT NOT NULL CHECK (state IN
        ('pending', 'completed', 'superseded', 'cancelled')),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, to_generation),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE UNIQUE INDEX workflow_one_pending_rearm
    ON workflow_rearms(repository, target_kind, target_number)
    WHERE state = 'pending';
CREATE TABLE workflow_rearm_commands (
    request_id TEXT NOT NULL REFERENCES workflow_rearms(request_id),
    operation_key TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_commands(command_id),
    disposition TEXT NOT NULL CHECK (disposition IN ('required', 'retired')),
    disposition_evidence_json TEXT NOT NULL,
    PRIMARY KEY (request_id, operation_key)
);
CREATE TABLE workflow_evidence (
    evidence_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    command_id TEXT REFERENCES lifecycle_commands(command_id),
    kind TEXT NOT NULL CHECK (kind IN ('observation', 'absence', 'retry_applicability', 'prior_receipt')),
    payload_json TEXT NOT NULL,
    CHECK (kind = 'observation' OR command_id IS NOT NULL),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_retry_authorizations (
    authorization_id TEXT PRIMARY KEY,
    request_id TEXT REFERENCES workflow_rearms(request_id),
    command_id TEXT NOT NULL REFERENCES lifecycle_commands(command_id),
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    association_kind TEXT NOT NULL CHECK (association_kind IN ('standalone', 'rearm')),
    binding_json TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES workflow_evidence(evidence_id),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    consumed_attempt INTEGER CHECK (consumed_attempt > 0),
    CHECK ((association_kind = 'standalone' AND request_id IS NULL)
        OR (association_kind = 'rearm' AND request_id IS NOT NULL))
);
ALTER TABLE runs ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_reconciliation_json TEXT;
```

The reconciliation payload is a validated active/reconciliation-only/retired
union with original unresolved time, next read time and count; a retired
unknown command remains read-only reconcilable. Existing command state and
effect receipts remain the source for effect results. Store current episode's
family epoch in its typed payload and validate against the activation row on
every mutation. Cross-row target/generation/owner matching is checked inside
the same transaction even when a foreign key alone cannot enforce it.

The existing effect receipt table stores one latest receipt and retry paths
delete/replace it. Before doing that for a migrated command, archive its exact
prior receipt into `workflow_evidence(kind='prior_receipt')` in the
same transaction. This preserves authority evidence across explicit retries
without making JSONL the only history of an ambiguous or refused effect.
Retirement never deletes commands or receipts. New archival evidence and
absence proofs are append-only and may not be supplied by an operator request.

A PR workflow row cannot carry a resolve episode; first-slice constructors and
importer reject it. The typed target identity includes PR now so later review
integration does not change every fence, but first-slice activation and data
remain issue-only. The review slice extends the validated episode union after
first-slice acceptance. Generic SQL family/payload columns avoid rebuilding
authority tables merely to add that variant; current readers/writers still
reject unknown families or payload tags and support only resolve.

## Required first-slice evidence

Use the resolve-applicable rows in the whole-phase fault matrix, including
expiry → absence → authorized single retry → restart, daemon recovery without
dashboard, exclusive/shared barrier ordering, terminal/superseded/cancelled
intent, closed/reopened targets, simultaneous holds, stale publication,
owner/repository misuse, malformed bindings and retired writer fencing. The
rollback test preserves explicit retry authorizations and archived receipts.
No production schema initialization or activation is allowed while the pause
is in force. This document is a proposed patch contract, not test evidence.
