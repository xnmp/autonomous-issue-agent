"""Transaction-local review execution preflight over a live parent claim."""

from dataclasses import replace
import json
import sqlite3

from ..domain.actions import AgentKind
from ..domain.fix import breaker_key
from ..domain.review_preflight import ReviewPreflightSelection, plan_review_preflight
from ..domain.review_execution import ActivatedReviewExecution
from ..domain.review_rounds import (
    ReviewParentAbandoned, ReviewPlanningOpen, ReviewPlanningSealed, ReviewRoundPlan,
    ReviewStagePlan,
)
from ..domain.review_workflow import ObservedPullRequest
from ..domain.review_rearm import ReleaseStatus
from ..domain.workflow_identity import (
    IssueRef, WorkflowKey, WorkflowRevision, WorkflowWriter, validate_workflow_identifier,
)
from .control_store import SQLiteControlStore, _stamp
from .deployment_authority import deployment_role
from .review_effects import ReviewReads, read_pull_request
from .review_dispatch_holds import review_release_projection, unresolved_review_submissions
from .review_linkage import ReviewLinkedIssueUnavailable, review_linked_issue_holds
from .process_supervisor import ProcessSupervisor
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_STAGE_NAMESPACE, WorkflowAuthorityError, WorkflowRevisionConflict,
    linked_issue_workflow_holds, load_review_workflow_row, persist_workflow_snapshot,
    require_review_stage_receipts, review_stage_receipt_key,
)


_PREFLIGHT_NAMESPACE = "review-round-preflight"
_CLAIM_NAMESPACE = "dispatch-claim-lifecycle"
_REVIEW_CLAIM_LEGS = frozenset({AgentKind.REVIEW.name, AgentKind.RE_REVIEW.name})


