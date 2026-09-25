"""Transaction-local workflow reads shared by admission and progression; ADR 0015."""

from __future__ import annotations

import hashlib
import sqlite3
import json
from typing import TypeVar

from ..domain.resolve_workspace import EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared
from ..domain.review_workflow import (
    AnyWorkflowSnapshot, CancelledReviewRearm, CompletedReviewRearm, PendingReviewRearm, ReviewEpisode,
    ReviewRearmIntent, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from ..domain.workflow import (
    CancelledRearm, CompletedRearm, OpenGate, PendingRearm, RearmIntent, RearmingGate,
    ResolveEpisode, SupersededRearm, WorkflowAbsent, WorkflowMigrationRequired, WorkflowSnapshot,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision, target_family,
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


# Phase 3 owns one family per target kind (ADR 0015): the indexed target kind is
# the family discriminator for rows that carry no family column.
_FAMILY_TARGET_KIND = {WorkflowFamily.RESOLVE: "issue", WorkflowFamily.REVIEW_REJECTIONS: "pr"}


def family_target_kind(family: WorkflowFamily) -> str:
    if not isinstance(family, WorkflowFamily):
        raise ValueError("workflow family must be a recognized variant")
    return _FAMILY_TARGET_KIND[family]


def family_run_scope_bounds(repository: str, family: WorkflowFamily) -> tuple[str, str]:
    """Indexed episode-key range of one family's admitted runs in a repository."""
    prefix = json.dumps((repository, family_target_kind(family)), ensure_ascii=False, separators=(",", ":"))[:-1] + ","
    return prefix, prefix + "\uffff"


def load_family_authority(db: sqlite3.Connection, family: WorkflowFamily, repository: str) -> FamilyAuthority | None:
    """One family's activation; another family's retained rows never count as its authority."""
    kind = family_target_kind(family)
    row = db.execute("SELECT epoch FROM workflow_authority_epochs WHERE repository=? AND family=?",
                     (repository, family.value)).fetchone()
    if row is not None:
        try:
            return FamilyAuthority(family, row["epoch"])
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError(f"invalid {family.value} activation epoch: {exc}") from exc
    retained = (
        db.execute("SELECT 1 FROM workflows WHERE repository=? AND target_kind=? LIMIT 1", (repository, kind)).fetchone()
        or db.execute("SELECT 1 FROM workflow_rearms WHERE repository=? AND target_kind=? LIMIT 1", (repository, kind)).fetchone()
        or db.execute("SELECT 1 FROM workflow_gates WHERE repository=? AND target_kind=? LIMIT 1", (repository, kind)).fetchone()
        or db.execute("SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? LIMIT 1",
                      family_run_scope_bounds(repository, family)).fetchone()
    )
    if retained is not None:
        raise WorkflowAuthorityError(f"{family.value} activation is missing despite retained workflow authority")
    return None


def load_resolve_authority(db: sqlite3.Connection, repository: str) -> FamilyAuthority | None:
    return load_family_authority(db, WorkflowFamily.RESOLVE, repository)


def _require_family(key: WorkflowKey, family: WorkflowFamily) -> tuple[str, str, int]:
    if not isinstance(key, WorkflowKey) or target_family(key.target) is not family:
        raise WorkflowAuthorityError(f"{family.value} authority does not own this workflow target")
    return workflow_scope(key)


def _row_or_absence(db: sqlite3.Connection, key: WorkflowKey, family: WorkflowFamily) -> tuple[FamilyAuthority, sqlite3.Row | None]:
    """A missing row is absence only after this family's activation is verified."""
    scope = _require_family(key, family)
    activation = load_family_authority(db, family, key.repository)
    if activation is None:
        raise WorkflowAuthorityError(f"{family.value} workflow migration is required")
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
    return activation, row


def _load_gates(db: sqlite3.Connection, scope: tuple[str, str, int]) -> tuple[OpenGate | RearmingGate, ...]:
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
    return tuple(gates)


def _latest_intent_row(db: sqlite3.Connection, scope: tuple[str, str, int]) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM workflow_rearms WHERE repository=? AND target_kind=? AND target_number=? "
        "ORDER BY to_generation DESC LIMIT 1", scope,
    ).fetchone()


def _check_intent_index(intent_row: sqlite3.Row, intent: RearmIntent | ReviewRearmIntent, states: dict[type, str]) -> None:
    if intent_row["request_id"] != intent.request_id or intent_row["gate_id"] != intent.gate_id or intent_row["to_generation"] != intent.to_generation or intent_row["state"] != states[type(intent)]:
        raise WorkflowEncodingError("rearm index disagrees with its validated payload")


def load_resolve_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> WorkflowSnapshot | None:
    """Resolve authority for an issue; a PR key is never read as resolve."""
    activation, row = _row_or_absence(db, key, WorkflowFamily.RESOLVE)
    if row is None:
        return None
    scope = workflow_scope(key)
    try:
        family = decode_workflow(row["episode_json"], tuple)
        if len(family) != 2 or not isinstance(family[0], FamilyAuthority) or not isinstance(family[1], ResolveEpisode):
            raise WorkflowEncodingError("invalid resolve family episode")
        authority, episode = family
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        gates = _load_gates(db, scope)
        intent_row = _latest_intent_row(db, scope)
        intent = None
        if intent_row is not None:
            intent = decode_workflow(intent_row["intent_json"], PendingRearm | CompletedRearm | SupersededRearm | CancelledRearm)
            _check_intent_index(intent_row, intent, {PendingRearm: "pending", CompletedRearm: "completed", SupersededRearm: "superseded", CancelledRearm: "cancelled"})
        snapshot = WorkflowSnapshot(
            WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"]),
            authority, episode, gates, intent,
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


def load_review_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot | None:
    """Review-rejection authority for a PR; an issue key is never read as review."""
    activation, row = _row_or_absence(db, key, WorkflowFamily.REVIEW_REJECTIONS)
    if row is None:
        return None
    scope = workflow_scope(key)
    try:
        family = decode_workflow(row["episode_json"], tuple)
        if len(family) != 2 or not isinstance(family[0], FamilyAuthority) or not isinstance(family[1], ReviewEpisode):
            raise WorkflowEncodingError("invalid review rejection family episode")
        authority, episode = family
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        gates = _load_gates(db, scope)
        intent_row = _latest_intent_row(db, scope)
        intent = None
        if intent_row is not None:
            intent = decode_workflow(
                intent_row["intent_json"],
                PendingReviewRearm | CompletedReviewRearm | SupersededReviewRearm | CancelledReviewRearm,
            )
            _check_intent_index(intent_row, intent, {
                PendingReviewRearm: "pending", CompletedReviewRearm: "completed",
                SupersededReviewRearm: "superseded", CancelledReviewRearm: "cancelled",
            })
        return ReviewWorkflowSnapshot(
            WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"]),
            authority, episode, gates, intent,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowAuthorityError(f"invalid workflow authority: {exc}") from exc


def load_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> AnyWorkflowSnapshot | None:
    """Dispatch on the target's family; each family decodes only its own rows."""
    if not isinstance(key, WorkflowKey):
        raise WorkflowAuthorityError("workflow read requires a typed workflow key")
    if target_family(key.target) is WorkflowFamily.REVIEW_REJECTIONS:
        return load_review_workflow_row(db, key)
    return load_resolve_workflow_row(db, key)


def load_workflow_read(
    db: sqlite3.Connection, key: WorkflowKey,
) -> AnyWorkflowSnapshot | WorkflowAbsent | WorkflowMigrationRequired:
    """Absence is distinct from a family that was never activated; never file fallback."""
    if not isinstance(key, WorkflowKey):
        raise WorkflowAuthorityError("workflow read requires a typed workflow key")
    family = target_family(key.target)
    if load_family_authority(db, family, key.repository) is None:
        return WorkflowMigrationRequired(key, family)
    snapshot = load_workflow_row(db, key)
    return snapshot if snapshot is not None else WorkflowAbsent(key)


def require_resolve_binding(
    db: sqlite3.Connection, binding: WorkflowBinding, *, allow_gated: bool = False,
) -> WorkflowSnapshot:
    snapshot = load_resolve_workflow_row(db, binding.fence.key)
    return _require_current(snapshot, binding, allow_gated=allow_gated)


def require_review_binding(
    db: sqlite3.Connection, binding: WorkflowBinding, *, allow_gated: bool = False,
) -> ReviewWorkflowSnapshot:
    snapshot = load_review_workflow_row(db, binding.fence.key)
    return _require_current(snapshot, binding, allow_gated=allow_gated)


def require_workflow_binding(
    db: sqlite3.Connection, binding: WorkflowBinding, *, allow_gated: bool = False,
) -> AnyWorkflowSnapshot:
    if binding.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
        return require_review_binding(db, binding, allow_gated=allow_gated)
    return require_resolve_binding(db, binding, allow_gated=allow_gated)


_Snapshot = TypeVar("_Snapshot", WorkflowSnapshot, ReviewWorkflowSnapshot)


def _require_current(snapshot: _Snapshot | None, binding: WorkflowBinding, *, allow_gated: bool) -> _Snapshot:
    if snapshot is None:
        raise WorkflowAuthorityError("captured workflow is absent")
    if snapshot.binding != binding:
        raise WorkflowAuthorityError("captured workflow generation, epoch or episode is stale")
    if snapshot.gates and not allow_gated:
        raise WorkflowAuthorityError("workflow has unresolved gates")
    return snapshot


def persist_workflow_snapshot(db: sqlite3.Connection, after: AnyWorkflowSnapshot) -> None:
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
