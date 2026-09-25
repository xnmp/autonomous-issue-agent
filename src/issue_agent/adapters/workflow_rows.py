"""Transaction-local workflow reads shared by admission and progression; ADR 0015."""

import hashlib
import sqlite3
import json

from ..domain.resolve_workspace import EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared

from ..domain.workflow import (
    CancelledRearm, CompletedRearm, OpenGate, PendingRearm, RearmingGate,
    ResolveEpisode, SupersededRearm, WorkflowSnapshot,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision,
)
from .workflow_codec import WorkflowEncodingError, decode_workflow, encode_workflow


class WorkflowAuthorityError(ValueError):
    pass


class WorkflowRevisionConflict(WorkflowAuthorityError):
    def __init__(self, current: WorkflowRevision, reason: str):
        super().__init__(reason)
        self.current = current


WORKSPACE_ACK_NAMESPACE = "workflow-workspace-completed"


def load_workspace_ack(db: sqlite3.Connection, handoff: EpisodeWorkspaceHandoff) -> EpisodeWorkspacePrepared | None:
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (WORKSPACE_ACK_NAMESPACE, handoff.destination.fence.key.repository, handoff.handoff_id),
    ).fetchone()
    if row is None:
        return None
    try:
        completed = decode_workflow(row["payload_json"], EpisodeWorkspacePrepared)
        if completed.handoff != handoff:
            raise ValueError("stored preparation names another workspace handoff")
        return completed
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError(f"invalid workspace acknowledgement: {exc}") from exc


def workflow_scope(key: WorkflowKey) -> tuple[str, str, int]:
    return key.repository, "issue" if isinstance(key.target, IssueRef) else "pr", key.target.number


def workflow_run_scope_bounds(key: WorkflowKey) -> tuple[str, str]:
    """Indexed target scope across retained epochs/episodes, never JSON payload scans."""
    prefix = json.dumps(workflow_scope(key), ensure_ascii=False, separators=(",", ":"))[:-1] + ","
    return prefix, prefix + "\uffff"


def load_resolve_authority(db: sqlite3.Connection, repository: str) -> FamilyAuthority | None:
    row = db.execute("SELECT epoch FROM workflow_authority_epochs WHERE repository=? AND family='resolve'",
                     (repository,)).fetchone()
    if row is not None:
        try:
            return FamilyAuthority(WorkflowFamily.RESOLVE, row["epoch"])
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError(f"invalid resolve activation epoch: {exc}") from exc
    prefix = json.dumps((repository,), ensure_ascii=False, separators=(",", ":"))[:-1] + ","
    retained = (
        db.execute("SELECT 1 FROM workflows WHERE repository=? LIMIT 1", (repository,)).fetchone()
        or db.execute("SELECT 1 FROM workflow_rearms WHERE repository=? LIMIT 1", (repository,)).fetchone()
        or db.execute("SELECT 1 FROM workflow_gates WHERE repository=? LIMIT 1", (repository,)).fetchone()
        or db.execute("SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? LIMIT 1",
                      (prefix, prefix + "\uffff")).fetchone()
    )
    if retained is not None:
        raise WorkflowAuthorityError("resolve activation is missing despite retained workflow authority")
    return None


