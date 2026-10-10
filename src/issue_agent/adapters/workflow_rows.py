"""Transaction-local workflow reads shared by admission and progression; ADR 0015."""

from __future__ import annotations

import hashlib
import sqlite3
import json
from dataclasses import replace
from datetime import datetime
from typing import TypeVar

from ..domain.resolve_workspace import EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared
from ..domain import continuation_components as cc
from ..domain.review_workflow import (
    AnyWorkflowSnapshot, CancelledReviewRearm, CompleteReviews, CompletedReviewRearm, PendingReviewRearm, PullRequestState,
    RecordedPullRequest, ReviewEpisode,
    ReviewFamilyEpisodes, ReviewRearmIntent, ReviewWorkflowSnapshot, SupersededReviewRearm,
    ReviewExecutionRenewalGrant,
)
from ..domain.review_execution import (
    MAX_EXECUTION_SUFFIX,
    ActivatedReviewExecution,
    CopiedReviewProgress,
    ExecutionNotActivated,
    PaidReviewProgress,
    ImportedReviewExecutionDecision,
    ImportedReviewExecutionOriginReceipt,
    InitializedReviewExecutionOriginReceipt,
    RenewedReviewExecutionOriginReceipt,
    initialized_review_execution,
    ReviewExecutionCheckpoint,
    ReviewExecutionGenerationBoundary,
    ReviewExecutionOriginKind,
    ReviewRoundOutcome,
    same_execution_episode,
    CurrentReviewExecution, BlockedReviewExecutionMigration,
    OpenReviewExecutionRound,
    CompletedReviewExecutionRound,
    PendingReviewExecutionRound,
    review_generation_receipt_id,
    reduce_current_review_execution,
)
from ..domain.review_execution_activation import MAX_LEGACY_SQL_TEXT_BYTES
from ..domain.review_rounds import (
    DeferredReviewPreparation, FinalizedReviewRound, ReviewParentAbandoned,
    ReviewSealedParentAbandoned,
    MAX_REVIEW_STAGES, MAX_ROUND_SUBMISSIONS, ReviewPlanningComplete, ReviewPlanningOpen,
    ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan, ReviewStagePlan,
    ReviewSubmissionConfirmed, ReviewSubmissionRegistration,
    StaleReviewProductivitySettlement, finalize_review_round,
    CurrentReviewOutcomeReceipt,
    CurrentStaleReviewProductivitySettlement,
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
    WorkflowMutation, WorkflowRejected, WorkflowSnapshot, WorkflowUnchanged, IssueFamilyEpisodes, IssueWorkflowShell,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision, target_family, MAX_WORKFLOW_GATES,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow


class WorkflowAuthorityError(ValueError):
    pass


class ReviewExecutionHeld(WorkflowAuthorityError):
    """Prior authority is readable for settlement, but grants no new admission."""

    def __init__(self, snapshot: ReviewWorkflowSnapshot):
        super().__init__("legacy review execution is held pending stopped verified conversion")
        self.snapshot = snapshot


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
MAX_EXECUTION_HOT_ROUND_SOURCE_BYTES = 2**27


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
    """Retain the exact planning conclusion while fencing a departed parent's children."""
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
    if not isinstance(planning, ReviewPlanningSealed):
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
                               recorded[3], plan, planning.conclusion))
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


def encode_workflow_episodes(snapshot: AnyWorkflowSnapshot | IssueWorkflowShell) -> str:
    if isinstance(snapshot, IssueWorkflowShell):
        return encode_workflow(snapshot)
    if isinstance(snapshot, ReviewWorkflowSnapshot):
        return encode_workflow(ReviewFamilyEpisodes(snapshot.authority, snapshot.episode, snapshot.execution, snapshot.pr_repair))
    if not isinstance(snapshot.issue_recovery, cc.InactiveIssueRecovery) or not isinstance(snapshot.steward, cc.InactiveSteward):
        return encode_workflow(IssueFamilyEpisodes(snapshot.authority, snapshot.episode, snapshot.issue_recovery, snapshot.steward))
    return encode_workflow((snapshot.authority, snapshot.episode))


def initialize_new_continuations(db: sqlite3.Connection, snapshot: AnyWorkflowSnapshot | IssueWorkflowShell):
    """First observation only; accepted historical decisions are never a fresh baseline."""
    if not db.in_transaction:
        raise ValueError("continuation discovery requires the creation transaction")
    key = snapshot.revision.fence.key
    created = []
    def identity(family: WorkflowFamily):
        authority = load_family_authority(db, family, key.repository)
        if authority is None:
            return None
        if db.execute("SELECT 1 FROM workflow_activation_imports WHERE repository=? AND family=? AND target_kind=? AND target_number=?",
                      (key.repository, family.value, *workflow_scope(key)[1:])).fetchone():
            raise WorkflowAuthorityError("historical continuation decision cannot initialize fresh state")
        evidence = db.execute("SELECT plan_digest FROM workflow_activation_evidence WHERE repository=? AND family=? AND epoch=? "
                              "AND typeof(plan_digest)='text' AND length(CAST(plan_digest AS BLOB))=64",
                              (key.repository, family.value, authority.epoch)).fetchone()
        if evidence is None or any(c not in "0123456789abcdef" for c in evidence[0]):
            raise WorkflowAuthorityError("continuation discovery requires accepted activation evidence")
        from ..domain.workflow_identity import ContinuationComponentIdentity
        episode = "fresh:" + hashlib.sha256(encode_workflow((snapshot.revision.fence, authority, evidence[0])).encode()).hexdigest()
        return ContinuationComponentIdentity(key, authority, episode)
    if isinstance(snapshot, ReviewWorkflowSnapshot):
        from .continuation_activation import retain_deferred_pr_repair
        snapshot = retain_deferred_pr_repair(db, snapshot)
        if isinstance(snapshot.pr_repair, cc.InactivePRRepair):
            selected = identity(WorkflowFamily.PR_REPAIR)
            if selected:
                snapshot = replace(snapshot, pr_repair=cc.ActivePRRepair(selected, cc.PRRepairState()))
            if selected:
                created.append(snapshot.pr_repair)
    else:
        if isinstance(snapshot.issue_recovery, cc.InactiveIssueRecovery):
            selected = identity(WorkflowFamily.ISSUE_RECOVERY)
            if selected:
                snapshot = replace(snapshot, issue_recovery=cc.ActiveIssueRecovery(selected, cc.IssueRecoveryState()))
                created.append(snapshot.issue_recovery)
        if isinstance(snapshot.steward, cc.InactiveSteward):
            selected = identity(WorkflowFamily.STEWARD)
            if selected:
                snapshot = replace(snapshot, steward=cc.ActiveSteward(selected, cc.StewardState()))
                created.append(snapshot.steward)
    for component in created:
        if isinstance(component, (cc.ActiveIssueRecovery, cc.ActivePRRepair, cc.ActiveSteward)):
            insert_workflow_component(db, key, component.identity.authority, component.identity.episode_id)
    return snapshot


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
    WorkflowFamily.ISSUE_RECOVERY: "issue",
    WorkflowFamily.PR_REPAIR: "pr",
    WorkflowFamily.STEWARD: "issue",
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
    if family in (WorkflowFamily.REVIEW_EXECUTION, WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD):
        retained = (
            db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND family=? LIMIT 1", (repository, family.value)).fetchone()
            or (db.execute("SELECT 1 FROM review_execution_rounds WHERE repository=? LIMIT 1", (repository,)).fetchone() if family is WorkflowFamily.REVIEW_EXECUTION else None)
            or db.execute("SELECT 1 FROM workflow_activation_imports WHERE repository=? AND family=? LIMIT 1", (repository, family.value)).fetchone()
        )
    else:
        retained = (
        db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND family=? LIMIT 1", (repository, family.value)).fetchone()
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
            or db.execute("SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? LIMIT 1",
                          workflow_run_scope_bounds(key)).fetchone()
        )
        imported = db.execute("SELECT 1 FROM workflow_activation_imports WHERE repository=? AND target_kind=? AND target_number=? LIMIT 1", scope).fetchone()
        if retained is not None or imported is not None:
            raise WorkflowAuthorityError("workflow is missing despite retained target authority")
    return activation, row


