"""Family-neutral compare-and-swap commit helpers for workflow control; ADR 0015.

Extracted from the resolve control without behaviour change so the review
family commits through the same receipt, replay and CAS rules. Callers own the
surrounding `BEGIN IMMEDIATE` transaction and all family-specific validation;
nothing here performs remote I/O or decides policy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

from ..domain.review_workflow import (
    AnyWorkflowSnapshot, CancelledReviewRearm, CompletedReviewRearm, PendingReviewRearm, ReviewRearmIntent,
    SupersededReviewRearm,
)
from ..domain.workflow import (
    AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, OpenGate, PendingRearm, RearmIntent, RearmObligation, RearmingGate, RequestRearm,
    ResolveAttemptResult,
    SupersededRearm, WorkflowChanged, WorkflowCommitted, WorkflowConflict,
)
from ..domain.workflow_identity import WorkflowWriter
from ..domain.workflow_retry import RetryAuthorizationFacts, minted_authorization
from .control_store import SQLiteControlStore, _stamp
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError

_INTENT_STATES: dict[type, str] = {
    PendingRearm: "pending", CompletedRearm: "completed",
    CancelledRearm: "cancelled", SupersededRearm: "superseded",
    PendingReviewRearm: "pending", CompletedReviewRearm: "completed",
    CancelledReviewRearm: "cancelled", SupersededReviewRearm: "superseded",
}


def opened_gate(gate: OpenGate | RearmingGate) -> OpenGate:
    return gate.gate if isinstance(gate, RearmingGate) else gate


def intent_state(intent: RearmIntent | ReviewRearmIntent) -> str:
    """The indexed `workflow_rearms.state` of a typed intent of either family."""
    return _INTENT_STATES[type(intent)]


def accepted_proposal(proposal: WorkflowChanged) -> str:
    """Retain accepted facts and plan; these are not the operator's identity."""
    snapshot: AnyWorkflowSnapshot = replace(
        proposal.snapshot, gates=tuple(sorted(proposal.snapshot.gates, key=lambda g: opened_gate(g).gate_id)),
    )
    return json.dumps({
        "mutation": proposal.mutation.value,
        "expected": encode_workflow(proposal.expected),
        "snapshot": encode_workflow(snapshot),
        "audit": encode_workflow(proposal.audit),
        "commands": [{
            "id": c.command_id, "idempotency": c.idempotency_key,
            "kind": c.kind.value, "target": c.target,
            "revision": c.expected_revision, "payload": dict(c.payload),
            "config": c.config_version, "repository": c.repository,
            "owner": c.executor_identity, "workflow": encode_workflow(c.workflow),
        } for c in proposal.commands],
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)


def transition_receipt(proposal: WorkflowChanged) -> str:
    """A paid result's receipt is its exact audit; every other transition keeps its plan."""
    return encode_workflow(proposal.audit) if isinstance(proposal.audit, ResolveAttemptResult) else accepted_proposal(proposal)


def same_receipt(raw: str, proposal: WorkflowChanged, encoded: str) -> bool:
    if not isinstance(proposal.audit, AuthorizedOperatorCommand):
        return raw == encoded
    try:
        accepted = json.loads(raw)
        if not isinstance(accepted, dict):
            raise ValueError("accepted workflow audit must be an object")
        accepted_audit = accepted.get("audit")
        if not isinstance(accepted_audit, str):
            raise ValueError("accepted workflow audit is missing its typed payload")
        audit = decode_workflow(accepted_audit, AuthorizedOperatorCommand)
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError("invalid accepted operator audit") from exc
    # Authentication is established before this bound port is called. Replay
    # belongs to the same authenticated subject and exact operator command;
    # refreshed observations, channel provenance and regenerated plans do not
    # authorize a second write or replace the original accepted audit.
    return audit.actor.subject == proposal.audit.actor.subject and audit.command == proposal.audit.command


def transition_dedupe_key(proposal: WorkflowChanged) -> tuple[str, str]:
    audit = proposal.audit
    if isinstance(audit, ResolveAttemptResult):
        # A run is one paid leg. The full payload also binds the leg identity;
        # changing just that identity cannot buy another allowance charge.
        return "workflow-result", str(audit.attempt.run_id)
    if isinstance(audit, AuthorizedOperatorCommand):
        command = audit.command
        if isinstance(command, (RequestRearm, ContinueGrantedRearm)):
            return "workflow-request", command.request_id
        if isinstance(command, CancelRearm):
            return "workflow-cancellation", command.cancellation_id
        if isinstance(command, AuthorizeAbsentRetry):
            return "workflow-retry-authorization", command.authorization_id
    return "workflow-transition", hashlib.sha256(encode_workflow(proposal.expected).encode()).hexdigest()


def replay_or_conflict(
    db: sqlite3.Connection, repository: str, prior: AnyWorkflowSnapshot,
    transition: WorkflowChanged, encoded: str,
) -> WorkflowCommitted | WorkflowConflict | None:
    """An exact replay is idempotent; reuse or a moved revision conflicts; None proceeds."""
    namespace, dedupe = transition_dedupe_key(transition)
    receipt = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (namespace, repository, dedupe),
    ).fetchone()
    if receipt is not None:
        if not same_receipt(receipt["payload_json"], transition, encoded):
            return WorkflowConflict(prior.revision, "durable workflow identity was reused with different content")
        return WorkflowCommitted(prior, idempotent=True)
    if prior.revision != transition.expected:
        return WorkflowConflict(prior.revision, "workflow revision changed before commit")
    return None


