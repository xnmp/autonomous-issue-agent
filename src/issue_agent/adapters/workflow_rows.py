"""Transaction-local workflow reads shared by admission and progression; ADR 0015."""

from __future__ import annotations

import hashlib
import sqlite3
import json
from dataclasses import replace
from typing import TypeVar

from ..domain.resolve_workspace import EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared
from ..domain.review_workflow import (
    AnyWorkflowSnapshot, CancelledReviewRearm, CompletedReviewRearm, PendingReviewRearm, ReviewEpisode,
    ReviewFamilyEpisodes, ReviewRearmIntent, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from ..domain.review_execution import (
    MAX_EXECUTION_SUFFIX, ActivatedReviewExecution, ExecutionNotActivated,
    ReviewExecutionCheckpoint, ReviewRoundOutcome, project_review_execution,
)
from ..domain.review_rounds import (
    DeferredReviewPreparation, FinalizedReviewRound, ReviewParentAbandoned,
    ReviewSealedParentAbandoned,
    MAX_REVIEW_STAGES, MAX_ROUND_SUBMISSIONS, ReviewPlanningComplete, ReviewPlanningOpen,
    ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan, ReviewStagePlan,
    ReviewSubmissionRegistration,
    finalize_review_round,
)
from ..domain.review_preflight import ReviewPreflightSelection
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
REVIEW_STAGE_NAMESPACE = "review-round-stage"
REVIEW_SEAL_NAMESPACE = "review-round-seal"
REVIEW_ABANDONMENT_NAMESPACE = "review-round-abandonment"
REVIEW_SUBMISSION_NAMESPACE = "review-round-submission"
REVIEW_SUBMISSION_INDEX_NAMESPACE = "review-round-submission-index"
REVIEW_OUTCOME_NAMESPACE = "review-round-outcome"


def review_submission_receipt_prefix(round_id: str) -> str:
    return hashlib.sha256(encode_workflow(round_id).encode()).hexdigest() + ":"


def review_submission_receipt_key(round_id: str, command_id: str) -> str:
    return review_submission_receipt_prefix(round_id) + hashlib.sha256(encode_workflow(command_id).encode()).hexdigest()


def read_review_submission_registration(
    db: sqlite3.Connection, repository: str, command_id: str,
) -> ReviewSubmissionRegistration | None:
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (REVIEW_SUBMISSION_NAMESPACE, repository, command_id),
    ).fetchone()
    return decode_workflow(row["payload_json"], ReviewSubmissionRegistration) if row is not None else None