class SQLiteReviewExecutionControl:
    """Allocate one unpaid round, preserving its exact read and claim provenance."""

    def __init__(self, control: SQLiteControlStore, writer: WorkflowWriter, gateway: ReviewReads) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("review execution requires a typed control store and writer")
        if writer.role != "review" or deployment_role(writer.repository, writer.owner_id) != "review":
            raise ValueError("review execution requires the repository review owner")
        if not isinstance(gateway, ReviewReads):
            raise ValueError("review execution requires a read-only pull request gateway")
        self._control = control
        self._writer = writer
        self._gateway = gateway

    def _stored_round(
        self, db: sqlite3.Connection, request_id: str,
    ) -> tuple[ReviewPreflightSelection, ReviewRoundPlan, ReviewRoundPlan, sqlite3.Row] | None:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (_PREFLIGHT_NAMESPACE, self._writer.repository, request_id),
        ).fetchone()
        if row is None:
            return None
        stored = decode_workflow(row["payload_json"], tuple)
        if (len(stored) != 3 or not isinstance(stored[0], ReviewPreflightSelection)
                or stored[0].request_id != request_id or stored[1] != self._writer.owner_id
                or not isinstance(stored[2], ReviewRoundPlan)):
            raise WorkflowAuthorityError("review preflight receipt contains conflicting facts")
        selection, plan = stored[0], stored[2]
        if plan.round.round_id != request_id or plan.round.binding != selection.binding:
            raise WorkflowAuthorityError("review preflight receipt disagrees with its round identity")
        round_row = db.execute(
            "SELECT * FROM review_execution_rounds WHERE round_id=?", (request_id,),
        ).fetchone()
        if round_row is None or round_row["claim_occurrence_id"] != selection.claim_occurrence_id:
            raise WorkflowAuthorityError("review preflight receipt lacks its exact durable round")
        current_plan = decode_workflow(round_row["plan_json"], ReviewRoundPlan)
        if replace(plan, stages=current_plan.stages, submissions=current_plan.submissions) != current_plan:
            raise WorkflowAuthorityError("review preflight receipt disagrees with its evolved round plan")
        identity = plan.round
        binding = identity.binding
        if (
            round_row["repository"] != selection.expected.fence.key.repository
            or round_row["target_kind"] != "pr"
            or round_row["target_number"] != selection.expected.fence.key.target.number
            or round_row["execution_epoch"] != binding.execution_epoch
            or round_row["execution_episode"] != binding.execution_episode
            or round_row["generation"] != binding.rejection.fence.generation
            or round_row["sequence"] != identity.sequence
        ):
            raise WorkflowAuthorityError("review preflight receipt disagrees with its round index")
        require_review_stage_receipts(db, self._writer.repository, current_plan)
        return selection, plan, current_plan, round_row

    def _receipt(self, db: sqlite3.Connection, selection: ReviewPreflightSelection) -> ReviewRoundPlan | None:
        stored = self._stored_round(db, selection.request_id)
        if stored is None:
            return None
        recorded, plan, _, _ = stored
        if recorded != selection:
            raise WorkflowAuthorityError("review preflight idempotency key has conflicting facts")
        return plan

    def _stage_receipt(
        self, db: sqlite3.Connection, round_id: str, stage: ReviewStagePlan, current: ReviewRoundPlan,
    ) -> bool:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_STAGE_NAMESPACE, self._writer.repository, review_stage_receipt_key(round_id, stage.stage_id)),
        ).fetchone()
        existing = next((item for item in current.stages if item.stage_id == stage.stage_id), None)
        if existing is not None and existing != stage:
            raise WorkflowAuthorityError("review stage identity has conflicting children")
        if row is None:
            if existing is not None:
                raise WorkflowAuthorityError("registered review stage lacks its immutable receipt")
            return False
        recorded = decode_workflow(row["payload_json"], tuple)
        if recorded != (round_id, stage) or existing != stage:
            raise WorkflowAuthorityError("review stage receipt conflicts with its canonical plan")
        return True

    def _claim(self, db: sqlite3.Connection, selection: ReviewPreflightSelection) -> None:
        target = f"issue:{selection.claim_target}" if selection.claim_target > 0 else f"pr:{-selection.claim_target}"
        claim = db.execute(
            "SELECT * FROM dispatch_claims WHERE repository=? AND role='review' AND target=?",
            (self._writer.repository, target),
        ).fetchone()
        if (claim is None or claim["occurrence_id"] != selection.claim_occurrence_id
                or claim["owner_id"] != self._writer.owner_id or claim["leg"] not in _REVIEW_CLAIM_LEGS):
            raise WorkflowAuthorityError("review parent dispatch claim is absent, changed or unowned")
        created = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (_CLAIM_NAMESPACE, self._writer.repository, f"{selection.claim_occurrence_id}:created"),
        ).fetchone()
        try:
            fact = json.loads(created["payload_json"]) if created is not None else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkflowAuthorityError("review parent claim creation receipt is unreadable") from exc
        if not isinstance(fact, dict) or fact != {
            "role": "review", "target": target, "leg": claim["leg"], "owner_id": claim["owner_id"],
            "owner_pid": claim["owner_pid"], "owner_start_ticks": claim["owner_start_ticks"],
            "claimed_at": claim["claimed_at"], "source": fact.get("source"),
        } or fact["source"] not in ("admission", "v24-migration"):
            raise WorkflowAuthorityError("review parent claim disagrees with its creation receipt")
        if db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key IN (?,?) LIMIT 1",
            (_CLAIM_NAMESPACE, self._writer.repository, f"{selection.claim_occurrence_id}:released",
             f"{selection.claim_occurrence_id}:reclaimed"),
        ).fetchone() is not None:
            raise WorkflowAuthorityError("review parent claim has a contradictory terminal receipt")

    def preflight(self, selection: ReviewPreflightSelection) -> ReviewRoundPlan:
        """Mint one free round after a fresh gateway read; replay is exact and inert."""
        if not isinstance(selection, ReviewPreflightSelection):
            raise ValueError("review preflight requires a typed selection")
        key = selection.expected.fence.key
        if key.repository != self._writer.repository:
            raise WorkflowAuthorityError("review preflight belongs to another repository")
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            prior = self._receipt(db, selection)
            if prior is not None:
                return prior
            claim = db.execute(
                "SELECT owner_pid,owner_start_ticks FROM dispatch_claims "
                "WHERE repository=? AND role='review' AND occurrence_id=?",
                (self._writer.repository, selection.claim_occurrence_id),
            ).fetchone()
            if claim is None:
                raise WorkflowAuthorityError("review parent claim occurrence is absent")
            parent_pid, parent_start_ticks = claim["owner_pid"], claim["owner_start_ticks"]

        if not ProcessSupervisor.matches_identity(parent_pid, parent_start_ticks):
            raise WorkflowAuthorityError("review parent claim process is no longer live")

        observed, view = read_pull_request(self._gateway, key, self._control.now(), reviews=True)
        if (not isinstance(observed, ObservedPullRequest)
                or replace(observed, observed_at=selection.observation.observed_at) != selection.observation):
            raise WorkflowAuthorityError("review preflight lacks its exact fresh complete gateway observation")
        if view is None or breaker_key(view) != selection.claim_target:
            raise WorkflowAuthorityError("review preflight issue linkage disagrees with its parent claim")
        if selection.claim_target > 0:
            try:
                external_holds = review_linked_issue_holds(self._control, self._gateway, key, view)
            except ReviewLinkedIssueUnavailable as exc:
                raise WorkflowAuthorityError(f"linked issue evidence unavailable: {exc}") from exc
            if external_holds:
                raise WorkflowAuthorityError("review preflight held by linked issue: " + "; ".join(external_holds))

        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            prior = self._receipt(db, selection)
            if prior is not None:
                return prior
            self._claim(db, selection)
            current_claim = db.execute(
                "SELECT owner_pid,owner_start_ticks FROM dispatch_claims "
                "WHERE repository=? AND role='review' AND occurrence_id=?",
                (self._writer.repository, selection.claim_occurrence_id),
            ).fetchone()
            if current_claim is None or (current_claim["owner_pid"], current_claim["owner_start_ticks"]) != (
                parent_pid, parent_start_ticks,
            ):
                raise WorkflowAuthorityError("review parent claim process identity changed during preflight")
            if selection.claim_target > 0 and linked_issue_workflow_holds(
                db, WorkflowKey(key.repository, IssueRef(selection.claim_target)),
            ):
                raise WorkflowAuthorityError("review preflight is held by linked issue workflow")
            snapshot = load_review_workflow_row(db, key)
            if snapshot is None:
                raise WorkflowAuthorityError("review execution workflow is absent")
            if snapshot.revision != selection.expected:
                raise WorkflowRevisionConflict(snapshot.revision, "review preflight aggregate version changed")
            if unresolved_review_submissions(db, self._control, snapshot.binding):
                raise WorkflowAuthorityError("review preflight is held by unresolved submission")
            release = review_release_projection(db, self._writer, snapshot)
            if release is not None and release.status is not ReleaseStatus.VERIFIED:
                raise WorkflowAuthorityError("review preflight is held by unverified release projection")
            now = self._control.now()
            plan = plan_review_preflight(snapshot, selection, now=now)
            if not isinstance(snapshot.execution, ActivatedReviewExecution):
                raise WorkflowAuthorityError("review execution is inactive")
            execution = replace(snapshot.execution, last_sequence=plan.round.sequence)
            after = replace(
                snapshot,
                revision=WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1),
                execution=execution,
            )
            db.execute(
                "INSERT INTO review_execution_rounds(round_id,repository,target_kind,target_number,execution_epoch,"
                "execution_episode,generation,sequence,claim_occurrence_id,plan_json,planning_json) "
                "VALUES (?,?,'pr',?,?,?,?,?,?,?,?)",
                (plan.round.round_id, key.repository, key.target.number, selection.binding.execution_epoch,
                 selection.binding.execution_episode, selection.binding.rejection.fence.generation,
                 plan.round.sequence, selection.claim_occurrence_id, encode_workflow(plan),
                 encode_workflow(ReviewPlanningOpen())),
            )
            persist_workflow_snapshot(db, after)
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (_PREFLIGHT_NAMESPACE, key.repository, selection.request_id, _stamp(now),
                 encode_workflow((selection, self._writer.owner_id, plan))),
            )
            self._control._transition(db, "review_round", plan.round.round_id, "preflight_open", {
                "sequence": plan.round.sequence, "claim_occurrence_id": selection.claim_occurrence_id,
            })
            return plan

    def register_stage(
        self, round_id: str, expected: WorkflowRevision, stage: ReviewStagePlan,
    ) -> ReviewRoundPlan:
        """Append one immutable child stage to an open round before reservation."""
        validate_workflow_identifier(round_id, "review round")
        if not isinstance(expected, WorkflowRevision) or not isinstance(stage, ReviewStagePlan):
            raise ValueError("stage registration requires a revision and typed stage")
        if any(child.round.round_id != round_id for child in stage.children):
            raise ValueError("planned review stage belongs to another round")
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review round has no preflight receipt")
            selection, _, current, _ = stored
            if self._stage_receipt(db, round_id, stage, current):
                return current
            claim = db.execute(
                "SELECT owner_pid,owner_start_ticks FROM dispatch_claims "
                "WHERE repository=? AND role='review' AND occurrence_id=?",
                (self._writer.repository, selection.claim_occurrence_id),
            ).fetchone()
            if claim is None:
                raise WorkflowAuthorityError("review parent claim occurrence is absent")
            parent_pid, parent_start_ticks = claim["owner_pid"], claim["owner_start_ticks"]

        if not ProcessSupervisor.matches_identity(parent_pid, parent_start_ticks):
            raise WorkflowAuthorityError("review parent claim process is no longer live")

        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review round preflight receipt disappeared")
            selection, _, current, row = stored
            if self._stage_receipt(db, round_id, stage, current):
                return current
            self._claim(db, selection)
            claim = db.execute(
                "SELECT owner_pid,owner_start_ticks FROM dispatch_claims "
                "WHERE repository=? AND role='review' AND occurrence_id=?",
                (self._writer.repository, selection.claim_occurrence_id),
            ).fetchone()
            if claim is None or (claim["owner_pid"], claim["owner_start_ticks"]) != (
                parent_pid, parent_start_ticks,
            ):
                raise WorkflowAuthorityError("review parent claim process identity changed during stage planning")
            if (not isinstance(decode_workflow(row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned),
                               ReviewPlanningOpen)
                    or row["closure_json"] is not None or row["outcome_json"] is not None):
                raise WorkflowAuthorityError("review round planning is already sealed")
            snapshot = load_review_workflow_row(db, expected.fence.key)
            if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
                raise WorkflowAuthorityError("review execution workflow is absent or inactive")
            if snapshot.revision != expected:
                raise WorkflowRevisionConflict(snapshot.revision, "review stage aggregate version changed")
            if (snapshot.execution.binding != current.round.binding
                    or snapshot.binding != current.round.binding.rejection):
                raise WorkflowAuthorityError("review stage captured a stale dual binding")
            updated = replace(current, stages=current.stages + (stage,))
            changed = db.execute(
                "UPDATE review_execution_rounds SET plan_json=? WHERE round_id=? "
                "AND plan_json=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(updated), round_id, row["plan_json"]),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("review round changed during stage planning")
            after = replace(snapshot, revision=WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1))
            persist_workflow_snapshot(db, after)
            now = self._control.now()
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_STAGE_NAMESPACE, self._writer.repository, review_stage_receipt_key(round_id, stage.stage_id),
                 _stamp(now), encode_workflow((round_id, stage))),
            )
            self._control._transition(db, "review_round", round_id, "stage_registered", {
                "stage_id": stage.stage_id, "children": len(stage.children),
            })
            return updated
