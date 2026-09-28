"""Transaction-local workflow reads shared by admission and progression; ADR 0015."""

from __future__ import annotations

import hashlib
import sqlite3
import json
from dataclasses import replace
from datetime import datetime
from typing import TypeVar

from ..domain.resolve_workspace import EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared
from ..domain.review_workflow import (
    AnyWorkflowSnapshot, CancelledReviewRearm, CompleteReviews, CompletedReviewRearm, PendingReviewRearm, PullRequestState,
    RecordedPullRequest, ReviewEpisode,
    ReviewFamilyEpisodes, ReviewRearmIntent, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from ..domain.review_execution import (
    MAX_EXECUTION_SUFFIX, ActivatedReviewExecution, CopiedReviewProgress, ExecutionNotActivated, PaidReviewProgress,
    ImportedReviewExecutionDecision, ImportedReviewExecutionOriginReceipt,
    InitializedReviewExecutionOriginReceipt, initialized_review_execution,
    ReviewExecutionAcceptedTransitionSource, ReviewExecutionTransitionProofSource,
    ReviewExecutionCheckpoint, ReviewExecutionCheckpointReceipt, ReviewExecutionGenerationBoundary,
    ReviewExecutionOriginKind, ReviewRoundOutcome,
    matches_accepted_review_transition_source, replay_review_execution_generations,
    review_generation_chain_digest, same_execution_episode,
)
from ..domain.review_rounds import (
    DeferredReviewPreparation, FinalizedReviewRound, ReviewParentAbandoned,
    ReviewSealedParentAbandoned,
    MAX_REVIEW_STAGES, MAX_ROUND_SUBMISSIONS, ReviewPlanningComplete, ReviewPlanningOpen,
    ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan, ReviewStagePlan,
    ReviewSubmissionConfirmed, ReviewSubmissionRegistration,
    StaleReviewProductivitySettlement, finalize_review_round,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.review_workflow_policy import (
    OBSERVATION_MAX_AGE_SECONDS, CeilingProductivityDecision, initial_review_workflow, open_review_ceiling,
)
from ..domain.workflow import (
    AuthorizedOperatorCommand, CancelledRearm, CompletedRearm, ContinueGrantedRearm, OpenGate,
    ObservedTransition, PendingRearm, RequestRearm,
    RearmIntent, RearmingGate,
    ResolveEpisode, SupersededRearm, WorkflowAbsent, WorkflowChanged, WorkflowMigrationRequired,
    WorkflowMutation, WorkflowRejected, WorkflowSnapshot, WorkflowUnchanged,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision, target_family,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow


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
REVIEW_PREPARATION_NAMESPACE = "review-round-preparation"
REVIEW_STALE_PRODUCTIVITY_NAMESPACE = "review-round-stale-productivity"
REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE = "review-round-superseded-productivity"
REVIEW_CEILING_PRODUCTIVITY_NAMESPACE = "review-round-ceiling-productivity"
REVIEW_EXECUTION_ORIGIN_NAMESPACE = "review-execution-origin"
REVIEW_EXECUTION_CHECKPOINT_NAMESPACE = "review-execution-checkpoint"
RECORDED_REVIEW_NAMESPACE = "review-recorded-submission"


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


def insert_imported_review_execution_origin(
    db: sqlite3.Connection, repository: str, receipt: ImportedReviewExecutionOriginReceipt,
    *, claimed_at: datetime,
) -> None:
    """Publish an imported origin in the transaction that publishes its component."""
    if (not isinstance(receipt, ImportedReviewExecutionOriginReceipt)
            or receipt.checkpoint.state.binding.rejection.fence.key.repository != repository
            or not isinstance(claimed_at, datetime) or claimed_at.tzinfo is None
            or claimed_at.utcoffset() is None):
        raise ValueError("imported origin requires matching typed activation evidence")
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
        (REVIEW_EXECUTION_ORIGIN_NAMESPACE, repository, receipt.origin.evidence_id,
         claimed_at.isoformat(), encode_workflow(receipt)),
    )


def insert_initialized_review_execution_origin(
    db: sqlite3.Connection, repository: str, receipt: InitializedReviewExecutionOriginReceipt,
    *, claimed_at: datetime,
) -> None:
    """Publish first-observation ancestry with the newly created component."""
    if (not isinstance(receipt, InitializedReviewExecutionOriginReceipt)
            or receipt.checkpoint.state.binding.rejection.fence.key.repository != repository
            or not isinstance(claimed_at, datetime) or claimed_at.tzinfo is None
            or claimed_at.utcoffset() is None):
        raise ValueError("initialized origin requires matching typed observation evidence")
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
        (REVIEW_EXECUTION_ORIGIN_NAMESPACE, repository, receipt.origin.evidence_id,
         claimed_at.isoformat(), encode_workflow(receipt)),
    )


def _unique_text_identity_rowid(
    db: sqlite3.Connection, table: str, text_fields: tuple[tuple[str, str], ...],
    integer_fields: tuple[tuple[str, int], ...] = (),
    *, source: str = "review execution origin",
) -> int:
    predicates = [f"{name} IN (?,CAST(? AS BLOB))" for name, _ in text_fields]
    predicates.extend(f"{name} IN (?,CAST(? AS BLOB))" for name, _ in integer_fields)
    columns = ",".join(f"typeof({name}) AS {name}_type" for name, _ in (*text_fields, *integer_fields))
    params = (*tuple(value for _, value in text_fields for _ in range(2)),
              *tuple(value for _, value in integer_fields for _ in range(2)))
    rows = db.execute(
        f"SELECT rowid,{columns} FROM {table} WHERE {' AND '.join(predicates)} LIMIT 2",
        params,
    ).fetchall()
    if (len(rows) != 1
            or any(rows[0][f"{name}_type"] != "text" for name, _ in text_fields)
            or any(rows[0][f"{name}_type"] != "integer" for name, _ in integer_fields)):
        raise WorkflowAuthorityError(f"{source} {table} source is absent or aliased")
    return rows[0]["rowid"]


def _read_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution,
) -> tuple[ImportedReviewExecutionOriginReceipt | InitializedReviewExecutionOriginReceipt, datetime]:
    origin_id = execution.origin.evidence_id
    origin_rowid = _unique_text_identity_rowid(
        db, "dedupe_keys", (("namespace", REVIEW_EXECUTION_ORIGIN_NAMESPACE),
                            ("scope", scope[0]), ("key", origin_id)),
    )
    metadata = db.execute(
        "SELECT rowid,max(ifnull(length(CAST(claimed_at AS BLOB)),0),"
        "ifnull(length(CAST(payload_json AS BLOB)),0)) AS largest "
        "FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (REVIEW_EXECUTION_ORIGIN_NAMESPACE, scope[0], origin_id),
    ).fetchone()
    if (metadata is None or metadata["rowid"] != origin_rowid
            or metadata["largest"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("review execution origin source exceeds its bound")
    row = db.execute(
        "SELECT rowid,claimed_at,reusable_after,payload_json,"
        "typeof(claimed_at) AS claimed_type,typeof(payload_json) AS payload_type "
        "FROM dedupe_keys WHERE rowid=? AND namespace=? AND scope=? AND key=?",
        (metadata["rowid"], REVIEW_EXECUTION_ORIGIN_NAMESPACE, scope[0], origin_id),
    ).fetchone()
    if (row is None or row["rowid"] != origin_rowid
            or row["reusable_after"] is not None or row["claimed_type"] != "text"
            or row["payload_type"] != "text"):
        raise WorkflowAuthorityError("review execution origin source changed or is malformed")
    try:
        claimed = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
        if claimed.tzinfo is None or claimed.utcoffset() is None:
            raise ValueError("origin claim has no timezone")
        receipt_type = (ImportedReviewExecutionOriginReceipt
                        if execution.origin.kind is ReviewExecutionOriginKind.IMPORTED
                        else InitializedReviewExecutionOriginReceipt
                        if execution.origin.kind is ReviewExecutionOriginKind.INITIALIZED else None)
        if receipt_type is None:
            raise ValueError("unsupported execution origin cause")
        captured = decode_workflow(row["payload_json"], receipt_type)
        if encode_workflow(captured) != row["payload_json"]:
            raise ValueError("origin receipt is not canonical")
    except (TypeError, ValueError, UnicodeError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("review execution origin receipt is unreadable") from exc
    origin_binding = captured.checkpoint.state.binding
    if (captured.origin != execution.origin
            or not same_execution_episode(origin_binding, execution.binding)
            or origin_binding.execution_epoch != execution.binding.execution_epoch
            or captured.checkpoint.policy != execution.checkpoint.policy
            or (execution.checkpoint.through_sequence == 0
                and execution.checkpoint.boundary_cursor is None
                and execution.checkpoint != captured.checkpoint)):
        raise WorkflowAuthorityError("review execution origin disagrees with its component")
    return captured, claimed


def review_execution_checkpoint_receipt_id(receipt: ReviewExecutionCheckpointReceipt) -> str:
    if not isinstance(receipt, ReviewExecutionCheckpointReceipt):
        raise ValueError("checkpoint identity requires a typed receipt")
    return "review-execution-checkpoint:" + hashlib.sha256(encode_workflow(receipt).encode()).hexdigest()


def _read_checkpoint_receipt_row(
    db: sqlite3.Connection, repository: str, receipt_id: str,
) -> tuple[ReviewExecutionCheckpointReceipt, datetime]:
    rowid = _unique_text_identity_rowid(
        db, "dedupe_keys", (("namespace", REVIEW_EXECUTION_CHECKPOINT_NAMESPACE),
                            ("scope", repository), ("key", receipt_id)),
        source="review execution checkpoint",
    )
    metadata = db.execute(
        "SELECT rowid,max(ifnull(length(CAST(claimed_at AS BLOB)),0),"
        "ifnull(length(CAST(payload_json AS BLOB)),0)) AS largest "
        "FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, repository, receipt_id),
    ).fetchone()
    if (metadata is None or metadata["rowid"] != rowid
            or metadata["largest"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("review execution checkpoint receipt exceeds its bound")
    row = db.execute(
        "SELECT rowid,claimed_at,reusable_after,payload_json,"
        "typeof(claimed_at) AS claimed_type,typeof(payload_json) AS payload_type "
        "FROM dedupe_keys WHERE rowid=? AND namespace=? AND scope=? AND key=?",
        (rowid, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, repository, receipt_id),
    ).fetchone()
    if (row is None or row["reusable_after"] is not None or row["claimed_type"] != "text"
            or row["payload_type"] != "text"):
        raise WorkflowAuthorityError("review execution checkpoint receipt changed or is malformed")
    try:
        claimed = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
        if claimed.tzinfo is None or claimed.utcoffset() is None:
            raise ValueError("checkpoint claim has no timezone")
        receipt = decode_workflow(row["payload_json"], ReviewExecutionCheckpointReceipt)
        if (encode_workflow(receipt) != row["payload_json"]
                or review_execution_checkpoint_receipt_id(receipt) != receipt_id):
            raise ValueError("checkpoint receipt identity is not canonical")
    except (TypeError, ValueError, UnicodeError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("review execution checkpoint receipt is unreadable") from exc
    return receipt, claimed


def _check_execution_checkpoint_receipt(
    db: sqlite3.Connection, repository: str, execution: ActivatedReviewExecution,
    origin: ImportedReviewExecutionOriginReceipt | InitializedReviewExecutionOriginReceipt,
) -> None:
    receipt_id = execution.checkpoint_receipt
    if receipt_id is None:
        raise WorkflowAuthorityError("review execution checkpoint lacks its compaction receipt")
    receipt, claimed = _read_checkpoint_receipt_row(db, repository, receipt_id)
    if receipt.origin != origin.origin or receipt.checkpoint != execution.checkpoint:
        raise WorkflowAuthorityError("review execution checkpoint disagrees with its origin or component")
    if receipt.previous_receipt_id is None:
        previous = origin.checkpoint
    else:
        predecessor, previous_claimed = _read_checkpoint_receipt_row(
            db, repository, receipt.previous_receipt_id,
        )
        if predecessor.origin != origin.origin or previous_claimed > claimed:
            raise WorkflowAuthorityError("review execution checkpoint has a foreign predecessor")
        previous = predecessor.checkpoint
    if receipt.previous_checkpoint != previous:
        raise WorkflowAuthorityError("review execution checkpoint has a changed predecessor")
    if receipt.last_round_source is not None:
        from .review_execution_activation import read_checkpoint_round_row_source

        round_source = read_checkpoint_round_row_source(db, receipt.last_round_source.round)
        if round_source.source_digest != receipt.last_round_source.row_sha256:
            raise WorkflowAuthorityError("review execution checkpoint round row changed after compaction")
    if receipt.last_generation_source is not None:
        from .review_execution_activation import verify_checkpoint_generation_anchor

        verify_checkpoint_generation_anchor(
            db, receipt.previous_checkpoint.state.binding, receipt.last_generation_source,
        )


def _check_initialized_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution,
    captured: InitializedReviewExecutionOriginReceipt, claimed: datetime,
) -> None:
    key = WorkflowKey(scope[0], PullRequestRef(scope[2]))
    rejection_authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, scope[0])
    if rejection_authority is None:
        raise WorkflowAuthorityError("initialized execution origin lost rejection authority")
    initial = initial_review_workflow(key, rejection_authority)
    expected, receipt = initialized_review_execution(
        initial.binding, execution.binding.execution_epoch, captured.observation_evidence_id,
        captured.activation_plan_digest, captured.source_digest, captured.inventory_digest,
        captured.activated_at,
    )
    if captured != receipt or not same_execution_episode(expected.binding, execution.binding):
        raise WorkflowAuthorityError("initialized execution origin disagrees with first observation")
    family = WorkflowFamily.REVIEW_EXECUTION.value
    activation_rowid = _unique_text_identity_rowid(
        db, "workflow_authority_epochs", (("repository", scope[0]), ("family", family)),
    )
    evidence_rowid = _unique_text_identity_rowid(
        db, "workflow_activation_evidence", (("repository", scope[0]), ("family", family)),
        (("epoch", execution.binding.execution_epoch),),
    )
    activation = db.execute(
        "SELECT epoch,activated_at,source_digest,inventory_digest FROM workflow_authority_epochs "
        "WHERE rowid=? AND repository=? AND family=? "
        "AND length(CAST(activated_at AS BLOB))<=64 "
        "AND length(CAST(source_digest AS BLOB))<=64 "
        "AND length(CAST(inventory_digest AS BLOB))<=64",
        (activation_rowid, scope[0], family),
    ).fetchone()
    evidence = db.execute(
        "SELECT plan_digest FROM workflow_activation_evidence "
        "WHERE rowid=? AND repository=? AND family=? AND epoch=? "
        "AND length(CAST(plan_digest AS BLOB))<=64",
        (evidence_rowid, scope[0], family, execution.binding.execution_epoch),
    ).fetchone()
    if activation is None or evidence is None:
        raise WorkflowAuthorityError("initialized execution origin lost activation evidence")
    try:
        activated = datetime.fromisoformat(activation["activated_at"].replace("Z", "+00:00"))
        digests = (activation["source_digest"], activation["inventory_digest"], evidence["plan_digest"])
        if (activated.tzinfo is None or activated.utcoffset() is None
                or type(activation["epoch"]) is not int
                or activation["epoch"] != execution.binding.execution_epoch
                or digests != (captured.source_digest, captured.inventory_digest,
                               captured.activation_plan_digest)
                or activated != captured.activated_at
                or any(type(value) is not str or len(value) != 64
                       or any(char not in "0123456789abcdef" for char in value) for value in digests)
                or activated > claimed):
            raise ValueError("activation source or time is malformed")
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowAuthorityError("initialized execution origin activation is unreadable") from exc
    import_rows = db.execute(
        "SELECT rowid FROM workflow_activation_imports WHERE repository IN (?,CAST(? AS BLOB)) "
        "AND family IN (?,CAST(? AS BLOB)) AND target_kind IN (?,CAST(? AS BLOB)) "
        "AND target_number IN (?,CAST(? AS BLOB)) LIMIT 1",
        (scope[0], scope[0], family, family, scope[1], scope[1], scope[2], scope[2]),
    ).fetchone()
    if import_rows is not None:
        raise WorkflowAuthorityError("initialized execution origin conflicts with an import")
    observation_rowid = _unique_text_identity_rowid(
        db, "workflow_evidence", (("evidence_id", captured.observation_evidence_id),),
    )
    row = db.execute(
        "SELECT repository,target_kind,target_number,command_id,kind,payload_json,"
        "typeof(repository) AS repository_type,typeof(target_kind) AS target_kind_type,"
        "typeof(target_number) AS target_number_type,typeof(payload_json) AS payload_type "
        "FROM workflow_evidence WHERE rowid=? AND length(CAST(payload_json AS BLOB))<=?",
        (observation_rowid, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if (row is None or row["repository_type"] != "text" or row["target_kind_type"] != "text"
            or row["target_number_type"] != "integer" or row["payload_type"] != "text"
            or (row["repository"], row["target_kind"], row["target_number"]) != scope
            or row["kind"] != "observation" or row["command_id"] is not None):
        raise WorkflowAuthorityError("initialized execution origin observation is absent or malformed")
    try:
        recorded = decode_workflow(row["payload_json"], RecordedPullRequest)
        if (encode_workflow(recorded) != row["payload_json"] or recorded.observation.key != key
                or not isinstance(recorded.observation.reviews, CompleteReviews)
                or "pr-observation:" + hashlib.sha256(row["payload_json"].encode()).hexdigest()
                != captured.observation_evidence_id
                or recorded.observation.observed_at < activated
                or not (0 <= (claimed - recorded.observation.observed_at).total_seconds()
                        <= OBSERVATION_MAX_AGE_SECONDS)):
            raise ValueError("observation does not prove fresh first discovery")
    except (TypeError, ValueError, UnicodeError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("initialized execution origin observation is unreadable") from exc


def _check_imported_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution,
    captured: ImportedReviewExecutionOriginReceipt, claimed: datetime,
) -> None:
    family = WorkflowFamily.REVIEW_EXECUTION.value
    activation_rowid = _unique_text_identity_rowid(
        db, "workflow_authority_epochs", (("repository", scope[0]), ("family", family)),
    )
    evidence_rowid = _unique_text_identity_rowid(
        db, "workflow_activation_evidence", (("repository", scope[0]), ("family", family)),
        (("epoch", execution.binding.execution_epoch),),
    )
    import_rowid = _unique_text_identity_rowid(
        db, "workflow_activation_imports",
        (("repository", scope[0]), ("family", family), ("target_kind", scope[1])),
        (("target_number", scope[2]),),
    )
    activation = db.execute(
        "SELECT epoch,source_digest,inventory_digest,activated_at FROM workflow_authority_epochs "
        "WHERE rowid=? AND repository=? AND family=? AND length(CAST(source_digest AS BLOB))<=64 "
        "AND length(CAST(inventory_digest AS BLOB))<=64 "
        "AND length(CAST(activated_at AS BLOB))<=64",
        (activation_rowid, scope[0], family),
    ).fetchone()
    evidence = db.execute(
        "SELECT plan_digest FROM workflow_activation_evidence "
        "WHERE rowid=? AND repository=? AND family=? AND epoch=?",
        (evidence_rowid, scope[0], family, execution.binding.execution_epoch),
    ).fetchone()
    imported = db.execute(
        "SELECT epoch,source_digest,decision_json FROM workflow_activation_imports "
        "WHERE rowid=? AND repository=? AND family=? AND target_kind=? AND target_number=? "
        "AND length(CAST(source_digest AS BLOB))<=64 "
        "AND length(CAST(decision_json AS BLOB))<=?",
        (import_rowid, scope[0], family, scope[1], scope[2], MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if activation is None or evidence is None or imported is None:
        raise WorkflowAuthorityError("review execution origin lost its activation or import source")
    try:
        activated = datetime.fromisoformat(activation["activated_at"].replace("Z", "+00:00"))
        decision = decode_workflow(imported["decision_json"], ImportedReviewExecutionDecision)
        if (activated.tzinfo is None or activated.utcoffset() is None
                or encode_workflow(decision) != imported["decision_json"]):
            raise ValueError("origin activation decision or time is not canonical")
    except (TypeError, ValueError, UnicodeError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("review execution origin activation decision is unreadable") from exc
    if (claimed != activated
            or type(activation["epoch"]) is not int
            or activation["epoch"] != execution.binding.execution_epoch
            or activation["source_digest"] != captured.source_digest
            or activation["inventory_digest"] != captured.inventory_digest
            or evidence["plan_digest"] != captured.activation_plan_digest
            or imported["epoch"] != execution.binding.execution_epoch
            or imported["source_digest"] != captured.source_digest
            or type(imported["decision_json"]) is not str
            or decision.origin != captured.origin
            or decision.checkpoint != captured.checkpoint
            or decision.activation_plan_digest != captured.activation_plan_digest
            or decision.source_digest != captured.source_digest
            or hashlib.sha256(imported["decision_json"].encode()).hexdigest()
            != captured.import_decision_digest):
        raise WorkflowAuthorityError("review execution origin lost its activation or import source")


def _check_execution_projection(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution,
    revision: WorkflowRevision,
) -> None:
    origin, claimed = _read_execution_origin(db, scope, execution)
    if isinstance(origin, ImportedReviewExecutionOriginReceipt):
        _check_imported_execution_origin(db, scope, execution, origin, claimed)
    else:
        _check_initialized_execution_origin(db, scope, execution, origin, claimed)
    episode_scope = (scope[0], scope[2], execution.binding.execution_epoch, execution.binding.execution_episode)
    latest = db.execute(
        "SELECT sequence FROM review_execution_rounds WHERE repository=? AND target_number=? "
        "AND execution_epoch=? AND execution_episode=? ORDER BY sequence DESC LIMIT 1",
        episode_scope,
    ).fetchone()
    if (latest["sequence"] if latest is not None else 0) != execution.last_sequence:
        raise WorkflowAuthorityError("review execution round high water disagrees with allocated sequence")
    suffix_count = execution.last_sequence - execution.checkpoint.through_sequence
    if suffix_count > MAX_EXECUTION_SUFFIX:
        raise WorkflowAuthorityError("review execution suffix exceeds its bounded projection")
    for boundary in execution.boundaries:
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation' "
            "AND scope=? AND key=?", (scope[0], boundary.receipt_id),
        ).fetchone()
        if receipt is None:
            raise WorkflowAuthorityError("review generation boundary lacks its accepted receipt")
        captured = decode_workflow(
            receipt["payload_json"],
            tuple | ReviewExecutionAcceptedTransitionSource | ReviewExecutionTransitionProofSource,
        )
        if isinstance(captured, (ReviewExecutionAcceptedTransitionSource, ReviewExecutionTransitionProofSource)):
            if captured.boundary != boundary:
                raise WorkflowAuthorityError("review generation boundary receipt changed")
            namespace, key = captured.namespace, captured.key
        elif (len(captured) == 4 and captured[0] == boundary
              and all(isinstance(item, str) for item in captured[1:])):
            namespace, key = captured[1:3]
        else:
            raise WorkflowAuthorityError("review generation boundary receipt changed")
        committed = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (namespace, scope[0], key),
        ).fetchone()
        if (committed is None or not isinstance(committed["payload_json"], str)
                or not matches_accepted_review_transition_source(
                    captured, boundary, namespace, key, committed["payload_json"],
                )):
            raise WorkflowAuthorityError("review generation boundary lost its accepted transition")
        accepted = committed["payload_json"]
        try:
            payload = json.loads(accepted)
            observed = decode_workflow(payload["snapshot"], ReviewWorkflowSnapshot)
            expected = decode_workflow(payload["expected"], WorkflowRevision)
            audit = decode_workflow(payload["audit"], AuthorizedOperatorCommand) if (
                boundary.mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM)
            ) else decode_workflow(payload["audit"], ObservedTransition)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError("review generation boundary has invalid transition facts") from exc
        if (payload.get("mutation") != boundary.mutation.value or expected != boundary.from_revision
                or observed.revision != boundary.to_revision
                or not isinstance(observed.execution, ActivatedReviewExecution)
                or not observed.execution.boundaries
                or observed.execution.boundaries[-1] != boundary):
            raise WorkflowAuthorityError("review generation boundary disagrees with accepted transition")
        if (isinstance(captured, ReviewExecutionTransitionProofSource)
                and captured.audit_sha256 != hashlib.sha256(payload["audit"].encode()).hexdigest()):
            raise WorkflowAuthorityError("review generation proof disagrees with accepted transition")
        if isinstance(audit, AuthorizedOperatorCommand):
            if (not isinstance(audit.command, (RequestRearm, ContinueGrantedRearm))
                    or namespace != "workflow-request" or key != audit.command.request_id
                    or audit.command.expected != boundary.from_revision):
                raise WorkflowAuthorityError("review generation boundary request identity changed")
        elif (namespace != "workflow-transition" or key != hashlib.sha256(
            encode_workflow(boundary.from_revision).encode(),
        ).hexdigest()):
            raise WorkflowAuthorityError("review generation boundary release identity changed")
    if execution.checkpoint.through_sequence or execution.checkpoint.boundary_cursor is not None:
        _check_execution_checkpoint_receipt(db, scope[0], execution, origin)
        # The typed chain alone cannot prove the retired source digests or audit gate.
        raise WorkflowAuthorityError("review execution checkpoint source anchors are not yet verified")
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
        if isinstance(outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
            confirmed = tuple(item.plan.command_id for item in facts.submissions
                              if isinstance(item.result, ReviewSubmissionConfirmed))
            if len(confirmed) != 1:
                raise WorkflowAuthorityError("productive review round lacks one confirmed submission")
            baseline = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (RECORDED_REVIEW_NAMESPACE, scope[0], confirmed[0]),
            ).fetchone()
            stale = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_STALE_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
            ).fetchone()
            superseded = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
            ).fetchone()
            ceiling = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
            ).fetchone()
            if sum(item is not None for item in (baseline, stale, superseded, ceiling)) != 1:
                raise WorkflowAuthorityError("productive review round lacks one settlement authority")
            if baseline is not None:
                recorded_revision = decode_workflow(baseline["payload_json"], WorkflowRevision)
                if (recorded_revision.fence != plan.round.binding.rejection.fence
                        or (recorded_revision.fence == revision.fence
                            and recorded_revision.version > revision.version)):
                    raise WorkflowAuthorityError("productive review round baseline receipt disagrees with its generation")
            elif stale is not None:
                old_generation = identity.binding.rejection.fence.generation
                recorded = decode_workflow(
                    stale["payload_json"], tuple | StaleReviewProductivitySettlement,
                )
                if isinstance(recorded, tuple):
                    valid = recorded == (facts, outcome)
                else:
                    settled_generation = recorded.settled_revision.fence.generation
                    crossings = tuple(boundary for boundary in execution.boundaries
                                      if old_generation <= boundary.from_revision.fence.generation
                                      < settled_generation)
                    valid = (
                        recorded.facts == facts and recorded.outcome == outcome
                        and recorded.settled_revision.fence.key == revision.fence.key
                        and recorded.settled_revision.version <= revision.version
                        and settled_generation <= revision.fence.generation
                        and same_execution_episode(recorded.settled_binding, execution.binding)
                        and len(crossings) == settled_generation - old_generation
                        and all(boundary.from_revision.fence.generation == old_generation + index
                                and boundary.to_revision.fence.generation == old_generation + index + 1
                                and same_execution_episode(boundary.before.binding, identity.binding)
                                and same_execution_episode(boundary.after.binding, identity.binding)
                                for index, boundary in enumerate(crossings))
                        and bool(crossings)
                        and crossings[-1].to_revision.version <= recorded.settled_revision.version
                        and recorded.crossing_digest == review_generation_chain_digest(
                            tuple(boundary.receipt_id for boundary in crossings)
                        )
                    )
                if not valid or old_generation >= revision.fence.generation:
                    raise WorkflowAuthorityError("stale productive review round lacks its exact retirement")
            elif superseded is not None:
                supersession = decode_workflow(superseded["payload_json"], tuple)
                if (len(supersession) != 3 or not isinstance(supersession[0], str)
                        or not isinstance(supersession[1], WorkflowRevision)
                        or not isinstance(supersession[2], str)):
                    raise WorkflowAuthorityError("superseded review round receipt is malformed")
                recorded_id, recorded_revision, settled_stamp = supersession
                evidence = db.execute(
                    "SELECT payload_json,kind,repository,target_kind,target_number,command_id FROM workflow_evidence "
                    "WHERE evidence_id=?", (recorded_id,),
                ).fetchone()
                if (evidence is None or evidence["kind"] != "observation" or evidence["command_id"] is not None
                        or (evidence["repository"], evidence["target_kind"], evidence["target_number"]) != scope
                        or recorded_id != "pr-observation:" + hashlib.sha256(evidence["payload_json"].encode()).hexdigest()):
                    raise WorkflowAuthorityError("superseded review round lacks its exact observation")
                observed = decode_workflow(evidence["payload_json"], RecordedPullRequest)
                settled_at = datetime.fromisoformat(settled_stamp)
                if settled_at.tzinfo is None or settled_at.utcoffset() is None:
                    raise WorkflowAuthorityError("superseded review round settlement time is not aware")
                observation_age = (settled_at - observed.observation.observed_at).total_seconds()
                if (observed.observation.key != revision.fence.key
                        or (observed.observation.head_sha == plan.head_sha
                            and observed.observation.state is PullRequestState.OPEN)
                        or not 0 <= observation_age <= OBSERVATION_MAX_AGE_SECONDS
                        or recorded_revision.fence != plan.round.binding.rejection.fence
                        or (recorded_revision.fence == revision.fence
                            and recorded_revision.version > revision.version)):
                    raise WorkflowAuthorityError("superseded review round observation disagrees with its head")
            elif ceiling is not None:
                record = decode_workflow(ceiling["payload_json"], tuple)
                if (len(record) != 3 or not isinstance(record[0], CeilingProductivityDecision)
                        or not isinstance(record[1], WorkflowRevision)
                        or not isinstance(record[2], str)):
                    raise WorkflowAuthorityError("ceiling review round receipt is malformed")
                decision, recorded_revision, settled_stamp = record
                if (decision.escalation.escalate_cause != "ceiling"
                        or str(decision.escalation.submission.command_id) != confirmed[0]
                        or decision.escalation.submission.binding != plan.round.binding.rejection
                        or decision.prior.revision.fence != plan.round.binding.rejection.fence
                        or record[1].fence != plan.round.binding.rejection.fence
                        or (recorded_revision.fence == revision.fence
                            and recorded_revision.version > revision.version)):
                    raise WorkflowAuthorityError("ceiling review round lacks its bound escalation")
                evidence = db.execute(
                    "SELECT payload_json,kind,repository,target_kind,target_number,command_id FROM workflow_evidence "
                    "WHERE evidence_id=?", (decision.observation_evidence_id,),
                ).fetchone()
                if (evidence is None or evidence["kind"] != "observation" or evidence["command_id"] is not None
                        or (evidence["repository"], evidence["target_kind"], evidence["target_number"]) != scope
                        or decision.observation_evidence_id != "pr-observation:" + hashlib.sha256(
                            evidence["payload_json"].encode()).hexdigest()):
                    raise WorkflowAuthorityError("ceiling review round lacks its exact observation")
                observed = decode_workflow(evidence["payload_json"], RecordedPullRequest)
                settled_at = datetime.fromisoformat(settled_stamp)
                if settled_at.tzinfo is None or settled_at.utcoffset() is None:
                    raise WorkflowAuthorityError("ceiling review round settlement time is not aware")
                if (observed.observation.key != revision.fence.key
                        or not 0 <= (decision.decided_at - observed.observation.observed_at).total_seconds()
                        <= OBSERVATION_MAX_AGE_SECONDS
                        or not 0 <= (settled_at - observed.observation.observed_at).total_seconds()
                        <= OBSERVATION_MAX_AGE_SECONDS):
                    raise WorkflowAuthorityError("ceiling review round observation expired")
                replayed = open_review_ceiling(
                    decision.prior, observed.observation, now=decision.decided_at,
                    rule=decision.configured_rule, ceiling=decision.configured_ceiling,
                    escalation=decision.escalation, trigger_label=decision.trigger_label,
                    executor_identity=decision.executor_identity,
                    evidence_ids=(decision.observation_evidence_id,),
                )
                if (isinstance(replayed, WorkflowRejected)
                        and (replayed != decision.refusal or recorded_revision != decision.prior.revision)):
                    raise WorkflowAuthorityError("ceiling review refusal disagrees with its observation")
                if (isinstance(replayed, WorkflowChanged)
                        and (decision.refusal is not None or recorded_revision != replayed.snapshot.revision)):
                    raise WorkflowAuthorityError("ceiling review gate disagrees with its observation")
                if (isinstance(replayed, WorkflowUnchanged)
                        and (decision.refusal is not None or recorded_revision != decision.prior.revision)):
                    raise WorkflowAuthorityError("ceiling review replay disagrees with its observation")
        elif db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_STALE_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
        ).fetchone() is not None or db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
        ).fetchone() is not None or db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, scope[0], identity.round_id),
        ).fetchone() is not None:
            raise WorkflowAuthorityError("nonproductive review round has an unapplied productivity receipt")
        preparation = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_PREPARATION_NAMESPACE, scope[0], identity.round_id),
        ).fetchone()
        if isinstance(result, DeferredReviewPreparation):
            if preparation is None or decode_workflow(preparation["payload_json"], tuple) != (facts, result.failures):
                raise WorkflowAuthorityError("review preparation outcome lacks its immutable obligation")
        elif preparation is not None:
            raise WorkflowAuthorityError("review round has a contradictory preparation obligation")
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
    projected = replay_review_execution_generations(
        execution.checkpoint, tuple(outcomes), execution.boundaries,
    )
    if projected != execution.state:
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