def require_review_submission_receipts(
    db: sqlite3.Connection, repository: str, plan: ReviewRoundPlan,
    claim_occurrence_id: str, revision: WorkflowRevision,
) -> None:
    """Check the command and round indices against the immutable plan."""
    prefix = review_submission_receipt_prefix(plan.round.round_id)
    rows = db.execute(
        "SELECT key,payload_json FROM dedupe_keys WHERE namespace=? AND scope=? "
        "AND key>=? AND key<? LIMIT ?",
        (REVIEW_SUBMISSION_INDEX_NAMESPACE, repository, prefix, prefix[:-1] + ";",
         MAX_ROUND_SUBMISSIONS + 1),
    ).fetchall()
    if len(rows) != len(plan.submissions):
        raise WorkflowAuthorityError("review submission receipts disagree with its round plan")
    if not plan.submissions:
        return
    indexed = {row["key"]: row["payload_json"] for row in rows}
    preflight_row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-preflight' "
        "AND scope=? AND key=?", (repository, plan.round.round_id),
    ).fetchone()
    if preflight_row is None:
        raise WorkflowAuthorityError("review submission plan lacks its preflight owner")
    preflight = decode_workflow(preflight_row["payload_json"], tuple)
    if (len(preflight) != 3 or not isinstance(preflight[0], ReviewPreflightSelection)
            or not isinstance(preflight[1], str) or not preflight[1]
            or not isinstance(preflight[2], ReviewRoundPlan)
            or preflight[0].claim_occurrence_id != claim_occurrence_id
            or preflight[0].request_id != plan.round.round_id
            or preflight[0].binding != plan.round.binding
            or replace(preflight[2], stages=plan.stages, submissions=plan.submissions) != plan):
        raise WorkflowAuthorityError("review submission plan disagrees with its preflight")
    for submission in plan.submissions:
        key = review_submission_receipt_key(plan.round.round_id, submission.command_id)
        payload = indexed.get(key)
        if payload is None:
            raise WorkflowAuthorityError("planned review submission lacks its round index")
        registration = decode_workflow(payload, ReviewSubmissionRegistration)
        command_registration = read_review_submission_registration(db, repository, submission.command_id)
        if (command_registration != registration or registration.round != plan.round
                or registration.submission != submission
                or registration.claim_occurrence_id != claim_occurrence_id
                or registration.owner_id != preflight[1]
                or registration.registered_revision.fence != plan.round.binding.rejection.fence
                or (registration.registered_revision.fence == revision.fence
                    and registration.registered_revision.version > revision.version)):
            raise WorkflowAuthorityError("review submission registration disagrees with its round")
        command = db.execute(
            "SELECT repository,target,kind,expected_revision,config_version,executor_identity,"
            "idempotency_key,payload_json,payload_digest,workflow_binding_json FROM lifecycle_commands "
            "WHERE command_id=?", (submission.command_id,),
        ).fetchone()
        if (command is None or command["repository"] != repository
                or command["target"] != f"pr:{plan.round.binding.rejection.fence.key.target.number}"
                or command["kind"] != "submit_review"
                or command["expected_revision"] != plan.head_sha
                or command["config_version"] != registration.config_version
                or command["executor_identity"] != registration.owner_id
                or command["idempotency_key"] != registration.idempotency_key
                or command["payload_digest"] != registration.command_digest
                or command["workflow_binding_json"] != encode_workflow(plan.round.binding.rejection)):
            raise WorkflowAuthorityError("review submission command disagrees with its round receipt")
        try:
            payload_value = json.loads(command["payload_json"])
        except (TypeError, ValueError) as exc:
            raise WorkflowAuthorityError("review submission command payload is unreadable") from exc
        if (not isinstance(payload_value, dict) or payload_value.get("head") != plan.head_sha
                or payload_value.get("pr") != plan.round.binding.rejection.fence.key.target.number
                or hashlib.sha256((command["payload_json"] + "\n" + command["workflow_binding_json"]).encode()).hexdigest()
                   != command["payload_digest"]):
            raise WorkflowAuthorityError("review submission command payload disagrees with its round")


def review_parent_abandonment(
    db: sqlite3.Connection, repository: str, owner_id: str, claim_occurrence_id: str,
    claim_target: int, plan: ReviewRoundPlan,
) -> ReviewParentAbandoned:
    """Derive a fence from the exact terminated parent claim occurrence."""
    if db.execute(
        "SELECT 1 FROM dispatch_claims WHERE repository=? AND role='review' AND occurrence_id=?",
        (repository, claim_occurrence_id),
    ).fetchone() is not None:
        raise WorkflowAuthorityError("review parent claim is still present")
    receipts = {
        event: db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' "
            "AND scope=? AND key=?",
            (repository, f"{claim_occurrence_id}:{event}"),
        ).fetchone()
        for event in ("created", "released", "reclaimed")
    }
    terminal = tuple(event for event in ("released", "reclaimed") if receipts[event] is not None)
    if receipts["created"] is None or len(terminal) != 1:
        raise WorkflowAuthorityError("review parent lacks one exact creation and terminal claim receipt")
    try:
        created = json.loads(receipts["created"]["payload_json"])
        ended = json.loads(receipts[terminal[0]]["payload_json"])
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("review parent claim lifecycle receipt is unreadable") from exc
    target = f"issue:{claim_target}" if claim_target > 0 else f"pr:{-claim_target}"
    if (not isinstance(created, dict) or not isinstance(ended, dict)
            or set(created) != {"role", "target", "leg", "owner_id", "owner_pid",
                                "owner_start_ticks", "claimed_at", "source"}
            or created["role"] != "review" or created["target"] != target
            or created["leg"] not in ("REVIEW", "RE_REVIEW")
            or created["owner_id"] != owner_id
            or type(created["owner_pid"]) is not int or created["owner_pid"] <= 0
            or type(created["owner_start_ticks"]) is not int or created["owner_start_ticks"] <= 0
            or not isinstance(created["claimed_at"], str) or not created["claimed_at"]
            or created["source"] not in ("admission", "v24-migration")
            or ended != {key: value for key, value in created.items() if key != "source"}):
        raise WorkflowAuthorityError("review parent terminal receipt disagrees with its creation")
    payload = encode_workflow((
        plan.round.round_id, owner_id, claim_occurrence_id, terminal[0],
        json.dumps(created, sort_keys=True, separators=(",", ":")),
        json.dumps(ended, sort_keys=True, separators=(",", ":")), plan,
    ))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return ReviewParentAbandoned(
        f"dispatch-claim-lifecycle:{claim_occurrence_id}:{terminal[0]}",
        f"review-round-abandonment:{digest}",
    )