def _load_gates(db: sqlite3.Connection, scope: tuple[str, str, int]) -> tuple[OpenGate | RearmingGate, ...]:
    gates = []
    gate_rows = db.execute(
        "SELECT * FROM workflow_gates WHERE repository=? AND target_kind=? AND target_number=? "
        "AND state<>'closed' ORDER BY gate_id LIMIT ?", (*scope, MAX_WORKFLOW_GATES + 1),
    ).fetchall()
    if len(gate_rows) > MAX_WORKFLOW_GATES:
        raise WorkflowEncodingError("active gate count exceeds its bound")
    for gate_row in gate_rows:
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


def load_review_execution_import_decision(
    db: sqlite3.Connection, key: WorkflowKey,
) -> ImportedReviewExecutionDecision | BlockedReviewExecutionMigration | None:
    """Read one retained target decision under its exact active execution epoch."""
    scope = workflow_scope(key)
    family = WorkflowFamily.REVIEW_EXECUTION
    row = db.execute(
        "SELECT CASE WHEN typeof(epoch)='integer' THEN epoch END AS epoch,"
        "CASE WHEN typeof(source_digest)='text' AND length(CAST(source_digest AS BLOB))=64 "
        "THEN source_digest END AS source_digest,typeof(decision_json) AS payload_type,"
        "length(CAST(decision_json AS BLOB)) AS payload_bytes FROM workflow_activation_imports "
        "WHERE repository IN (?,CAST(? AS BLOB)) AND target_kind IN (?,CAST(? AS BLOB)) "
        "AND target_number IN (?,CAST(? AS BLOB)) AND family IN (?,CAST(? AS BLOB)) LIMIT 2",
        (scope[0], scope[0], scope[1], scope[1], scope[2], scope[2], family.value, family.value),
    ).fetchone()
    if row is None:
        return None
    authority = load_family_authority(db, family, key.repository)
    if (authority is None or row["epoch"] != authority.epoch or row["payload_type"] != "text"
            or type(row["payload_bytes"]) is not int or not 0 < row["payload_bytes"] <= MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("review execution target decision is absent, malformed or stale")
    rowid = _unique_text_identity_rowid(
        db, "workflow_activation_imports", (("repository", key.repository), ("family", family.value),
                                           ("target_kind", scope[1])),
        (("epoch", authority.epoch), ("target_number", scope[2])), source="review migration decision",
    )
    payload = db.execute("SELECT decision_json FROM workflow_activation_imports WHERE rowid=?", (rowid,)).fetchone()[0]
    decision = decode_workflow(payload, ImportedReviewExecutionDecision | BlockedReviewExecutionMigration)
    activation = db.execute(
        "SELECT CASE WHEN typeof(source_digest)='text' AND length(CAST(source_digest AS BLOB))=64 "
        "THEN source_digest END AS source_digest FROM workflow_authority_epochs WHERE repository=? AND family=? AND epoch=?",
        (key.repository, family.value, authority.epoch),
    ).fetchone()
    evidence = db.execute(
        "SELECT CASE WHEN typeof(plan_digest)='text' AND length(CAST(plan_digest AS BLOB))=64 "
        "THEN plan_digest END AS plan_digest FROM workflow_activation_evidence WHERE repository=? AND family=? AND epoch=?",
        (key.repository, family.value, authority.epoch),
    ).fetchone()
    target_key = decision.key if isinstance(decision, BlockedReviewExecutionMigration) else decision.checkpoint.state.binding.rejection.fence.key
    epoch = decision.authority.epoch if isinstance(decision, BlockedReviewExecutionMigration) else decision.checkpoint.state.binding.execution_epoch
    if (target_key != key or epoch != authority.epoch or encode_workflow(decision) != payload
            or activation is None or evidence is None or decision.source_digest != row["source_digest"]
            or decision.source_digest != activation["source_digest"]
            or decision.activation_plan_digest != evidence["plan_digest"]):
        raise WorkflowAuthorityError("review execution target decision conflicts with its activation")
    return decision


def _read_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution | CurrentReviewExecution,
) -> tuple[ImportedReviewExecutionOriginReceipt | InitializedReviewExecutionOriginReceipt | RenewedReviewExecutionOriginReceipt, datetime]:
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
                        if execution.origin.kind is ReviewExecutionOriginKind.INITIALIZED
                        else RenewedReviewExecutionOriginReceipt
                        if execution.origin.kind is ReviewExecutionOriginKind.RENEWED else None)
        if receipt_type is None:
            raise ValueError("unsupported execution origin cause")
        captured = decode_workflow(row["payload_json"], receipt_type)
        if encode_workflow(captured) != row["payload_json"]:
            raise ValueError("origin receipt is not canonical")
    except (TypeError, ValueError, UnicodeError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("review execution origin receipt is unreadable") from exc
    checkpoint = execution.baseline if isinstance(execution, CurrentReviewExecution) else execution.checkpoint
    origin_binding = captured.checkpoint.state.binding
    initial_checkpoint = checkpoint.through_sequence == 0 and (
        checkpoint.state.binding == origin_binding if isinstance(execution, CurrentReviewExecution)
        else checkpoint.boundary_cursor is None
    )
    if (captured.origin != execution.origin
            or not same_execution_episode(origin_binding, execution.binding)
            or origin_binding.execution_epoch != execution.binding.execution_epoch
            or captured.checkpoint.policy != checkpoint.policy
            or initial_checkpoint and checkpoint != captured.checkpoint):
        raise WorkflowAuthorityError("review execution origin disagrees with its component")
    return captured, claimed


def _check_renewed_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], captured: RenewedReviewExecutionOriginReceipt,
) -> None:
    from ..domain.workflow import AuthorizedOperatorCommand, RequestReviewExecutionRenewal, RequestReviewVerdictRelease

    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-request' AND scope=? AND key=? "
        "AND reusable_after IS NULL AND length(CAST(payload_json AS BLOB))<=?",
        (scope[0], captured.request_id, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    try:
        accepted = json.loads(row["payload_json"]) if row is not None else None
        if (not isinstance(accepted, dict) or set(accepted) != {"mutation", "expected", "snapshot", "audit", "commands"}
                or accepted.get("mutation") != "request_rearm"):
            raise ValueError("renewal lacks its accepted request transition")
        audit = decode_workflow(accepted["audit"], AuthorizedOperatorCommand)
        snapshot = decode_workflow(accepted["snapshot"], ReviewWorkflowSnapshot)
        command = audit.command
        execution = snapshot.execution
        grant = snapshot.intent.grant if snapshot.intent is not None else None
        if (not isinstance(command, RequestReviewExecutionRenewal)
                or isinstance(command, RequestReviewVerdictRelease)
                or command.request_id != captured.request_id or command.expected != captured.expected
                or decode_workflow(accepted["expected"], WorkflowRevision) != captured.expected
                or snapshot.revision.version != captured.expected.version + 1
                or command.expected_binding != captured.previous_binding.rejection
                or command.execution_epoch != captured.previous_binding.execution_epoch
                or command.execution_episode != captured.previous_binding.execution_episode
                or not isinstance(execution, CurrentReviewExecution)
                or execution.origin != captured.origin or execution.baseline != captured.checkpoint
                or not isinstance(grant, ReviewExecutionRenewalGrant)
                or grant.origin_evidence_id != captured.origin.evidence_id
                or grant.original_request_id != captured.request_id
                or grant.previous_episode != captured.previous_binding.execution_episode
                or grant.renewed_episode != captured.checkpoint.state.binding.execution_episode
                or grant.execution_epoch != captured.previous_binding.execution_epoch):
            raise ValueError("renewal origin disagrees with its accepted transition")
    except (ValueError, TypeError, KeyError, WorkflowEncodingError) as exc:
        raise WorkflowAuthorityError("renewed execution origin lacks its exact accepted transition") from exc


def _check_initialized_execution_origin(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution | CurrentReviewExecution,
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
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: ActivatedReviewExecution | CurrentReviewExecution,
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


def _check_active_round_source_budget(
    db: sqlite3.Connection, episode_scope: tuple[str, int, int, str],
    through_sequence: int, expected_count: int,
    *, max_source_bytes: int = MAX_EXECUTION_HOT_ROUND_SOURCE_BYTES,
) -> None:
    """Refuse an oversized active suffix before fetching its SQL payload fields."""
    if (type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_EXECUTION_HOT_ROUND_SOURCE_BYTES):
        raise ValueError("active round source budget must be positive and bounded")
    if (type(expected_count) is not int or not 0 <= expected_count <= MAX_EXECUTION_SUFFIX):
        raise ValueError("active round source count must fit the suffix bound")
    if expected_count == 0:
        return
    columns = ("round_id", "claim_occurrence_id", "plan_json", "planning_json",
               "closure_json", "outcome_json")
    # SQLite 3.43+ derives octet_length(column) from record metadata. Older
    # runtimes keep the prior weaker read behavior until a platform floor exists.
    sizes = [
        (f"ifnull(octet_length({column}),0)" if sqlite3.sqlite_version_info >= (3, 43, 0)
         else f"ifnull(length(CAST({column} AS BLOB)),0)")
        for column in columns
    ]
    metadata_sql = (
        "SELECT sequence,typeof(sequence) AS sequence_type,"
        "typeof(generation) AS generation_type,"
        + ",".join(f"typeof({column}) AS type_{column}" for column in columns)
        + f",max({sizes[2]},{sizes[3]},{sizes[4]},{sizes[5]}) AS largest_json,"
        + f"max({sizes[0]},{sizes[1]}) AS largest_identity,"
        + f"{'+'.join(sizes)} AS total_bytes "
        "FROM review_execution_rounds INDEXED BY review_execution_rounds_episode "
        "WHERE repository=? AND target_number=? AND execution_epoch=? "
        "AND execution_episode=? AND sequence=?"
    )
    source_bytes = 0
    for sequence in range(through_sequence + 1, through_sequence + expected_count + 1):
        row = db.execute(metadata_sql, (*episode_scope, sequence)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("review execution round index has a suffix gap")
        size = row["total_bytes"]
        if (row["sequence_type"] != "integer" or row["sequence"] != sequence
                or row["generation_type"] != "integer"
                or any(row[f"type_{column}"] != "text" for column in columns[:4])
                or any(row[f"type_{column}"] not in {"text", "null"} for column in columns[4:])
                or type(size) is not int or size < 0
                or type(row["largest_json"]) is not int
                or row["largest_json"] > MAX_WORKFLOW_PAYLOAD_BYTES
                or type(row["largest_identity"]) is not int
                or row["largest_identity"] > MAX_LEGACY_SQL_TEXT_BYTES
                or source_bytes + size > max_source_bytes):
            raise WorkflowAuthorityError("review execution round index exceeds its source byte bound")
        source_bytes += size


def _require_secondary_component(db: sqlite3.Connection, key: WorkflowKey, component, family: WorkflowFamily) -> None:
    authority = load_family_authority(db, family, key.repository)
    indexed = db.execute(
        "SELECT epoch,episode_id FROM workflow_components WHERE repository=? AND target_kind=? AND target_number=? AND family=?",
        (*workflow_scope(key), family.value),
    ).fetchone()
    if isinstance(component, (cc.ActiveIssueRecovery, cc.ActivePRRepair, cc.ActiveSteward)):
        if component.identity.key != key or component.identity.authority != authority:
            raise WorkflowAuthorityError("continuation activation disagrees with its component")
        _require_component_index(db, workflow_scope(key), authority, component.identity.episode_id)
    elif isinstance(component, (cc.BlockedIssueRecovery, cc.BlockedPRRepair, cc.BlockedSteward)):
        if component.key != key or component.authority != authority or indexed is not None:
            raise WorkflowAuthorityError("blocked continuation disagrees with its activation")
    elif indexed is not None:
        raise WorkflowAuthorityError("activated continuation lacks its exact component payload")


def load_issue_aggregate(db: sqlite3.Connection, key: WorkflowKey) -> WorkflowSnapshot | IssueWorkflowShell | None:
    try:
        return _load_issue_aggregate(db, key)
    except WorkflowAuthorityError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowAuthorityError(f"invalid issue aggregate authority: {exc}") from exc


def _load_issue_aggregate(db: sqlite3.Connection, key: WorkflowKey) -> WorkflowSnapshot | IssueWorkflowShell | None:
    _require_family(key, WorkflowFamily.RESOLVE)
    row = db.execute("SELECT generation,version,episode_json FROM workflows WHERE repository=? AND target_kind=? AND target_number=?", workflow_scope(key)).fetchone()
    if row is not None:
        payload = decode_workflow(row["episode_json"], tuple | IssueFamilyEpisodes | IssueWorkflowShell)
        if isinstance(payload, IssueWorkflowShell):
            revision = WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"])
            if payload.revision != revision:
                raise WorkflowAuthorityError("issue shell disagrees with its aggregate revision")
            if db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND target_kind=? AND target_number=? AND family='resolve'", workflow_scope(key)).fetchone():
                raise WorkflowAuthorityError("issue shell has unexpected resolve authority")
            _require_secondary_component(db, key, payload.issue_recovery, WorkflowFamily.ISSUE_RECOVERY)
            _require_secondary_component(db, key, payload.steward, WorkflowFamily.STEWARD)
            return payload
    if load_family_authority(db, WorkflowFamily.RESOLVE, key.repository) is None:
        return None
    return load_resolve_workflow_row(db, key)


def load_resolve_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> WorkflowSnapshot | None:
    """Resolve authority for an issue; a PR key is never read as resolve."""
    activation, row = _row_or_absence(db, key, WorkflowFamily.RESOLVE)
    if row is None:
        return None
    scope = workflow_scope(key)
    try:
        family = decode_workflow(row["episode_json"], tuple | IssueFamilyEpisodes)
        recovery, steward = cc.InactiveIssueRecovery(), cc.InactiveSteward()
        if isinstance(family, IssueFamilyEpisodes):
            authority, episode, recovery, steward = family.authority, family.episode, family.issue_recovery, family.steward
        elif len(family) == 2 and isinstance(family[0], FamilyAuthority) and isinstance(family[1], ResolveEpisode):
            authority, episode = family
        else:
            raise WorkflowEncodingError("invalid resolve family episode")
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        _require_component_index(db, scope, authority, episode.episode_id)
        _require_secondary_component(db, key, recovery, WorkflowFamily.ISSUE_RECOVERY)
        _require_secondary_component(db, key, steward, WorkflowFamily.STEWARD)
        gates = _load_gates(db, scope)
        intent_row = _latest_intent_row(db, scope)
        intent = None
        if intent_row is not None:
            intent = decode_workflow(intent_row["intent_json"], PendingRearm | CompletedRearm | SupersededRearm | CancelledRearm)
            _check_intent_index(intent_row, intent, {PendingRearm: "pending", CompletedRearm: "completed", SupersededRearm: "superseded", CancelledRearm: "cancelled"})
        snapshot = WorkflowSnapshot(
            WorkflowRevision(WorkflowFence(key, row["generation"]), row["version"]),
            authority, episode, gates, intent, recovery, steward,
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


def load_legacy_review_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot | None:
    """Stopped conversion and retired-mechanism tests; never new-work admission."""
    from .sql_read_budget import BudgetedSqlConnection, SqlReadBudgetExceeded, bounded_sql_source_reads

    if isinstance(db, BudgetedSqlConnection) and db.in_transaction and sqlite3.sqlite_version_info >= (3, 43, 0):
        try:
            with bounded_sql_source_reads(db, 2**27):
                return _load_review_workflow_row(db, key)
        except SqlReadBudgetExceeded as exc:
            raise WorkflowAuthorityError(str(exc)) from exc
    return _load_review_workflow_row(db, key)


def load_review_workflow_row(db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot | None:
    """Canonical current authority; legacy components remain explicitly held."""
    from .sql_read_budget import SqlReadBudgetExceeded, bounded_sql_source_reads

    try:
        with bounded_sql_source_reads(db, MAX_EXECUTION_HOT_ROUND_SOURCE_BYTES):
            return _load_review_workflow_row(db, key, current_state=True)
    except SqlReadBudgetExceeded as exc:
        raise WorkflowAuthorityError(str(exc)) from exc


def convert_legacy_review_execution(db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot:
    """Stopped operator conversion of a single bounded component without ancestry."""
    from .control_store import SQLiteControlStore

    with SQLiteControlStore.mutation_savepoint(db):
        return _convert_legacy_review_execution(db, key)


def _convert_legacy_review_execution(db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot:
    if not db.in_transaction:
        raise WorkflowAuthorityError("execution conversion requires the stopped acceptance transaction")
    try:
        current = load_review_workflow_row(db, key)
    except ReviewExecutionHeld as held:
        current = held.snapshot
    if current is None or isinstance(current.execution, ExecutionNotActivated):
        raise WorkflowAuthorityError("execution conversion requires an activated component")
    if isinstance(current.execution, CurrentReviewExecution):
        return current
    prior = current.execution
    if (prior.boundaries or prior.checkpoint.boundary_cursor is not None
            or prior.checkpoint_receipt is not None or prior.checkpoint.through_sequence != 0
            or prior.last_sequence > MAX_EXECUTION_SUFFIX):
        raise WorkflowAuthorityError("prior execution ancestry is unsupported; its data remains held")
    for statement in (
        "SELECT 1 FROM dispatch_claims WHERE repository=? LIMIT 1",
        "SELECT 1 FROM budget_reservations WHERE state='active' "
        "AND (repository=? OR workload_repository=? OR repository='*' AND workload_repository='*') LIMIT 1",
        "SELECT 1 FROM lifecycle_commands WHERE repository=? "
        "AND state NOT IN ('succeeded','permanent_failure') LIMIT 1",
        "SELECT 1 FROM runs r LEFT JOIN budget_reservations b ON b.run_id=r.run_id "
        "WHERE (r.state IN ('reserved','starting','running') "
        "OR EXISTS (SELECT 1 FROM leases l WHERE l.run_id=r.run_id)) "
        "AND (b.repository=? OR b.workload_repository=? OR b.run_id IS NULL "
        "OR b.repository='*' AND b.workload_repository='*') LIMIT 1",
    ):
        parameters = (key.repository,) * statement.count("?")
        if db.execute(statement, parameters).fetchone() is not None:
            raise WorkflowAuthorityError("execution conversion requires quiescent claims, money and commands")
    # Verification is stopped and capped to one initial generation. Runtime
    # admission never invokes this historical reader or trusts its fallback.
    verified = load_legacy_review_workflow_row(db, key)
    if verified != current:
        raise WorkflowAuthorityError("prior execution changed during stopped conversion")
    converted = replace(current, execution=CurrentReviewExecution(
        prior.origin, prior.checkpoint, prior.state, 0, prior.last_sequence,
    ))
    persist_workflow_snapshot(db, converted)
    if load_review_workflow_row(db, key) != converted:
        raise WorkflowAuthorityError("converted execution failed current readback")
    return converted


def read_current_review_pending(
    db: sqlite3.Connection, execution: CurrentReviewExecution,
) -> tuple[PendingReviewExecutionRound, ...]:
    """Select one generation's unresolved range, including results behind an open gap."""
    if not isinstance(execution, CurrentReviewExecution):
        raise WorkflowAuthorityError("pending read requires current execution authority")
    binding = execution.binding
    key = binding.rejection.fence.key
    episode = (key.repository, key.target.number, binding.execution_epoch, binding.execution_episode)
    count = execution.last_sequence - execution.baseline.through_sequence
    _check_active_round_source_budget(db, episode, execution.baseline.through_sequence, count)
    rows = db.execute(
        "SELECT round_id,generation,sequence,plan_json,planning_json,closure_json,outcome_json "
        "FROM review_execution_rounds INDEXED BY review_execution_rounds_generation_pending "
        "WHERE repository=? AND target_number=? AND execution_epoch=? AND execution_episode=? "
        "AND generation=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
        (*episode, binding.rejection.fence.generation, execution.baseline.through_sequence,
         execution.last_sequence, MAX_EXECUTION_SUFFIX + 1),
    ).fetchall()
    if len(rows) != count:
        raise WorkflowAuthorityError("current review execution pending range is incomplete")
    if not rows:
        return ()
    metadata = db.execute(
        "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' AND target_number=?",
        (key.repository, key.target.number),
    ).fetchone()
    if metadata is None or metadata["generation"] != binding.rejection.fence.generation:
        raise WorkflowAuthorityError("current pending source revision is absent or stale")
    revision = WorkflowRevision(binding.rejection.fence, metadata["version"])
    pending: list[PendingReviewExecutionRound] = []
    for sequence, row in enumerate(rows, execution.baseline.through_sequence + 1):
        plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
        if (plan.round.binding != binding or plan.round.sequence != sequence
                or row["sequence"] != sequence or row["round_id"] != plan.round.round_id):
            raise WorkflowAuthorityError("current pending round disagrees with its allocated identity")
        if row["outcome_json"] is None:
            captured = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-preflight' "
                "AND scope=? AND key=? AND reusable_after IS NULL "
                "AND length(CAST(payload_json AS BLOB))<=?",
                (key.repository, plan.round.round_id, MAX_WORKFLOW_PAYLOAD_BYTES),
            ).fetchone()
            if captured is None:
                raise WorkflowAuthorityError("current pending round lacks its preflight source")
            preflight = decode_workflow(captured["payload_json"], tuple)
            if (len(preflight) != 3 or not isinstance(preflight[0], ReviewPreflightSelection)
                    or not isinstance(preflight[1], str) or not isinstance(preflight[2], ReviewRoundPlan)):
                raise WorkflowAuthorityError("current pending preflight source is malformed")
            selection = preflight[0]
            if (selection.request_id != plan.round.round_id or selection.binding != plan.round.binding
                    or replace(preflight[2], stages=plan.stages, submissions=plan.submissions) != plan):
                raise WorkflowAuthorityError("current pending round disagrees with its exact preflight")
            planning = decode_workflow(
                row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
            )
            verified_review_completed_seal(
                db, key.repository, preflight[1], selection.claim_occurrence_id, plan, planning,
            )
            verified_review_parent_abandonment(
                db, key.repository, preflight[1], selection.claim_occurrence_id,
                selection.claim_target, plan, planning,
            )
            require_review_submission_receipts(
                db, key.repository, plan, selection.claim_occurrence_id, revision,
            )
            if row["closure_json"] is not None:
                raise WorkflowAuthorityError("current pending round has an incomplete closure")
            receipt = db.execute(
                "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_OUTCOME_NAMESPACE, key.repository, plan.round.round_id),
            ).fetchone()
            if receipt is not None:
                raise WorkflowAuthorityError("current open round has an orphaned outcome receipt")
            pending.append(OpenReviewExecutionRound(plan.round))
        else:
            outcome = _read_current_round_outcome(db, episode, sequence)
            if outcome.round != plan.round:
                raise WorkflowAuthorityError("current pending outcome belongs to another round")
            pending.append(CompletedReviewExecutionRound(outcome))
    return tuple(pending)


def read_review_outcome_receipt(
    db: sqlite3.Connection, repository: str, round_id: str,
) -> tuple | CurrentReviewOutcomeReceipt | None:
    """One immutable indexed receipt contract for live references and exact retries."""
    metadata = db.execute(
        "SELECT reusable_after,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes,claimed_at,"
        "CASE WHEN typeof(payload_json)='text' AND octet_length(payload_json) BETWEEN 1 AND ? "
        "THEN payload_json END AS payload_json "
        "FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (MAX_WORKFLOW_PAYLOAD_BYTES, REVIEW_OUTCOME_NAMESPACE, repository, round_id),
    ).fetchone()
    if metadata is None:
        return None
    if (metadata["reusable_after"] is not None or metadata["payload_type"] != "text"
            or type(metadata["payload_bytes"]) is not int
            or not 0 < metadata["payload_bytes"] <= MAX_WORKFLOW_PAYLOAD_BYTES
            or not isinstance(metadata["claimed_at"], str) or len(metadata["claimed_at"]) > 64):
        raise WorkflowAuthorityError("review outcome receipt is not bounded immutable evidence")
    try:
        claimed = datetime.fromisoformat(metadata["claimed_at"].replace("Z", "+00:00"))
        if claimed.tzinfo is None or claimed.utcoffset() is None:
            raise ValueError("outcome receipt time requires an offset")
    except ValueError as exc:
        raise WorkflowAuthorityError("review outcome receipt has a malformed claim time") from exc
    return decode_workflow(metadata["payload_json"], tuple | CurrentReviewOutcomeReceipt)


def require_current_review_productivity_receipt(
    db: sqlite3.Connection, receipt: CurrentReviewOutcomeReceipt,
) -> None:
    if not isinstance(receipt.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
        return
    confirmed = tuple(item for item in receipt.facts.submissions
                      if isinstance(item.result, ReviewSubmissionConfirmed))
    if len(confirmed) != 1:
        raise WorkflowAuthorityError("current productivity lacks one exact confirmed submission")
    round = receipt.outcome.round
    scope = round.binding.rejection.fence.key.repository
    stale = receipt.expected.fence.generation > round.binding.rejection.fence.generation
    keys = ((REVIEW_STALE_PRODUCTIVITY_NAMESPACE, round.round_id),) if stale else (
        ("review-recorded-submission", confirmed[0].plan.command_id),
        (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, round.round_id),
        (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, round.round_id),
    )
    for namespace, key in keys:
        row = db.execute(
            "SELECT payload_json,claimed_at FROM dedupe_keys WHERE namespace=? AND scope=? AND key=? "
            "AND reusable_after IS NULL AND typeof(payload_json)='text' "
            "AND typeof(claimed_at)='text' AND length(CAST(claimed_at AS BLOB)) BETWEEN 1 AND 128 "
            "AND length(CAST(payload_json AS BLOB)) BETWEEN 1 AND ?",
            (namespace, scope, key, MAX_WORKFLOW_PAYLOAD_BYTES),
        ).fetchone()
        if row is None:
            continue
        try:
            claimed = datetime.fromisoformat(row["claimed_at"])
            if claimed.tzinfo is None or claimed.utcoffset() is None:
                raise ValueError("productivity receipt requires an aware claim time")
            if stale:
                witness = decode_workflow(row["payload_json"], CurrentStaleReviewProductivitySettlement)
                if witness.receipt == receipt:
                    return
            elif namespace == "review-recorded-submission":
                if decode_workflow(row["payload_json"], WorkflowRevision) == receipt.expected:
                    return
            else:
                marker = decode_workflow(row["payload_json"], tuple)
                if (len(marker) != 3 or marker[1] != receipt.expected
                        or not isinstance(marker[2], str) or len(marker[2]) > 128):
                    continue
                settled_at = datetime.fromisoformat(marker[2])
                if settled_at.tzinfo is None or settled_at.utcoffset() is None:
                    raise ValueError("productivity settlement requires an aware time")
                # SQL receipt stamps retain milliseconds; the marker preserves source precision.
                if settled_at.replace(microsecond=settled_at.microsecond // 1000 * 1000) != claimed:
                    continue
                evidence_id = marker[0] if namespace == REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE else (
                    marker[0].observation_evidence_id if isinstance(marker[0], CeilingProductivityDecision) else None
                )
                evidence = db.execute(
                    "SELECT payload_json FROM workflow_evidence WHERE evidence_id=? AND repository=? "
                    "AND target_kind='pr' AND target_number=? AND kind='observation' "
                    "AND length(CAST(payload_json AS BLOB))<=?",
                    (evidence_id, scope, round.binding.rejection.fence.key.target.number,
                     MAX_WORKFLOW_PAYLOAD_BYTES),
                ).fetchone() if isinstance(evidence_id, str) else None
                if evidence is None:
                    continue
                observed = decode_workflow(evidence["payload_json"], RecordedPullRequest).observation
                if observed.key != round.binding.rejection.fence.key:
                    continue
                if namespace == REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE:
                    if observed.head_sha != receipt.facts.plan.head_sha or observed.state is not PullRequestState.OPEN:
                        return
                elif (marker[0].escalation.submission.binding == round.binding.rejection
                      and str(marker[0].escalation.submission.command_id) == confirmed[0].plan.command_id):
                    return
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("current productivity receipt is malformed") from exc
    raise WorkflowAuthorityError("current productive outcome lacks its exact joint settlement receipt")


def _read_current_round_outcome(
    db: sqlite3.Connection, episode: tuple[str, int, int, str], sequence: int,
) -> ReviewRoundOutcome:
    row = db.execute(
        "SELECT round_id,closure_json,outcome_json FROM review_execution_rounds "
        "WHERE repository=? AND target_number=? AND execution_epoch=? AND execution_episode=? "
        "AND sequence=? AND length(CAST(closure_json AS BLOB))<=? "
        "AND length(CAST(outcome_json AS BLOB))<=?",
        (*episode, sequence, MAX_WORKFLOW_PAYLOAD_BYTES, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None or row["closure_json"] is None or row["outcome_json"] is None:
        raise WorkflowAuthorityError("current execution reference lacks its finalized round")
    captured_receipt = read_review_outcome_receipt(db, episode[0], row["round_id"])
    if captured_receipt is None:
        raise WorkflowAuthorityError("current execution reference lacks its exact outcome receipt")
    # These values were just decoded and validated from the retained receipt.
    # Exact canonical bytes permit reuse; other encodings keep the full decoder.
    if isinstance(captured_receipt, CurrentReviewOutcomeReceipt):
        receipt_facts, receipt_outcome = captured_receipt.facts, captured_receipt.outcome
    elif (len(captured_receipt) == 2 and isinstance(captured_receipt[0], ReviewRoundFacts)
          and isinstance(captured_receipt[1], ReviewRoundOutcome)):
        receipt_facts, receipt_outcome = captured_receipt
    else:
        receipt_facts, receipt_outcome = None, None
    facts = receipt_facts if receipt_facts is not None and row["closure_json"] == encode_workflow(receipt_facts) \
        else decode_workflow(row["closure_json"], ReviewRoundFacts)
    outcome = receipt_outcome if receipt_outcome is not None and row["outcome_json"] == encode_workflow(receipt_outcome) \
        else decode_workflow(row["outcome_json"], ReviewRoundOutcome)
    if isinstance(captured_receipt, CurrentReviewOutcomeReceipt):
        require_current_review_productivity_receipt(db, captured_receipt)
    matching_receipt = (
        captured_receipt.facts == facts and captured_receipt.outcome == outcome
        if isinstance(captured_receipt, CurrentReviewOutcomeReceipt)
        else captured_receipt == (facts, outcome)
    )
    final = finalize_review_round(facts)
    if (not isinstance(final, (FinalizedReviewRound, DeferredReviewPreparation))
            or final.outcome != outcome or outcome.round.round_id != row["round_id"]
            or outcome.round.sequence != sequence
            or outcome.round.binding.rejection.fence.key.repository != episode[0]
            or outcome.round.binding.rejection.fence.key.target.number != episode[1]
            or outcome.round.binding.execution_epoch != episode[2]
            or outcome.round.binding.execution_episode != episode[3]
            or not matching_receipt):
        raise WorkflowAuthorityError("current execution outcome disagrees with its retained receipt")
    from .review_execution_claims import _read_review_preflight_record
    from .review_round_sources import read_review_round_sources
    from .control_store import ControlStoreError

    preflight = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-preflight' "
        "AND scope=? AND key=? AND reusable_after IS NULL "
        "AND length(CAST(payload_json AS BLOB))<=?",
        (episode[0], row["round_id"], MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if preflight is None:
        raise WorkflowAuthorityError("current outcome reference lacks its live preflight receipt")
    captured = decode_workflow(preflight["payload_json"], tuple)
    if len(captured) != 3 or not isinstance(captured[1], str):
        raise WorkflowAuthorityError("current outcome preflight owner is malformed")
    stored = _read_review_preflight_record(db, episode[0], captured[1], row["round_id"], captured)
    selection, _, plan, source = stored
    planning = decode_workflow(source["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned)
    revision_row = db.execute(
        "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' AND target_number=?",
        episode[:2],
    ).fetchone()
    if revision_row is None:
        raise WorkflowAuthorityError("current outcome reference lost its workflow")
    revision = WorkflowRevision(WorkflowFence(plan.round.binding.rejection.fence.key, revision_row["generation"]),
                                revision_row["version"])
    if isinstance(captured_receipt, CurrentReviewOutcomeReceipt) and (
        captured_receipt.expected.fence.generation > revision.fence.generation
        or captured_receipt.expected.fence == revision.fence and captured_receipt.expected.version > revision.version
    ):
        raise WorkflowAuthorityError("current outcome receipt contains a future concurrency precondition")
    try:
        actual = read_review_round_sources(db, episode[0], captured[1], selection, plan, planning, revision)
    except ControlStoreError as exc:
        raise WorkflowAuthorityError("current outcome reference lost its actual accounting or effect evidence") from exc
    if actual != facts:
        raise WorkflowAuthorityError("current outcome reference lost its actual accounting or effect evidence")
    if isinstance(captured_receipt, tuple) and isinstance(outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
        confirmed = tuple(item.plan.command_id for item in facts.submissions
                          if isinstance(item.result, ReviewSubmissionConfirmed))
        if len(confirmed) != 1:
            raise WorkflowAuthorityError("legacy productivity lacks one exact confirmed submission")
        markers = db.execute(
            "SELECT namespace,payload_json FROM dedupe_keys WHERE scope=? AND reusable_after IS NULL "
            "AND ((namespace='review-recorded-submission' AND key=?) "
            "OR (namespace IN (?,?) AND key=?)) "
            "AND length(CAST(payload_json AS BLOB)) BETWEEN 1 AND ? LIMIT 3",
            (episode[0], confirmed[0], REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE,
             REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, outcome.round.round_id, MAX_WORKFLOW_PAYLOAD_BYTES),
        ).fetchall()
        if not markers:
            raise WorkflowAuthorityError("legacy productivity lacks its exact joint settlement receipt")
        for marker in markers:
            encoded = decode_workflow(marker["payload_json"], WorkflowRevision | tuple)
            expected = encoded if isinstance(encoded, WorkflowRevision) else encoded[1] if len(encoded) == 3 else None
            if (not isinstance(expected, WorkflowRevision) or expected.fence != outcome.round.binding.rejection.fence
                    or expected.fence.generation > revision.fence.generation
                    or expected.fence == revision.fence and expected.version > revision.version):
                raise WorkflowAuthorityError("legacy productivity contains an invalid concurrency precondition")
            require_current_review_productivity_receipt(
                db, CurrentReviewOutcomeReceipt(facts, outcome, expected, captured[1]),
            )
    return outcome


def _check_current_execution(
    db: sqlite3.Connection, scope: tuple[str, str, int], execution: CurrentReviewExecution,
    revision: WorkflowRevision,
) -> None:
    """Validate active identities and live references, never replay retired history."""
    from ..domain.review_execution import (
        ClearReviewSession, NoReviewSession, ReplaceReviewSession, SetDisplayedRound,
    )

    origin, claimed = _read_execution_origin(db, scope, execution)
    if isinstance(origin, ImportedReviewExecutionOriginReceipt):
        _check_imported_execution_origin(db, scope, execution, origin, claimed)
    elif isinstance(origin, RenewedReviewExecutionOriginReceipt):
        _check_renewed_execution_origin(db, scope, origin)
    else:
        _check_initialized_execution_origin(db, scope, execution, origin, claimed)
    origin_generation = origin.checkpoint.state.binding.rejection.fence.generation
    generation = execution.binding.rejection.fence.generation
    if generation < origin_generation:
        raise WorkflowAuthorityError("current execution precedes its origin generation")
    if generation == origin_generation:
        if execution.generation_start_sequence != 0:
            raise WorkflowAuthorityError("initial current execution has an invented generation cutoff")
    else:
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation' "
            "AND scope=? AND key=? AND reusable_after IS NULL "
            "AND length(CAST(payload_json AS BLOB))<=?",
            (scope[0], review_generation_receipt_id(execution.binding, generation), MAX_WORKFLOW_PAYLOAD_BYTES),
        ).fetchone()
        if receipt is None:
            raise WorkflowAuthorityError("current generation lacks its exact transition receipt")
        boundary = decode_workflow(receipt["payload_json"], ReviewExecutionGenerationBoundary)
        if (boundary.after.binding != execution.binding
                or boundary.through_sequence != execution.generation_start_sequence
                or boundary.to_revision.fence != revision.fence
                or boundary.to_revision.version > revision.version):
            raise WorkflowAuthorityError("current generation transition disagrees with its cutoff or binding")
    episode = (scope[0], scope[2], execution.binding.execution_epoch, execution.binding.execution_episode)
    latest = db.execute(
        "SELECT sequence,typeof(sequence) AS sequence_type FROM review_execution_rounds "
        "WHERE repository=? AND target_number=? AND execution_epoch=? AND execution_episode=? "
        "ORDER BY sequence DESC LIMIT 1", episode,
    ).fetchone()
    if (latest is not None and latest["sequence_type"] != "integer"
            or (latest["sequence"] if latest is not None else 0) != execution.last_sequence):
        raise WorkflowAuthorityError("current execution allocation disagrees with its indexed high water")
    read_current_review_pending(db, execution)
    # Baseline and current state usually name the same round. Validate each
    # selected source once within this read, including carried display sources.
    sequences = {
        sequence
        for state in (execution.baseline.state, execution.state)
        for sequence in (state.display_sequence, state.productive_sequence,
                         state.session_sequence if state.session_sequence > execution.generation_start_sequence else 0)
        if sequence > 0
    }
    references = {sequence: _read_current_round_outcome(db, episode, sequence) for sequence in sequences}
    if any(not same_execution_episode(execution.binding, outcome.round.binding)
           or outcome.round.binding.rejection.fence.generation > generation
           for outcome in references.values()):
        raise WorkflowAuthorityError("current execution live reference belongs to a foreign or future binding")
    for state in (execution.baseline.state, execution.state):
        if state.session_sequence <= execution.generation_start_sequence:
            if generation == origin_generation:
                if state.session != origin.checkpoint.state.session or state.session_sequence != 0:
                    raise WorkflowAuthorityError("current imported session disagrees with its verified origin")
            elif (not isinstance(state.session, NoReviewSession)
                  or state.session_sequence != execution.generation_start_sequence):
                raise WorkflowAuthorityError("current carried session disagrees with its generation transition")
        if state.session_sequence > execution.generation_start_sequence:
            outcome = references[state.session_sequence]
            update = outcome.session
            if (outcome.round.binding != execution.binding
                    or not (isinstance(update, ReplaceReviewSession) and update.session == state.session
                            or isinstance(update, ClearReviewSession) and isinstance(state.session, NoReviewSession))):
                raise WorkflowAuthorityError("current session lacks its exact productive source")
        if state.display_sequence > 0:
            outcome = references[state.display_sequence]
            if not isinstance(outcome.display, SetDisplayedRound) or outcome.display.number != state.displayed_round:
                raise WorkflowAuthorityError("current displayed round lacks its exact outcome source")
        if state.productive_sequence > 0:
            outcome = references[state.productive_sequence]
            if not isinstance(outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
                raise WorkflowAuthorityError("current productive watermark lacks its exact outcome source")


def _load_review_workflow_row(
    db: sqlite3.Connection, key: WorkflowKey, *, current_state: bool = False,
) -> ReviewWorkflowSnapshot | None:
    """Review-rejection authority for a PR; an issue key is never read as review."""
    activation, row = _row_or_absence(db, key, WorkflowFamily.REVIEW_REJECTIONS)
    if row is None:
        return None
    scope = workflow_scope(key)
    try:
        family = decode_workflow(row["episode_json"], tuple | ReviewFamilyEpisodes)
        execution = ExecutionNotActivated()
        repair = cc.InactivePRRepair()
        if isinstance(family, ReviewFamilyEpisodes):
            authority, episode, execution, repair = family.authority, family.episode, family.execution, family.pr_repair
        elif len(family) == 2 and isinstance(family[0], FamilyAuthority) and isinstance(family[1], ReviewEpisode):
            authority, episode = family
        else:
            raise WorkflowEncodingError("invalid review rejection family episode")
        if authority != activation:
            raise WorkflowEncodingError("workflow activation epoch does not match its episode")
        _require_component_index(db, scope, authority, episode.episode_id)
        _require_secondary_component(db, key, repair, WorkflowFamily.PR_REPAIR)
        execution_authority = load_family_authority(db, WorkflowFamily.REVIEW_EXECUTION, key.repository)
        if isinstance(execution, (ActivatedReviewExecution, CurrentReviewExecution)):
            if execution_authority is None or execution.binding.execution_epoch != execution_authority.epoch:
                raise WorkflowEncodingError("execution activation epoch disagrees with its component")
            _require_component_index(
                db, scope, execution_authority, execution.binding.execution_episode,
            )
        elif isinstance(execution, BlockedReviewExecutionMigration):
            if (execution.authority != execution_authority
                    or load_review_execution_import_decision(db, key) != execution
                    or db.execute("SELECT 1 FROM workflow_components WHERE repository=? AND target_kind=? "
                                  "AND target_number=? AND family=?", (*scope, WorkflowFamily.REVIEW_EXECUTION.value)).fetchone() is not None):
                raise WorkflowEncodingError("blocked migration lost its exact retained decision")
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
            authority, episode, gates, intent, execution, repair,
        )
        if current_state:
            if isinstance(execution, ActivatedReviewExecution):
                raise ReviewExecutionHeld(snapshot)
            if isinstance(execution, CurrentReviewExecution):
                _check_current_execution(db, scope, execution, snapshot.revision)
        elif isinstance(execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("current execution is unavailable to the retired legacy reader")
        elif isinstance(execution, ActivatedReviewExecution):
            if (execution.boundaries or execution.checkpoint.through_sequence != 0
                    or execution.checkpoint_receipt is not None or execution.last_sequence > MAX_EXECUTION_SUFFIX):
                raise WorkflowAuthorityError("prior execution ancestry is unsupported; its data remains held")
            candidate = CurrentReviewExecution(execution.origin, execution.checkpoint,
                                               execution.state, 0, execution.last_sequence)
            _check_current_execution(db, scope, candidate, snapshot.revision)
            initial = replace(candidate, state=candidate.baseline.state)
            if reduce_current_review_execution(initial, read_current_review_pending(db, initial)).execution.state != execution.state:
                raise WorkflowAuthorityError("prior execution disagrees with its bounded selected outcomes")
        return snapshot
    except ReviewExecutionHeld:
        raise
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
    if family is WorkflowFamily.RESOLVE:
        aggregate = load_issue_aggregate(db, key)
        if isinstance(aggregate, IssueWorkflowShell):
            return WorkflowMigrationRequired(key, family)
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
    if isinstance(snapshot.execution if snapshot is not None else None, BlockedReviewExecutionMigration) and not allow_gated:
        raise WorkflowAuthorityError("review execution migration is blocked")
    return _require_current(snapshot, binding, allow_gated=allow_gated)


def linked_issue_workflow_holds(db: sqlite3.Connection, key: WorkflowKey) -> tuple[str, ...]:
    """Join issue gates without initializing an inactive or unobserved family.

    An inert shell is a validated aggregate with no gates or re-arm intent, so
    it holds nothing; the strict resolve loader rejects it by design.
    """
    snapshot = load_issue_aggregate(db, key)
    if not isinstance(snapshot, WorkflowSnapshot):
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


def persist_workflow_snapshot(db: sqlite3.Connection, after: AnyWorkflowSnapshot | IssueWorkflowShell) -> None:
    """Write an existing workflow row and its unresolved gate index."""
    scope = workflow_scope(after.revision.fence.key)
    if isinstance(after, (WorkflowSnapshot, IssueWorkflowShell)):
        _require_secondary_component(db, after.revision.fence.key, after.issue_recovery, WorkflowFamily.ISSUE_RECOVERY)
        _require_secondary_component(db, after.revision.fence.key, after.steward, WorkflowFamily.STEWARD)
    else:
        _require_secondary_component(db, after.revision.fence.key, after.pr_repair, WorkflowFamily.PR_REPAIR)
    if isinstance(after, ReviewWorkflowSnapshot) and isinstance(after.execution, (ActivatedReviewExecution, CurrentReviewExecution)):
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
    if isinstance(after, IssueWorkflowShell):
        return
    component = db.execute(
        "UPDATE workflow_components SET epoch=?,episode_id=? WHERE repository=? AND target_kind=? "
        "AND target_number=? AND family=?",
        (after.authority.epoch, after.episode.episode_id, *scope, after.authority.family.value),
    ).rowcount
    if component != 1:
        raise WorkflowAuthorityError("workflow component index is absent")
    # Closed causes remain forensic history, never a runtime file fallback.
    db.execute(
        "UPDATE workflow_gates SET state='closed' WHERE repository=? AND target_kind=? AND target_number=? "
        "AND state<>'closed'",
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