def record_commit(
    db: sqlite3.Connection, control: SQLiteControlStore, repository: str,
    transition: WorkflowChanged, encoded: str,
) -> WorkflowCommitted:
    """Persist the dedupe receipt and outbox transition for an applied commit."""
    namespace, dedupe = transition_dedupe_key(transition)
    db.execute(
        "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) VALUES (?, ?, ?, ?, ?)",
        (namespace, repository, dedupe, control._now().isoformat(), encoded),
    )
    control._transition(
        db, "workflow", encode_workflow(transition.expected.fence.key), transition.mutation.value,
        {"generation": transition.snapshot.revision.fence.generation,
         "version": transition.snapshot.revision.version},
    )
    return WorkflowCommitted(transition.snapshot)


def insert_pending_rearm(
    db: sqlite3.Connection, scope: tuple[str, str, int],
    intent: PendingRearm | PendingReviewRearm, audit: AuthorizedOperatorCommand,
) -> None:
    db.execute(
        "INSERT INTO workflow_rearms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (intent.request_id, *scope, intent.gate_id, intent.from_generation, intent.to_generation,
         "pending", hashlib.sha256(encode_workflow((audit.actor.subject, audit.command)).encode()).hexdigest(),
         encode_workflow(audit), encode_workflow(intent)),
    )


def update_rearm_state(db: sqlite3.Connection, intent: RearmIntent | ReviewRearmIntent) -> None:
    db.execute(
        "UPDATE workflow_rearms SET state=?, intent_json=? WHERE request_id=?",
        (intent_state(intent), encode_workflow(intent), intent.request_id),
    )


def record_stop_evidence(db: sqlite3.Connection, scope: tuple[str, str, int], proposal: WorkflowChanged) -> str:
    """The audited retirement disposition of a stopped intent; returns its evidence id."""
    retirement_id = "workflow-stop:" + hashlib.sha256(encode_workflow(proposal.audit).encode()).hexdigest()
    db.execute(
        "INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
        (retirement_id, *scope, encode_workflow(proposal.audit)),
    )
    return retirement_id


def retire_rearm_obligation(
    db: sqlite3.Connection, intent: CancelledRearm | SupersededRearm | CancelledReviewRearm | SupersededReviewRearm,
    command_id: str,
) -> None:
    db.execute(
        "UPDATE workflow_rearm_commands SET disposition='retired', disposition_evidence_json=? "
        "WHERE request_id=? AND command_id=?",
        (encode_workflow(intent), intent.request_id, command_id),
    )


def owns_command(db: sqlite3.Connection, writer: WorkflowWriter, owner: str) -> bool:
    """Terminal receipts retain their executor through audited adoption."""
    seen: set[str] = set()
    for _ in range(64):
        if owner == writer.owner_id:
            return True
        if owner in seen:
            return False
        seen.add(owner)
        row = db.execute(
            "SELECT successor_owner_id, repository, role FROM retired_owners WHERE owner_id=?", (owner,),
        ).fetchone()
        if row is None or row["repository"] != writer.repository or row["role"] != writer.role:
            return False
        owner = row["successor_owner_id"]
    return False


def persist_retry_authorization(
    db: sqlite3.Connection, control: SQLiteControlStore, transition: WorkflowChanged,
    facts: RetryAuthorizationFacts,
) -> None:
    """Persist one accepted operator retry grant (both families share the grant contract)."""
    audit = transition.audit
    assert isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, AuthorizeAbsentRetry)
    grant = minted_authorization(audit.command, facts)
    association = grant.association
    request_id = association.request_id if isinstance(association, RearmObligation) else None
    db.execute(
        "INSERT INTO workflow_retry_authorizations (authorization_id, request_id, command_id, gate_id, "
        "association_kind, binding_json, evidence_id, request_digest, actor_json, generation, accepted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (grant.authorization_id, request_id, str(grant.command_id), grant.gate_id,
         "rearm" if request_id is not None else "standalone",
         encode_workflow(grant.binding), grant.evidence_id,
         hashlib.sha256(encode_workflow((audit.actor.subject, audit.command)).encode()).hexdigest(),
         encode_workflow(audit), grant.binding.fence.generation, _stamp(control._now())),
    )
    control._transition(db, "workflow_retry_authorization", grant.authorization_id, "authorized", {
        "command_id": str(grant.command_id), "gate_id": grant.gate_id, "evidence_id": grant.evidence_id,
    })