def review_sealed_parent_abandonment(
    db: sqlite3.Connection, repository: str, owner_id: str, claim_occurrence_id: str,
    claim_target: int, plan: ReviewRoundPlan, seal: ReviewPlanningSealed,
) -> ReviewSealedParentAbandoned:
    """Retain the completed seal while fencing a departed parent's unadmitted children."""
    if not isinstance(seal, ReviewPlanningSealed):
        raise ValueError("post-seal abandonment requires completed planning")
    verified_review_completed_seal(db, repository, owner_id, claim_occurrence_id, plan, seal)
    parent = review_parent_abandonment(
        db, repository, owner_id, claim_occurrence_id, claim_target, plan,
    )
    digest = hashlib.sha256(encode_workflow((parent.admission_fence_receipt, seal)).encode()).hexdigest()
    return ReviewSealedParentAbandoned(
        parent.abandonment_receipt, f"review-round-abandonment:{digest}", seal,
    )


def verified_review_parent_abandonment(
    db: sqlite3.Connection, repository: str, owner_id: str, claim_occurrence_id: str,
    claim_target: int, plan: ReviewRoundPlan,
    planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
) -> ReviewParentAbandoned | None:
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (REVIEW_ABANDONMENT_NAMESPACE, repository, plan.round.round_id),
    ).fetchone()
    if not isinstance(planning, ReviewParentAbandoned):
        if row is not None:
            raise WorkflowAuthorityError("review planning contradicts its abandonment fence")
        return None
    if row is None:
        raise WorkflowAuthorityError("abandoned review planning lacks its immutable fence")
    expected = (
        review_sealed_parent_abandonment(
            db, repository, owner_id, claim_occurrence_id, claim_target, plan,
            planning.completed_seal,
        )
        if isinstance(planning, ReviewSealedParentAbandoned)
        else review_parent_abandonment(
            db, repository, owner_id, claim_occurrence_id, claim_target, plan,
        )
    )
    recorded = decode_workflow(row["payload_json"], tuple)
    if (planning != expected
            or recorded != (plan.round.round_id, owner_id, claim_occurrence_id, plan, expected)):
        raise WorkflowAuthorityError("abandoned review planning disagrees with its claim fence")
    return planning


def verified_review_completed_seal(
    db: sqlite3.Connection, repository: str, owner_id: str, claim_occurrence_id: str,
    plan: ReviewRoundPlan, planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
) -> WorkflowRevision | None:
    """Match a completed parent decision to its immutable round receipt."""
    if isinstance(planning, ReviewSealedParentAbandoned):
        planning = planning.completed_seal
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (REVIEW_SEAL_NAMESPACE, repository, plan.round.round_id),
    ).fetchone()
    if isinstance(planning, ReviewPlanningOpen):
        if row is not None:
            raise WorkflowAuthorityError("open review planning has a contradictory seal receipt")
        return None
    if not isinstance(planning, ReviewPlanningSealed) or not isinstance(planning.conclusion, ReviewPlanningComplete):
        if row is not None:
            raise WorkflowAuthorityError("review planning seal receipt contradicts its branch")
        return None
    if row is None:
        raise WorkflowAuthorityError("review planning seal lacks its immutable receipt")
    recorded = decode_workflow(row["payload_json"], tuple)
    if (len(recorded) != 6 or not isinstance(recorded[3], WorkflowRevision)
            or recorded != (plan.round.round_id, owner_id, claim_occurrence_id,
                            recorded[3], plan, planning)
            or recorded[3].fence != plan.round.binding.rejection.fence):
        raise WorkflowAuthorityError("review planning seal disagrees with its round and parent")
    payload = encode_workflow((plan.round.round_id, owner_id, claim_occurrence_id,
                               recorded[3], plan, ReviewPlanningComplete()))
    if planning.seal_receipt != "review-round-seal:" + hashlib.sha256(payload.encode()).hexdigest():
        raise WorkflowAuthorityError("review planning seal identity disagrees with its facts")
    return recorded[3]


