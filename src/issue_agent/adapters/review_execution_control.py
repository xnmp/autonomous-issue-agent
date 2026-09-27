"""Transaction-local review execution preflight over a live parent claim."""

from dataclasses import replace
import hashlib
import sqlite3

from ..domain.fix import breaker_key
from ..domain.contracts import CommandKind, LifecycleCommand
from ..domain.review_preflight import ReviewPreflightSelection, plan_review_preflight
from ..domain.review_execution import ActivatedReviewExecution
from ..domain.review_rounds import (
    ReviewParentAbandoned, ReviewPlanningComplete, ReviewPlanningOpen, ReviewPlanningSealed, ReviewRoundPlan,
    ReviewStagePlan, ReviewSubmissionKind, ReviewSubmissionPlan, ReviewSubmissionRegistration,
)
from ..domain.review_workflow import ObservedPullRequest
from ..domain.review_workflow_policy import review_owed
from ..domain.review_rearm import ReleaseStatus
from ..domain.workflow_identity import (
    IssueRef, WorkflowFence, WorkflowKey, WorkflowRevision, WorkflowWriter, validate_workflow_identifier,
)
from .control_store import (
    PendingCommand, SQLiteControlStore, _command_from_row, _command_payload_digest, _json, _stamp,
)
from .deployment_authority import deployment_role
from .review_effects import ReviewReads, bound_submission_op, read_pull_request
from .review_dispatch_holds import review_release_projection, unresolved_review_submissions
from .review_execution_claims import (
    PREFLIGHT_NAMESPACE, load_review_preflight_record, require_review_parent_claim,
)
from .review_linkage import ReviewLinkedIssueUnavailable, review_linked_issue_holds
from .process_supervisor import ProcessSupervisor
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_ABANDONMENT_NAMESPACE, REVIEW_SEAL_NAMESPACE, REVIEW_STAGE_NAMESPACE,
    REVIEW_SUBMISSION_INDEX_NAMESPACE, REVIEW_SUBMISSION_NAMESPACE,
    WorkflowAuthorityError, WorkflowRevisionConflict,
    linked_issue_workflow_holds, load_review_workflow_row, persist_workflow_snapshot,
    read_review_submission_registration, require_review_submission_receipts,
    review_parent_abandonment, review_stage_receipt_key, review_submission_receipt_key,
    verified_review_completed_seal,
    verified_review_parent_abandonment,
)


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
        return load_review_preflight_record(db, self._writer.repository, self._writer.owner_id, request_id)

    def _abandonment_round(
        self, db: sqlite3.Connection, round_id: str,
    ) -> tuple[str, ReviewPreflightSelection, ReviewRoundPlan, sqlite3.Row] | None:
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (PREFLIGHT_NAMESPACE, self._writer.repository, round_id),
        ).fetchone()
        if receipt is None:
            return None
        captured = decode_workflow(receipt["payload_json"], tuple)
        if len(captured) != 3 or not isinstance(captured[1], str) or not captured[1]:
            raise WorkflowAuthorityError("review abandonment preflight has no captured owner")
        owner_id = captured[1]
        if owner_id not in self._control._owner_lineage(db, self._writer.repository, self._writer.owner_id):
            raise WorkflowAuthorityError("review abandonment owner is outside this deployment lineage")
        stored = load_review_preflight_record(db, self._writer.repository, owner_id, round_id)
        if stored is None:
            raise WorkflowAuthorityError("review abandonment lost its preflight")
        selection, _, plan, row = stored
        return owner_id, selection, plan, row

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

    @staticmethod
    def _require_refused_submissions(db: sqlite3.Connection, plan: ReviewRoundPlan) -> None:
        """A fallback branch needs definitive non-submission of earlier commands."""
        for submission in plan.submissions:
            row = db.execute(
                "SELECT state,attempts FROM lifecycle_commands WHERE command_id=?",
                (submission.command_id,),
            ).fetchone()
            receipt = db.execute(
                "SELECT status,remote_object_id FROM effect_receipts WHERE command_id=?",
                (submission.command_id,),
            ).fetchone()
            if (row is None or row["state"] != "permanent_failure" or row["attempts"] < 1
                    or receipt is None or receipt["status"] != "permanent_failure"
                    or receipt["remote_object_id"] is not None):
                raise WorkflowAuthorityError("review fallback awaits definitive submission refusal")

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
            require_review_parent_claim(
                db, self._writer.repository, self._writer.owner_id, selection.claim_occurrence_id,
                selection.claim_target, probed_identity=(parent_pid, parent_start_ticks),
            )
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
                (PREFLIGHT_NAMESPACE, key.repository, selection.request_id, _stamp(now),
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
            require_review_parent_claim(
                db, self._writer.repository, self._writer.owner_id, selection.claim_occurrence_id,
                selection.claim_target, probed_identity=(parent_pid, parent_start_ticks),
            )
            if (not isinstance(decode_workflow(row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned),
                               ReviewPlanningOpen)
                    or row["closure_json"] is not None or row["outcome_json"] is not None):
                raise WorkflowAuthorityError("review round planning is already sealed")
            self._require_refused_submissions(db, current)
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

    def _completed_seal_receipt(
        self, db: sqlite3.Connection, expected: WorkflowRevision,
        selection: ReviewPreflightSelection, plan: ReviewRoundPlan, row: sqlite3.Row,
    ) -> ReviewPlanningSealed | None:
        planning = decode_workflow(
            row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
        )
        captured = verified_review_completed_seal(
            db, self._writer.repository, self._writer.owner_id,
            selection.claim_occurrence_id, plan, planning,
        )
        if captured is None:
            if not isinstance(planning, ReviewPlanningOpen):
                raise WorkflowAuthorityError("review planning already has another conclusion")
            return None
        if captured != expected or not isinstance(planning, ReviewPlanningSealed):
            raise WorkflowAuthorityError("review planning seal has conflicting revision")
        return planning

    def register_submission(
        self, round_id: str, expected: WorkflowRevision, command: LifecycleCommand,
        kind: ReviewSubmissionKind,
    ) -> PendingCommand:
        """Append the command-to-round link and enqueue the remote effect atomically."""
        validate_workflow_identifier(round_id, "review round")
        if not isinstance(expected, WorkflowRevision) or not isinstance(command, LifecycleCommand):
            raise ValueError("submission registration requires a revision and typed command")
        if not isinstance(kind, ReviewSubmissionKind) or command.kind is not CommandKind.SUBMIT_REVIEW:
            raise ValueError("submission registration requires a paid or copied review command")
        submission = ReviewSubmissionPlan(str(command.command_id), kind)
        digest = _command_payload_digest(command, _json(command.payload))

        def replay(db: sqlite3.Connection, plan: ReviewRoundPlan, claim_occurrence_id: str) -> PendingCommand | None:
            registration = read_review_submission_registration(db, self._writer.repository, submission.command_id)
            if registration is None:
                return None
            expected_registration = ReviewSubmissionRegistration(
                plan.round, submission, claim_occurrence_id, self._writer.owner_id,
                expected, digest, command.idempotency_key, command.config_version,
            )
            if registration != expected_registration or submission not in plan.submissions:
                raise WorkflowAuthorityError("review submission identity has conflicting round facts")
            with_row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE command_id=?", (submission.command_id,),
            ).fetchone()
            if with_row is None or _command_from_row(with_row) != command:
                raise WorkflowAuthorityError("review submission receipt lacks its exact command")
            current_revision_row = db.execute(
                "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' "
                "AND target_number=?",
                (self._writer.repository, plan.round.binding.rejection.fence.key.target.number),
            ).fetchone()
            if current_revision_row is None:
                raise WorkflowAuthorityError("review submission lost its aggregate")
            current_revision = WorkflowRevision(
                WorkflowFence(plan.round.binding.rejection.fence.key, current_revision_row["generation"]),
                current_revision_row["version"],
            )
            require_review_submission_receipts(
                db, self._writer.repository, plan, claim_occurrence_id, current_revision,
            )
            return PendingCommand(_command_from_row(with_row), with_row["state"],
                                  with_row["attempts"], int(with_row["refusals"]))

        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review submission lacks its exact preflight")
            selection, _, plan, _ = stored
            prior = replay(db, plan, selection.claim_occurrence_id)
            if prior is not None:
                return prior
            claim = db.execute(
                "SELECT owner_pid,owner_start_ticks FROM dispatch_claims "
                "WHERE repository=? AND role='review' AND occurrence_id=?",
                (self._writer.repository, selection.claim_occurrence_id),
            ).fetchone()
            if claim is None:
                raise WorkflowAuthorityError("review parent claim occurrence is absent")
            parent_identity = claim["owner_pid"], claim["owner_start_ticks"]

        if not ProcessSupervisor.matches_identity(*parent_identity):
            raise WorkflowAuthorityError("review parent claim process is no longer live")

        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review submission lost its preflight")
            selection, _, plan, row = stored
            prior = replay(db, plan, selection.claim_occurrence_id)
            if prior is not None:
                return prior
            require_review_parent_claim(
                db, self._writer.repository, self._writer.owner_id,
                selection.claim_occurrence_id, selection.claim_target,
                probed_identity=parent_identity,
            )
            planning = decode_workflow(
                row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
            )
            if (not isinstance(planning, ReviewPlanningOpen)
                    or row["closure_json"] is not None or row["outcome_json"] is not None):
                raise WorkflowAuthorityError("review submission planning is already sealed")
            self._require_refused_submissions(db, plan)
            if (kind is ReviewSubmissionKind.COPIED and plan.stages
                    or kind is ReviewSubmissionKind.PAID and not plan.stages):
                raise WorkflowAuthorityError("review submission kind disagrees with planned work")
            snapshot = load_review_workflow_row(db, expected.fence.key)
            if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
                raise WorkflowAuthorityError("review execution workflow is absent or inactive")
            if snapshot.revision != expected:
                raise WorkflowRevisionConflict(snapshot.revision, "review submission aggregate version changed")
            if (snapshot.execution.binding != plan.round.binding
                    or snapshot.binding != plan.round.binding.rejection):
                raise WorkflowAuthorityError("review submission captured a stale dual binding")
            op = bound_submission_op(command)
            if (op is None or op.head != plan.head_sha
                    or command.workflow != plan.round.binding.rejection
                    or command.repository != self._writer.repository
                    or command.executor_identity != self._writer.owner_id
                    or command.target != f"pr:{expected.fence.key.target.number}"):
                raise WorkflowAuthorityError("review submission command disagrees with its round")
            if kind is ReviewSubmissionKind.COPIED and review_owed(snapshot, op.head, op.patch_id):
                raise WorkflowAuthorityError("copied review submission lacks a same-generation covered patch")
            if db.execute(
                "SELECT 1 FROM lifecycle_commands WHERE command_id=? OR idempotency_key=?",
                (submission.command_id, command.idempotency_key),
            ).fetchone() is not None:
                raise WorkflowAuthorityError("review submission command exists without its round registration")
            pending = self._control._enqueue_command(
                db, command, registering_review_submission=True,
            )
            updated = replace(plan, submissions=plan.submissions + (submission,))
            changed = db.execute(
                "UPDATE review_execution_rounds SET plan_json=? WHERE round_id=? "
                "AND plan_json=? AND planning_json=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(updated), round_id, row["plan_json"], row["planning_json"]),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("review round changed during submission planning")
            registration = ReviewSubmissionRegistration(
                plan.round, submission, selection.claim_occurrence_id, self._writer.owner_id,
                expected, digest, command.idempotency_key, command.config_version,
            )
            now = _stamp(self._control.now())
            for namespace, key in (
                (REVIEW_SUBMISSION_NAMESPACE, submission.command_id),
                (REVIEW_SUBMISSION_INDEX_NAMESPACE, review_submission_receipt_key(round_id, submission.command_id)),
            ):
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (namespace, self._writer.repository, key, now, encode_workflow(registration)),
                )
            after = replace(snapshot, revision=WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1))
            persist_workflow_snapshot(db, after)
            self._control._transition(db, "review_round", round_id, "submission_registered", {
                "command_id": submission.command_id, "kind": kind.value,
            })
            return pending

    def seal_completed_planning(
        self, round_id: str, expected: WorkflowRevision,
    ) -> ReviewPlanningSealed:
        """Close the parent's stage list without classifying child outcomes."""
        validate_workflow_identifier(round_id, "review round")
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("planning seal requires a captured aggregate revision")
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review planning seal lacks its preflight")
            selection, _, plan, row = stored
            prior = self._completed_seal_receipt(db, expected, selection, plan, row)
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

        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review planning seal lost its preflight")
            selection, _, plan, row = stored
            prior = self._completed_seal_receipt(db, expected, selection, plan, row)
            if prior is not None:
                return prior
            require_review_parent_claim(
                db, self._writer.repository, self._writer.owner_id, selection.claim_occurrence_id,
                selection.claim_target, probed_identity=(parent_pid, parent_start_ticks),
            )
            if not (plan.stages or plan.submissions):
                raise WorkflowAuthorityError("completed review planning has no registered work")
            snapshot = load_review_workflow_row(db, expected.fence.key)
            if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
                raise WorkflowAuthorityError("review execution workflow is absent or inactive")
            if snapshot.revision != expected:
                raise WorkflowRevisionConflict(snapshot.revision, "review planning seal aggregate version changed")
            if (snapshot.execution.binding != plan.round.binding
                    or snapshot.binding != plan.round.binding.rejection):
                raise WorkflowAuthorityError("review planning seal captured a stale dual binding")
            payload = encode_workflow((round_id, self._writer.owner_id, selection.claim_occurrence_id,
                                       expected, plan, ReviewPlanningComplete()))
            sealed = ReviewPlanningSealed(
                "review-round-seal:" + hashlib.sha256(payload.encode()).hexdigest(),
                ReviewPlanningComplete(),
            )
            changed = db.execute(
                "UPDATE review_execution_rounds SET planning_json=? WHERE round_id=? "
                "AND planning_json=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(sealed), round_id, row["planning_json"]),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("review round changed during planning seal")
            after = replace(snapshot, revision=WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1))
            persist_workflow_snapshot(db, after)
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_SEAL_NAMESPACE, self._writer.repository, round_id,
                 _stamp(self._control.now()), encode_workflow((
                     round_id, self._writer.owner_id, selection.claim_occurrence_id, expected, plan, sealed,
                 ))),
            )
            self._control._transition(db, "review_round", round_id, "planning_sealed", {
                "seal_receipt": sealed.seal_receipt,
            })
            return sealed

    def seal_abandoned_planning(self, round_id: str) -> ReviewParentAbandoned:
        """Fence a round after the exact parent claim occurrence terminates."""
        validate_workflow_identifier(round_id, "review round")
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._abandonment_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review abandonment lacks its preflight")
            parent_owner, selection, plan, row = stored
            planning = decode_workflow(
                row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
            )
            prior = verified_review_parent_abandonment(
                db, self._writer.repository, parent_owner,
                selection.claim_occurrence_id, selection.claim_target, plan, planning,
            )
            if prior is not None:
                return prior
            if (not isinstance(planning, ReviewPlanningOpen)
                    or row["closure_json"] is not None or row["outcome_json"] is not None):
                raise WorkflowAuthorityError("review planning already has a conclusion")
            abandoned = review_parent_abandonment(
                db, self._writer.repository, parent_owner,
                selection.claim_occurrence_id, selection.claim_target, plan,
            )
            changed = db.execute(
                "UPDATE review_execution_rounds SET planning_json=? WHERE round_id=? "
                "AND planning_json=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(abandoned), round_id, row["planning_json"]),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("review round changed during abandonment fence")
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_ABANDONMENT_NAMESPACE, self._writer.repository, round_id,
                 _stamp(self._control.now()), encode_workflow((
                     round_id, parent_owner, selection.claim_occurrence_id, plan, abandoned,
                 ))),
            )
            self._control._transition(db, "review_round", round_id, "planning_abandoned", {
                "claim_occurrence_id": selection.claim_occurrence_id,
                "abandonment_receipt": abandoned.abandonment_receipt,
            })
            return abandoned