def load_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> WorkflowSnapshot | None:
    """A missing row is absence only after this family's activation is verified."""
    scope = workflow_scope(key)
    activation = load_resolve_authority(db, key.repository)
    if activation is None:
        raise WorkflowAuthorityError("resolve workflow migration is required")
    row = db.execute(
        "SELECT * FROM workflows WHERE repository=? AND target_kind=? AND target_number=?", scope,
    ).fetchone()
    if row is None:
        retained = (
            db.execute("SELECT 1 FROM workflow_rearms WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
            or db.execute("SELECT 1 FROM workflow_gates WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
            or db.execute("SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? LIMIT 1",
                          workflow_run_scope_bounds(key)).fetchone()
        )
        if retained is not None:
            raise WorkflowAuthorityError("workflow is missing despite retained target authority")
        return None
    try:
        family = decode_workflow(row["episode_json"], tuple)
        if len(family) != 2 or not isinstance(family[0], FamilyAuthority) or not isinstance(family[1], ResolveEpisode):
            raise WorkflowEncodingError("invalid resolve family episode")
        authority, episode = family
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        gates = []
        for gate_row in db.execute(
            "SELECT * FROM workflow_gates WHERE repository=? AND target_kind=? AND target_number=? "
            "AND state<>'closed' ORDER BY gate_id", scope,
        ):
            gate = decode_workflow(gate_row["payload_json"], OpenGate | RearmingGate)
            opened = gate.gate if isinstance(gate, RearmingGate) else gate
            expected_state = "rearming" if isinstance(gate, RearmingGate) else "open"
            if gate_row["gate_id"] != opened.gate_id or gate_row["opened_generation"] != opened.opened_generation or gate_row["state"] != expected_state:
                raise WorkflowEncodingError("gate index disagrees with its validated payload")
            gates.append(gate)
        intent_row = db.execute(
            "SELECT * FROM workflow_rearms WHERE repository=? AND target_kind=? AND target_number=? "
            "ORDER BY to_generation DESC LIMIT 1", scope,
        ).fetchone()
        intent = None
        if intent_row is not None:
            intent = decode_workflow(intent_row["intent_json"], PendingRearm | CompletedRearm | SupersededRearm | CancelledRearm)
            states = {PendingRearm: "pending", CompletedRearm: "completed", SupersededRearm: "superseded", CancelledRearm: "cancelled"}
            if intent_row["request_id"] != intent.request_id or intent_row["gate_id"] != intent.gate_id or intent_row["to_generation"] != intent.to_generation or intent_row["state"] != states[type(intent)]:
                raise WorkflowEncodingError("rearm index disagrees with its validated payload")
        snapshot = WorkflowSnapshot(
            WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"]),
            authority, episode, tuple(gates), intent,
        )
        if intent is not None:
            acknowledgement = load_workspace_ack(db, intent.handoff)
            if acknowledgement is not None and acknowledgement.performed_under.fence.generation > snapshot.revision.fence.generation:
                raise WorkflowAuthorityError("workspace acknowledgement belongs to a future execution generation")
            if isinstance(intent, CompletedRearm) and acknowledgement != intent.workspace:
                raise WorkflowAuthorityError("completed rearm lacks its exact durable workspace acknowledgement")
        return snapshot
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowAuthorityError(f"invalid workflow authority: {exc}") from exc


def require_workflow_binding(
    db: sqlite3.Connection, binding: WorkflowBinding, *, allow_gated: bool = False,
) -> WorkflowSnapshot:
    snapshot = load_workflow_row(db, binding.fence.key)
    if snapshot is None:
        raise WorkflowAuthorityError("captured workflow is absent")
    if snapshot.binding != binding:
        raise WorkflowAuthorityError("captured workflow generation, epoch or episode is stale")
    if snapshot.gates and not allow_gated:
        raise WorkflowAuthorityError("workflow has unresolved gates")
    return snapshot


def persist_workflow_snapshot(db: sqlite3.Connection, after: WorkflowSnapshot) -> None:
    """Write an existing workflow row and its unresolved gate index."""
    scope = workflow_scope(after.revision.fence.key)
    changed = db.execute(
        "UPDATE workflows SET generation=?, version=?, episode_json=? "
        "WHERE repository=? AND target_kind=? AND target_number=?",
        (after.revision.fence.generation, after.revision.version,
         encode_workflow((after.authority, after.episode)), *scope),
    ).rowcount
    if changed != 1:
        raise WorkflowAuthorityError("persisted workflow row is absent")
    # Closed causes remain forensic history, never a runtime file fallback.
    db.execute(
        "UPDATE workflow_gates SET state='closed' WHERE repository=? AND target_kind=? AND target_number=?",
        scope,
    )
    for gate in after.gates:
        opened = gate.gate if isinstance(gate, RearmingGate) else gate
        cause_key = hashlib.sha256(encode_workflow(opened.cause).encode()).hexdigest()
        existing = db.execute("SELECT repository, target_kind, target_number FROM workflow_gates WHERE gate_id=?", (opened.gate_id,)).fetchone()
        if existing is not None and tuple(existing) != scope:
            raise WorkflowAuthorityError("gate identity belongs to another workflow")
        db.execute(
            "INSERT INTO workflow_gates VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(gate_id) DO UPDATE SET state=excluded.state, payload_json=excluded.payload_json",
            (opened.gate_id, *scope, opened.opened_generation, cause_key,
             "rearming" if isinstance(gate, RearmingGate) else "open", encode_workflow(gate)),
        )