def review_stage_receipt_prefix(round_id: str) -> str:
    return hashlib.sha256(encode_workflow(round_id).encode()).hexdigest() + ":"


def review_stage_receipt_key(round_id: str, stage_id: str) -> str:
    return review_stage_receipt_prefix(round_id) + hashlib.sha256(encode_workflow(stage_id).encode()).hexdigest()


def require_review_stage_receipts(db: sqlite3.Connection, repository: str, plan: ReviewRoundPlan) -> None:
    """The bounded append-only stage list and its immutable receipts are one authority."""
    round_id = plan.round.round_id
    prefix = review_stage_receipt_prefix(round_id)
    receipts = db.execute(
        "SELECT key,payload_json FROM dedupe_keys WHERE namespace=? AND scope=? "
        "AND key>=? AND key<? LIMIT ?",
        (REVIEW_STAGE_NAMESPACE, repository, prefix, prefix[:-1] + ";", MAX_REVIEW_STAGES + 1),
    ).fetchall()
    if len(receipts) != len(plan.stages):
        raise WorkflowAuthorityError("review round stage receipts disagree with its canonical plan")
    planned = {stage.stage_id: stage for stage in plan.stages}
    for receipt in receipts:
        recorded = decode_workflow(receipt["payload_json"], tuple)
        if (len(recorded) != 2 or recorded[0] != round_id
                or not isinstance(recorded[1], ReviewStagePlan)
                or receipt["key"] != review_stage_receipt_key(round_id, recorded[1].stage_id)
                or planned.get(recorded[1].stage_id) != recorded[1]):
            raise WorkflowAuthorityError("review round stage receipt conflicts with its canonical plan")


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


def _require_component_index(
    db: sqlite3.Connection, scope: tuple[str, str, int], authority: FamilyAuthority, episode_id: str,
) -> None:
    row = db.execute(
        "SELECT epoch,episode_id FROM workflow_components WHERE repository=? AND target_kind=? "
        "AND target_number=? AND family=?", (*scope, authority.family.value),
    ).fetchone()
    if row is None or row["epoch"] != authority.epoch or row["episode_id"] != episode_id:
        raise WorkflowAuthorityError("workflow component index disagrees with canonical authority")


def insert_workflow_component(
    db: sqlite3.Connection, key: WorkflowKey, authority: FamilyAuthority, episode_id: str,
) -> None:
    db.execute(
        "INSERT INTO workflow_components(repository,target_kind,target_number,family,epoch,episode_id) "
        "VALUES (?,?,?,?,?,?)",
        (*workflow_scope(key), authority.family.value, authority.epoch, episode_id),
    )


def encode_workflow_episodes(snapshot: AnyWorkflowSnapshot) -> str:
    if isinstance(snapshot, ReviewWorkflowSnapshot):
        return encode_workflow(ReviewFamilyEpisodes(snapshot.authority, snapshot.episode, snapshot.execution))
    return encode_workflow((snapshot.authority, snapshot.episode))


def workflow_run_scope_bounds(key: WorkflowKey) -> tuple[str, str]:
    """Indexed target scope across retained epochs/episodes, never JSON payload scans."""
    prefix = json.dumps(workflow_scope(key), ensure_ascii=False, separators=(",", ":"))[:-1] + ","
    return prefix, prefix + "\uffff"


# Phase 3 owns one family per target kind (ADR 0015): the indexed target kind is
# the family discriminator for rows that carry no family column.
_FAMILY_TARGET_KIND = {
    WorkflowFamily.RESOLVE: "issue",
    WorkflowFamily.REVIEW_REJECTIONS: "pr",
    WorkflowFamily.REVIEW_EXECUTION: "pr",
}


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
    if family is WorkflowFamily.REVIEW_EXECUTION:
        retained = (
            db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND family=? LIMIT 1", (repository, family.value)).fetchone()
            or db.execute("SELECT 1 FROM review_execution_rounds WHERE repository=? LIMIT 1", (repository,)).fetchone()
            or db.execute("SELECT 1 FROM workflow_activation_imports WHERE repository=? AND family=? LIMIT 1", (repository, family.value)).fetchone()
        )
    else:
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


def active_families(db: sqlite3.Connection, repository: str) -> tuple[WorkflowFamily, ...]:
    """Every family activated for this repository; the legacy unblock is retired while any is (ADR 0015)."""
    return tuple(family for family in WorkflowFamily if load_family_authority(db, family, repository) is not None)


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
            or db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
            or db.execute("SELECT 1 FROM review_execution_rounds WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
            or db.execute("SELECT 1 FROM workflow_activation_imports WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
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


def _check_execution_projection(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution,
    revision: WorkflowRevision,
) -> None:
    episode_scope = (scope[0], scope[2], execution.binding.execution_epoch, execution.binding.execution_episode)
    count = db.execute(
        "SELECT COUNT(*) AS total FROM review_execution_rounds WHERE repository=? AND target_number=? "
        "AND execution_epoch=? AND execution_episode=?", episode_scope,
    ).fetchone()["total"]
    if count != execution.last_sequence:
        raise WorkflowAuthorityError("review execution rounds disagree with allocated sequence")
    if execution.checkpoint.through_sequence:
        if execution.checkpoint_receipt is None:
            raise WorkflowAuthorityError("review execution checkpoint lacks its compaction receipt")
        compacted = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-checkpoint' "
            "AND scope=? AND key=?", (scope[0], execution.checkpoint_receipt),
        ).fetchone()
        if compacted is None or decode_workflow(compacted["payload_json"], ReviewExecutionCheckpoint) != execution.checkpoint:
            raise WorkflowAuthorityError("review execution checkpoint disagrees with its compaction receipt")
        prefix_scope = (*episode_scope, execution.checkpoint.through_sequence)
        if db.execute(
            "SELECT 1 FROM review_execution_rounds WHERE repository=? AND target_number=? "
            "AND execution_epoch=? AND execution_episode=? AND sequence<=? "
            "AND outcome_json IS NULL LIMIT 1", prefix_scope,
        ).fetchone() is not None or db.execute(
            "SELECT 1 FROM review_execution_rounds WHERE repository=? AND target_number=? "
            "AND execution_epoch=? AND execution_episode=? AND sequence<=? "
            "AND closure_json IS NULL LIMIT 1", prefix_scope,
        ).fetchone() is not None:
            raise WorkflowAuthorityError("execution checkpoint includes an unfinalized review round")
    suffix_count = count - execution.checkpoint.through_sequence
    if suffix_count > MAX_EXECUTION_SUFFIX:
        raise WorkflowAuthorityError("review execution suffix exceeds its bounded projection")
    outcomes: list[ReviewRoundOutcome] = []
    rows = db.execute(
        "SELECT round_id,generation,sequence,claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json "
        "FROM review_execution_rounds WHERE repository=? AND target_number=? AND execution_epoch=? "
        "AND execution_episode=? AND sequence>? ORDER BY sequence",
        (*episode_scope, execution.checkpoint.through_sequence),
    )
    expected_sequence = execution.checkpoint.through_sequence + 1
    for row in rows:
        plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
        require_review_stage_receipts(db, scope[0], plan)
        require_review_submission_receipts(db, scope[0], plan, row["claim_occurrence_id"], revision)
        planning = decode_workflow(
            row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
        )
        identity = plan.round
        if (
            row["sequence"] != expected_sequence
            or row["round_id"] != identity.round_id
            or row["sequence"] != identity.sequence
            or row["generation"] != identity.binding.rejection.fence.generation
            or row["generation"] > execution.binding.rejection.fence.generation
            or identity.binding.execution_epoch != execution.binding.execution_epoch
            or identity.binding.execution_episode != execution.binding.execution_episode
            or identity.binding.rejection.authority != execution.binding.rejection.authority
            or identity.binding.rejection.episode_id != execution.binding.rejection.episode_id
            or identity.binding.rejection.fence.key != execution.binding.rejection.fence.key
            or not isinstance(row["claim_occurrence_id"], str)
            or not row["claim_occurrence_id"]
        ):
            raise WorkflowAuthorityError("review execution round index disagrees with its plan")
        seal_row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SEAL_NAMESPACE, scope[0], identity.round_id),
        ).fetchone()
        completed_planning = (
            planning.completed_seal if isinstance(planning, ReviewSealedParentAbandoned) else planning
        )
        if isinstance(completed_planning, ReviewPlanningSealed) and isinstance(completed_planning.conclusion, ReviewPlanningComplete):
            if seal_row is None:
                raise WorkflowAuthorityError("sealed review round lacks its immutable planning receipt")
            seal_record = decode_workflow(seal_row["payload_json"], tuple)
            if len(seal_record) != 6 or not isinstance(seal_record[1], str) or not seal_record[1]:
                raise WorkflowAuthorityError("sealed review round has malformed parent provenance")
            captured = verified_review_completed_seal(
                db, scope[0], seal_record[1], row["claim_occurrence_id"], plan, planning,
            )
            if (captured is None or captured.fence.generation > revision.fence.generation
                    or (captured.fence == revision.fence and captured.version > revision.version)):
                raise WorkflowAuthorityError("review planning seal revision is not applicable")
        elif seal_row is not None:
            raise WorkflowAuthorityError("review planning state contradicts its completed seal receipt")
        if not isinstance(planning, ReviewParentAbandoned) and db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_ABANDONMENT_NAMESPACE, scope[0], identity.round_id),
        ).fetchone() is not None:
            raise WorkflowAuthorityError("review planning has a contradictory abandonment fence")
        preflight = None
        if isinstance(planning, ReviewParentAbandoned) or row["outcome_json"] is not None:
            preflight_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-preflight' "
                "AND scope=? AND key=?", (scope[0], identity.round_id),
            ).fetchone()
            if preflight_row is None:
                raise WorkflowAuthorityError("finalized review round lacks its exact preflight")
            preflight = decode_workflow(preflight_row["payload_json"], tuple)
            if (len(preflight) != 3 or not isinstance(preflight[0], ReviewPreflightSelection)
                    or not isinstance(preflight[1], str) or not preflight[1]
                    or not isinstance(preflight[2], ReviewRoundPlan)
                    or preflight[0].request_id != identity.round_id
                    or preflight[0].claim_occurrence_id != row["claim_occurrence_id"]
                    or replace(preflight[2], stages=plan.stages, submissions=plan.submissions) != plan):
                raise WorkflowAuthorityError("finalized review round preflight disagrees with its plan")
            if isinstance(planning, ReviewParentAbandoned):
                verified_review_parent_abandonment(
                    db, scope[0], preflight[1], row["claim_occurrence_id"],
                    preflight[0].claim_target, plan, planning,
                )
        expected_sequence += 1
        if row["outcome_json"] is None:
            if row["closure_json"] is not None:
                raise WorkflowAuthorityError("closed review round is missing its outcome")
            continue
        if row["closure_json"] is None:
            raise WorkflowAuthorityError("review round outcome lacks closure facts")
        facts = decode_workflow(row["closure_json"], ReviewRoundFacts)
        result = finalize_review_round(facts)
        if not isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation)):
            raise WorkflowAuthorityError("review round closure has not finalized")
        outcome = decode_workflow(row["outcome_json"], ReviewRoundOutcome)
        if facts.plan != plan or facts.planning != planning or outcome != result.outcome:
            raise WorkflowAuthorityError("review round outcome disagrees with verified closure facts")
        if isinstance(planning, (ReviewParentAbandoned, ReviewPlanningSealed)):
            # Both nonproductive writers commit the same source-backed receipt.
            from .review_round_sources import read_review_round_sources

            assert preflight is not None
            source = read_review_round_sources(
                db, scope[0], preflight[1], preflight[0], plan, planning, revision,
            )
            receipt = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_OUTCOME_NAMESPACE, scope[0], identity.round_id),
            ).fetchone()
            if source != facts or receipt is None or decode_workflow(
                receipt["payload_json"], tuple,
            ) != (facts, outcome):
                raise WorkflowAuthorityError("review outcome disagrees with durable source receipt")
        outcomes.append(outcome)
    if expected_sequence != execution.last_sequence + 1:
        raise WorkflowAuthorityError("review execution round suffix has a gap")
    projected = project_review_execution(execution.checkpoint, tuple(outcomes))
    if projected.state != execution.state:
        raise WorkflowAuthorityError("review execution state disagrees with ordered outcomes")


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
        _require_component_index(db, scope, authority, episode.episode_id)
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
        family = decode_workflow(row["episode_json"], tuple | ReviewFamilyEpisodes)
        execution = ExecutionNotActivated()
        if isinstance(family, ReviewFamilyEpisodes):
            authority, episode, execution = family.authority, family.episode, family.execution
        elif len(family) == 2 and isinstance(family[0], FamilyAuthority) and isinstance(family[1], ReviewEpisode):
            authority, episode = family
        else:
            raise WorkflowEncodingError("invalid review rejection family episode")
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        _require_component_index(db, scope, authority, episode.episode_id)
        execution_authority = load_family_authority(db, WorkflowFamily.REVIEW_EXECUTION, key.repository)
        if isinstance(execution, ActivatedReviewExecution):
            if execution_authority is None or execution.binding.execution_epoch != execution_authority.epoch:
                raise WorkflowEncodingError("execution activation epoch disagrees with its component")
            _require_component_index(
                db, scope, execution_authority, execution.binding.execution_episode,
            )
        elif execution_authority is not None:
            raise WorkflowEncodingError("activated review execution component is absent")
        elif db.execute(
            "SELECT 1 FROM workflow_components WHERE repository=? AND target_kind=? AND target_number=? "
            "AND family=?", (*scope, WorkflowFamily.REVIEW_EXECUTION.value),
        ).fetchone() is not None:
            raise WorkflowEncodingError("inactive review has an execution component index")
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
        snapshot = ReviewWorkflowSnapshot(
            WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"]),
            authority, episode, gates, intent, execution,
        )
        if isinstance(execution, ActivatedReviewExecution):
            _check_execution_projection(db, scope, execution, snapshot.revision)
        return snapshot
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


def linked_issue_workflow_holds(db: sqlite3.Connection, key: WorkflowKey) -> tuple[str, ...]:
    """Join issue gates without initializing an inactive or unobserved family."""
    _require_family(key, WorkflowFamily.RESOLVE)
    authority = load_family_authority(db, WorkflowFamily.RESOLVE, key.repository)
    snapshot = load_resolve_workflow_row(db, key) if authority is not None else None
    if snapshot is None:
        return ()
    holds = ("unresolved workflow gates",) if snapshot.gates else ()
    if isinstance(snapshot.intent, PendingRearm):
        holds += ("pending workflow re-arm",)
    return holds


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
    if isinstance(after, ReviewWorkflowSnapshot) and isinstance(after.execution, ActivatedReviewExecution):
        _require_component_index(
            db, scope, FamilyAuthority(WorkflowFamily.REVIEW_EXECUTION, after.execution.binding.execution_epoch),
            after.execution.binding.execution_episode,
        )
    changed = db.execute(
        "UPDATE workflows SET generation=?, version=?, episode_json=? "
        "WHERE repository=? AND target_kind=? AND target_number=?",
        (after.revision.fence.generation, after.revision.version,
         encode_workflow_episodes(after), *scope),
    ).rowcount
    if changed != 1:
        raise WorkflowAuthorityError("persisted workflow row is absent")
    component = db.execute(
        "UPDATE workflow_components SET epoch=?,episode_id=? WHERE repository=? AND target_kind=? "
        "AND target_number=? AND family=?",
        (after.authority.epoch, after.episode.episode_id, *scope, after.authority.family.value),
    ).rowcount
    if component != 1:
        raise WorkflowAuthorityError("workflow component index is absent")
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
