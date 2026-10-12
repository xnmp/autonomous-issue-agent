"""Transaction-local review execution preflight over a live parent claim."""

from dataclasses import dataclass, replace
import hashlib
import sqlite3

from ..domain.fix import breaker_key
from ..domain.contracts import CommandKind, LifecycleCommand
from ..domain.review_preflight import ReviewPreflightSelection, plan_review_preflight
from ..domain.review_execution import (
    ActivatedReviewExecution, CopiedReviewProgress, PaidReviewProgress, MAX_EXECUTION_SUFFIX,
    ReviewPreparationKind,
    ReviewRoundOutcome, same_execution_episode,
    CurrentReviewExecution, finalize_current_review_round,
)
from ..domain.review_rounds import (
    DeferredReviewPreparation, FinalizedReviewRound, ReviewParentAbandoned, ReviewPlanningComplete,
    ReviewPlanningConclusion, ReviewPlanningOpen, ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan, ReviewStagePlan,
    ReviewRoundAwaitingEvidence, ReviewSubmissionConfirmed, ReviewSubmissionKind, ReviewSubmissionPlan,
    ReviewSubmissionRegistration, finalize_review_round,
    CurrentReviewOutcomeReceipt,
    CurrentStaleReviewProductivitySettlement,
)
from ..domain.review_workflow import ObservedPullRequest, PullRequestState, RecordedPullRequest, ReviewWorkflowSnapshot
from ..domain.review_evidence import ReviewEvidenceKind
from ..domain.review_return import ReviewRuntimePreflightStop
from ..domain.review_workflow_policy import (
    OBSERVATION_MAX_AGE_SECONDS, CeilingProductivityDecision, review_owed,
)
from ..domain.review_rearm import ReleaseStatus
from ..domain.workflow_identity import (
    IssueRef, WorkflowFence, WorkflowKey, WorkflowRevision, WorkflowWriter, validate_workflow_identifier,
)
from .control_store import (
    ControlStoreError, PendingCommand, SQLiteControlStore, _command_from_row, _command_payload_digest,
    _dispatch_claim_issue, _json, _stamp,
)
from .deployment_authority import deployment_role
from .review_effects import ReviewReads, bound_submission_op, read_pull_request
from .review_dispatch_holds import review_release_projection, unresolved_review_submissions
from .review_execution_claims import (
    PREFLIGHT_NAMESPACE, load_review_preflight_record, require_review_parent_claim,
)
from .review_linkage import ReviewLinkedIssueUnavailable, review_linked_issue_holds
from .review_round_sources import read_review_round_sources
from .review_workflow_control import SQLiteReviewWorkflowControl
from .process_supervisor import ProcessSupervisor
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_ABANDONMENT_NAMESPACE, REVIEW_OUTCOME_NAMESPACE, REVIEW_PREPARATION_NAMESPACE,
    REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, REVIEW_STALE_PRODUCTIVITY_NAMESPACE,
    REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE,
    REVIEW_SEAL_NAMESPACE, REVIEW_STAGE_NAMESPACE,
    REVIEW_SUBMISSION_INDEX_NAMESPACE, REVIEW_SUBMISSION_NAMESPACE,
    WorkflowAuthorityError, WorkflowRevisionConflict, ReviewExecutionHeld,
    linked_issue_workflow_holds, load_review_workflow_row, persist_workflow_snapshot,
    read_review_submission_registration, require_review_submission_receipts,
    review_parent_abandonment, review_sealed_parent_abandonment,
    review_stage_receipt_key, review_submission_receipt_key,
    verified_review_completed_seal,
    verified_review_parent_abandonment,
    read_current_review_pending,
    read_review_outcome_receipt, require_current_review_productivity_receipt,
)


MAX_REVIEW_ROUND_SWEEP = 64
MAX_ABANDONMENT_WAIT_OBSERVATIONS = 8
MAX_ABANDONMENT_BLOCK_REASONS = 8
_START_CURSOR = (0, 0, "", 0)
_WAIT_NAMESPACE = "review-round-recovery-wait"
_BLOCK_NAMESPACE = "review-round-recovery-block"


@dataclass(frozen=True)
class ReviewRoundSweep:
    sealed: tuple[str, ...]
    finalized: tuple[str, ...]
    blocked: tuple[tuple[str, str], ...]
    has_more: bool


class SQLiteReviewExecutionControl:
    """Allocate one unpaid round, preserving its exact read and claim provenance."""

    def __init__(
        self, control: SQLiteControlStore, writer: WorkflowWriter, gateway: ReviewReads,
        *, preparation_control: SQLiteReviewWorkflowControl | None = None,
    ) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("review execution requires a typed control store and writer")
        if writer.role != "review" or deployment_role(writer.repository, writer.owner_id) != "review":
            raise ValueError("review execution requires the repository review owner")
        if not isinstance(gateway, ReviewReads):
            raise ValueError("review execution requires a read-only pull request gateway")
        if preparation_control is not None and (
            not isinstance(preparation_control, SQLiteReviewWorkflowControl)
            or preparation_control._control is not control or preparation_control.writer != writer
            or preparation_control._runtime_preflight is None
        ):
            raise ValueError("review preparation requires a local reader and the same execution authority")
        self._control = control
        self._writer = writer
        self._gateway = gateway
        self._preparation_control = preparation_control
        self._round_cursor = _START_CURSOR

    def _write_current_outcome_in_tx(
        self, db: sqlite3.Connection, facts: ReviewRoundFacts, expected: WorkflowRevision,
    ) -> FinalizedReviewRound | DeferredReviewPreparation:
        """Resolve exact receipts before current eligibility; publish atomically."""
        from ..domain.events import ControlTransitionCommitted, Correlation

        with self._control.mutation_savepoint(db):
            if not isinstance(facts, ReviewRoundFacts) or not isinstance(expected, WorkflowRevision):
                raise WorkflowAuthorityError("current outcome writer requires typed facts and revision")
            result = finalize_review_round(facts)
            if not isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation)):
                raise WorkflowAuthorityError("current outcome writer requires a terminal classified result")
            round = facts.plan.round
            if round.binding.rejection.fence.key.repository != self._writer.repository:
                raise WorkflowAuthorityError("current outcome belongs to another repository")
            receipt_payload = encode_workflow(CurrentReviewOutcomeReceipt(
                facts, result.outcome, expected, self._writer.owner_id,
            ))
            receipt = read_review_outcome_receipt(db, self._writer.repository, round.round_id)
            row = db.execute(
                "SELECT closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                (round.round_id,),
            ).fetchone()
            if receipt is not None:
                if not isinstance(receipt, CurrentReviewOutcomeReceipt):
                    raise WorkflowAuthorityError("current outcome retry requires its exact current writer receipt")
                recorded = receipt
                if (recorded.facts != facts or recorded.outcome != result.outcome or recorded.expected != expected
                        or row is None
                        or row["closure_json"] != encode_workflow(facts)
                        or row["outcome_json"] != encode_workflow(result.outcome)):
                    raise WorkflowAuthorityError("current outcome retry conflicts with its exact receipt")
                return result
            self._control._assert_owner_active(db, self._writer.owner_id)
            snapshot = load_review_workflow_row(db, round.binding.rejection.fence.key)
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
                raise WorkflowAuthorityError("current review execution is absent or inactive")
            if snapshot.revision != expected:
                raise WorkflowRevisionConflict(snapshot.revision, "current outcome captured revision is stale")
            stored = self._round_record(db, round.round_id)
            if stored is None or row is None or row["closure_json"] is not None or row["outcome_json"] is not None:
                raise WorkflowAuthorityError("current outcome lacks its open allocated round")
            parent_owner, selection, plan, source_row = stored
            planning = decode_workflow(
                source_row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
            )
            verified = read_review_round_sources(
                db, self._writer.repository, parent_owner, selection, plan, planning, snapshot.revision,
            )
            if verified != facts:
                raise WorkflowAuthorityError("current outcome differs from its actual durable source evidence")
            current = snapshot.execution
            if (not same_execution_episode(current.binding, round.binding)
                    or round.binding.rejection.fence.generation > current.binding.rejection.fence.generation):
                raise WorkflowAuthorityError("current outcome belongs to a foreign or future execution")
            stale = round.binding.rejection.fence.generation < current.binding.rejection.fence.generation
            if stale and round.sequence > current.generation_start_sequence:
                raise WorkflowAuthorityError("stale outcome exceeds its allocated generation cutoff")
            require_current_review_productivity_receipt(
                db, CurrentReviewOutcomeReceipt(facts, result.outcome, expected, self._writer.owner_id),
            )
            execution = current if stale else finalize_current_review_round(
                current, read_current_review_pending(db, current), result.outcome,
            ).execution
            if self._control._event_factory is None:
                raise WorkflowAuthorityError("current outcome requires a bound durable event outbox")
            changed = db.execute(
                "UPDATE review_execution_rounds SET closure_json=?,outcome_json=? "
                "WHERE round_id=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(facts), encode_workflow(result.outcome), round.round_id),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("current outcome round changed during finalization")
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_OUTCOME_NAMESPACE, self._writer.repository, round.round_id,
                 _stamp(self._control.now()), receipt_payload),
            )
            after = replace(snapshot, revision=WorkflowRevision(snapshot.revision.fence, expected.version + 1),
                            execution=execution)
            persist_workflow_snapshot(db, after)
            self._control.enqueue_event(self._control._event_factory(
                ControlTransitionCommitted("review_round", round.round_id, "outcome_finalized", {
                    "disposition": type(result.outcome.disposition).__name__, "sequence": round.sequence,
                }), Correlation(),
            ), db)
            return result

    def reconcile_review_rounds(self) -> ReviewRoundSweep:
        """Fence abandoned planning and finalize one indexed page of terminal rounds."""
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            rows = db.execute(
                "SELECT rr.round_id,rr.target_number,rr.execution_epoch,rr.execution_episode,rr.sequence,"
                "rr.planning_json,rr.closure_json,dc.occurrence_id AS live_claim, "
                "dc.target AS claim_target,dc.owner_id AS claim_owner,dc.owner_pid,dc.owner_start_ticks "
                "FROM review_execution_rounds rr LEFT JOIN dispatch_claims dc "
                "ON dc.repository=rr.repository AND dc.role='review' "
                "AND dc.occurrence_id=rr.claim_occurrence_id "
                "WHERE rr.repository=? AND rr.outcome_json IS NULL "
                "AND (rr.target_number,rr.execution_epoch,rr.execution_episode,rr.sequence)>(?,?,?,?) "
                "ORDER BY rr.target_number,rr.execution_epoch,rr.execution_episode,rr.sequence LIMIT ?",
                (self._writer.repository, *self._round_cursor, MAX_REVIEW_ROUND_SWEEP + 1),
            ).fetchall()
        page = rows[:MAX_REVIEW_ROUND_SWEEP]
        sealed: list[str] = []
        finalized: list[str] = []
        blocked: list[tuple[str, str]] = []
        for row in page:
            if row["closure_json"] is not None:
                reason = "closed review round is missing its outcome"
                blocked.append((row["round_id"], reason))
                self._record_recovery_block(row["round_id"], reason)
                continue
            round_id = row["round_id"]
            open_planning = row["planning_json"] == encode_workflow(ReviewPlanningOpen())
            claim_present = row["live_claim"] is not None
            if claim_present:
                parent_identity = (row["owner_pid"], row["owner_start_ticks"])
                parent_live = ProcessSupervisor.matches_identity(*parent_identity)
                if not parent_live:
                    target = _dispatch_claim_issue(row["claim_target"])
                    if target is None:
                        reason = "review parent claim target is malformed"
                        blocked.append((round_id, reason))
                        self._record_recovery_block(round_id, reason)
                        continue
                    # The dead identity is sampled outside SQLite's write lock;
                    # a replacement occurrence cannot be released by this sample.
                    claim_present = not self._control.release_dispatch_claim(
                        self._writer.repository, "review", target, row["claim_owner"],
                        expected_occurrence_id=row["live_claim"], expected_process_identity=parent_identity,
                    )
            if open_planning and claim_present:
                continue
            try:
                planning = (
                    ReviewPlanningOpen() if open_planning else decode_workflow(
                        row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
                    )
                )
                if open_planning or (isinstance(planning, ReviewPlanningSealed) and not claim_present):
                    planning = self.seal_abandoned_planning(round_id)
                    sealed.append(round_id)
                result = (
                    self.finalize_completed_round(round_id)
                    if isinstance(planning, ReviewPlanningSealed)
                    else self.finalize_abandoned_round(round_id)
                )
                if isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation)):
                    finalized.append(round_id)
                elif self._recovery_wait_at_ceiling(round_id):
                    blocked.append((round_id, "round evidence remains pending after bounded recovery observations"))
            except (ValueError, ControlStoreError) as exc:
                blocked.append((round_id, str(exc)))
                self._record_recovery_block(round_id, str(exc))
        self._round_cursor = (
            (page[-1]["target_number"], page[-1]["execution_epoch"],
             page[-1]["execution_episode"], page[-1]["sequence"])
            if len(rows) > MAX_REVIEW_ROUND_SWEEP else _START_CURSOR
        )
        return ReviewRoundSweep(tuple(sealed), tuple(finalized), tuple(blocked),
                                len(rows) > MAX_REVIEW_ROUND_SWEEP)

    def _recovery_wait_at_ceiling(self, round_id: str) -> bool:
        with self._control.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (_WAIT_NAMESPACE, self._writer.repository, round_id),
            ).fetchone()
            if row is None:
                return False
            return self._wait_at_ceiling(row["payload_json"])

    @staticmethod
    def _wait_at_ceiling(payload: str) -> bool:
        recorded = decode_workflow(payload, tuple)
        if (len(recorded) != 4 or not isinstance(recorded[0], str)
                or not isinstance(recorded[1], tuple)
                or any(not isinstance(reason, str) for reason in recorded[1])
                or not isinstance(recorded[2], str)
                or type(recorded[3]) is not int
                or not 1 <= recorded[3] <= MAX_ABANDONMENT_WAIT_OBSERVATIONS):
            raise WorkflowAuthorityError("review recovery wait observation is malformed")
        return recorded[3] == MAX_ABANDONMENT_WAIT_OBSERVATIONS

    @staticmethod
    def _pending_recovery_hold_in_tx(db: sqlite3.Connection, execution: CurrentReviewExecution) -> str | None:
        binding = execution.binding
        key = binding.rejection.fence.key
        rows = db.execute(
            "SELECT rr.round_id,d.payload_json FROM review_execution_rounds rr "
            "INDEXED BY review_execution_rounds_generation_pending JOIN dedupe_keys d "
            "ON d.namespace=? AND d.scope=rr.repository AND d.key=rr.round_id "
            "WHERE rr.repository=? AND rr.target_number=? AND rr.execution_epoch=? "
            "AND rr.execution_episode=? AND rr.generation=? AND rr.sequence>? AND rr.sequence<=? "
            "AND rr.outcome_json IS NULL ORDER BY rr.sequence LIMIT ?",
            (_WAIT_NAMESPACE, key.repository, key.target.number, binding.execution_epoch,
             binding.execution_episode, binding.rejection.fence.generation,
             execution.baseline.through_sequence, execution.last_sequence, MAX_EXECUTION_SUFFIX),
        ).fetchall()
        return next((f"unfinished review round reached recovery limit: {row['round_id']}"
                     for row in rows if SQLiteReviewExecutionControl._wait_at_ceiling(row["payload_json"])), None)

    def pending_recovery_hold(self, key: WorkflowKey) -> str | None:
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            snapshot = load_review_workflow_row(db, key)
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
                return None
            return self._pending_recovery_hold_in_tx(db, snapshot.execution)

    def _record_recovery_block(self, round_id: str, reason: str) -> None:
        with self._control.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (_BLOCK_NAMESPACE, self._writer.repository, round_id),
            ).fetchone()
            seen = decode_workflow(row["payload_json"], tuple) if row is not None else ()
            if (not isinstance(seen, tuple) or len(seen) > MAX_ABANDONMENT_BLOCK_REASONS
                    or any(not isinstance(item, str) or len(item) != 64 for item in seen)):
                raise WorkflowAuthorityError("review recovery block observation is malformed")
            digest = hashlib.sha256(reason.encode()).hexdigest()
            if digest in seen or len(seen) == MAX_ABANDONMENT_BLOCK_REASONS:
                return
            payload = encode_workflow((*seen, digest))
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,"
                "payload_json=excluded.payload_json",
                (_BLOCK_NAMESPACE, self._writer.repository, round_id,
                 _stamp(self._control.now()), payload),
            )
            self._control._transition(db, "review_round", round_id, "recovery_blocked", {
                "reason": reason[:480],
            })

    @staticmethod
    def _pending_progress_digest(db: sqlite3.Connection, facts: ReviewRoundFacts) -> tuple[str, bool]:
        child_rows = tuple(tuple(row) for row in db.execute(
            "SELECT r.review_child_id,r.run_id,r.state,r.outcome,r.started_at,r.completed_at,"
            "b.state,rr.policy,l.run_id,c.provider_receipt_id FROM runs r "
            "JOIN budget_reservations b USING(run_id) JOIN reservation_recovery rr USING(run_id) "
            "LEFT JOIN leases l USING(run_id) LEFT JOIN charges c USING(run_id) "
            "WHERE r.review_round_id=? ORDER BY r.review_child_id",
            (facts.plan.round.round_id,),
        ))
        command_rows = tuple(
            tuple(row) if row is not None else None
            for submission in facts.plan.submissions
            for row in (db.execute(
                "SELECT c.state,c.attempts,c.attempt_owner,e.status,e.receipt_digest "
                "FROM lifecycle_commands c LEFT JOIN effect_receipts e USING(command_id) "
                "WHERE c.command_id=?", (submission.command_id,),
            ).fetchone(),)
        )
        live_child = any(row[2] == "running" and row[8] is not None for row in child_rows)
        return hashlib.sha256(encode_workflow((child_rows, command_rows)).encode()).hexdigest(), live_child

    def _record_recovery_wait(
        self, db: sqlite3.Connection, facts: ReviewRoundFacts, waiting: ReviewRoundAwaitingEvidence,
    ) -> None:
        round_id = facts.plan.round.round_id
        reasons = tuple(sorted(reason.value for reason in waiting.reasons))
        progress, live_child = self._pending_progress_digest(db, facts)
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (_WAIT_NAMESPACE, self._writer.repository, round_id),
        ).fetchone()
        prior = decode_workflow(row["payload_json"], tuple) if row is not None else None
        if prior is not None and (
            len(prior) != 4 or not isinstance(prior[0], str)
            or not isinstance(prior[1], tuple)
            or any(not isinstance(reason, str) for reason in prior[1])
            or not isinstance(prior[2], str)
            or type(prior[3]) is not int
            or not 1 <= prior[3] <= MAX_ABANDONMENT_WAIT_OBSERVATIONS
        ):
            raise WorkflowAuthorityError("review recovery wait observation is malformed")
        same = prior is not None and prior[:3] == (facts.closure_receipt, reasons, progress)
        attempts = min(prior[3] + 1, MAX_ABANDONMENT_WAIT_OBSERVATIONS) if same and not live_child else 1
        if same and attempts == prior[3]:
            return
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
            "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,"
            "payload_json=excluded.payload_json",
            (_WAIT_NAMESPACE, self._writer.repository, round_id,
             _stamp(self._control.now()), encode_workflow((facts.closure_receipt, reasons, progress, attempts))),
        )
        if attempts in (1, MAX_ABANDONMENT_WAIT_OBSERVATIONS):
            self._control._transition(db, "review_round", round_id, "evidence_wait", {
                "reasons": reasons, "observations": attempts,
            })

    def read_round_facts(self, round_id: str) -> ReviewRoundFacts:
        """Join one round's source receipts under a single SQLite snapshot."""
        validate_workflow_identifier(round_id, "review round")
        with self._control.transaction() as db:
            return self._read_round_facts(db, round_id)

    def _read_round_facts(self, db: sqlite3.Connection, round_id: str) -> ReviewRoundFacts:
        stored = self._round_record(db, round_id)
        if stored is None:
            raise WorkflowAuthorityError("review round lacks its preflight")
        parent_owner, selection, plan, row = stored
        planning = decode_workflow(
            row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
        )
        key = plan.round.binding.rejection.fence.key
        aggregate = db.execute(
            "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' AND target_number=?",
            (key.repository, key.target.number),
        ).fetchone()
        if aggregate is None:
            raise WorkflowAuthorityError("review round lost its aggregate")
        revision = WorkflowRevision(WorkflowFence(key, aggregate["generation"]), aggregate["version"])
        return read_review_round_sources(
            db, self._writer.repository, parent_owner, selection, plan, planning, revision,
        )

    def finalize_abandoned_round(
        self, round_id: str,
    ) -> ReviewRoundAwaitingEvidence | FinalizedReviewRound | DeferredReviewPreparation:
        """Settle a nonproductive abandoned round and its execution projection atomically."""
        return self._finalize_nonproductive_round(round_id, ReviewParentAbandoned)

    def finalize_completed_round(
        self, round_id: str,
    ) -> ReviewRoundAwaitingEvidence | FinalizedReviewRound | DeferredReviewPreparation:
        """Settle a sealed, nonproductive round from its durable sources."""
        return self._finalize_nonproductive_round(round_id, ReviewPlanningSealed)

    def _finalize_nonproductive_round(
        self, round_id: str,
        planning_type: type[ReviewParentAbandoned] | type[ReviewPlanningSealed],
    ) -> ReviewRoundAwaitingEvidence | FinalizedReviewRound | DeferredReviewPreparation:
        validate_workflow_identifier(round_id, "review round")
        with self._control.transaction() as db:
            stored = self._round_record(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review round lacks its preflight")
            _, _, plan, row = stored
            facts = self._read_round_facts(db, round_id)
            result = finalize_review_round(facts)
            receipt = read_review_outcome_receipt(db, self._writer.repository, round_id)
            if receipt is not None:
                if not isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation)):
                    raise WorkflowAuthorityError("review round replay lost its terminal evidence")
                recorded = (receipt.facts, receipt.outcome) if isinstance(receipt, CurrentReviewOutcomeReceipt) else receipt
                if (not isinstance(facts.planning, planning_type)
                        or row["closure_json"] != encode_workflow(facts)
                        or row["outcome_json"] != encode_workflow(result.outcome)
                        or recorded != (facts, result.outcome)):
                    raise WorkflowAuthorityError("review round replay changed its sealed outcome")
                return result
            self._control._assert_owner_active(db, self._writer.owner_id)
            try:
                snapshot = load_review_workflow_row(db, plan.round.binding.rejection.fence.key)
            except ReviewExecutionHeld as held:
                if isinstance(result, ReviewRoundAwaitingEvidence):
                    self._record_recovery_wait(db, facts, result)
                    return result
                if not isinstance(facts.planning, planning_type):
                    raise WorkflowAuthorityError("review round planning has a different settlement path")
                return self._finalize_held_round_in_tx(db, held.snapshot, facts, result)
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
                raise WorkflowAuthorityError("review execution workflow is absent or inactive")
            if isinstance(result, ReviewRoundAwaitingEvidence):
                if isinstance(facts.planning, (ReviewParentAbandoned, ReviewPlanningSealed)):
                    self._record_recovery_wait(db, facts, result)
                return result
            if not isinstance(facts.planning, planning_type):
                raise WorkflowAuthorityError("review round planning has a different settlement path")
            preparation_error = (
                self._runtime_preparation_error(db, plan, result)
                if isinstance(result, DeferredReviewPreparation) else None
            )
            if not isinstance(result, (FinalizedReviewRound, DeferredReviewPreparation)):
                raise WorkflowAuthorityError("review preparation requires its bounded schedule")
            if isinstance(snapshot.execution, CurrentReviewExecution):
                return self._finalize_current_nonproductive_in_tx(
                    db, snapshot, facts, result, preparation_error,
                )
    def _finalize_held_round_in_tx(
        self, db: sqlite3.Connection, held: ReviewWorkflowSnapshot, facts: ReviewRoundFacts,
        result: FinalizedReviewRound | DeferredReviewPreparation,
    ) -> FinalizedReviewRound | DeferredReviewPreparation:
        """Retain exact terminal evidence without granting or changing prior authority."""
        from ..domain.events import ControlTransitionCommitted, Correlation

        if (not isinstance(held.execution, ActivatedReviewExecution)
                or not same_execution_episode(held.execution.binding, facts.plan.round.binding)
                or facts.plan.round.sequence > held.execution.last_sequence
                or facts.plan.round.binding.rejection.fence.generation > held.revision.fence.generation):
            raise WorkflowAuthorityError("held round belongs to a foreign or future component")
        with self._control.mutation_savepoint(db):
            if self._control._event_factory is None:
                raise WorkflowAuthorityError("held settlement requires a bound durable event outbox")
            changed = db.execute(
                "UPDATE review_execution_rounds SET closure_json=?,outcome_json=? "
                "WHERE round_id=? AND closure_json IS NULL AND outcome_json IS NULL",
                (encode_workflow(facts), encode_workflow(result.outcome), facts.plan.round.round_id),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("held round changed during settlement")
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_OUTCOME_NAMESPACE, self._writer.repository, facts.plan.round.round_id,
                 _stamp(self._control.now()), encode_workflow((facts, result.outcome))),
            )
            if isinstance(result.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
                confirmed = tuple(item.plan.command_id for item in facts.submissions
                                  if isinstance(item.result, ReviewSubmissionConfirmed))
                if len(confirmed) != 1:
                    raise WorkflowAuthorityError("held productive round lacks one confirmed submission")
                db.execute(
                    "INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    ("review-submission-settled", self._writer.repository, confirmed[0],
                     _stamp(self._control.now()), '{"outcome":"refused: held prior execution"}'),
                )
            if isinstance(result, DeferredReviewPreparation):
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (REVIEW_PREPARATION_NAMESPACE, self._writer.repository, facts.plan.round.round_id,
                     _stamp(self._control.now()), encode_workflow((facts, result.failures))),
                )
            self._control.enqueue_event(self._control._event_factory(
                ControlTransitionCommitted("review_round", facts.plan.round.round_id, "held_outcome_finalized", {
                    "disposition": type(result.outcome.disposition).__name__, "sequence": facts.plan.round.sequence,
                }), Correlation(),
            ), db)
            return result

    def _finalize_current_nonproductive_in_tx(
        self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot, facts: ReviewRoundFacts,
        result: FinalizedReviewRound | DeferredReviewPreparation, preparation_error: str | None,
    ) -> FinalizedReviewRound | DeferredReviewPreparation:
        with self._control.mutation_savepoint(db):
            round = facts.plan.round
            receipt = read_review_outcome_receipt(db, self._writer.repository, round.round_id)
            if receipt is not None:
                if not isinstance(receipt, CurrentReviewOutcomeReceipt):
                    raise WorkflowAuthorityError("current finalized round lacks its current exact receipt")
                return self._write_current_outcome_in_tx(db, facts, receipt.expected)
            if isinstance(result.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
                confirmed = tuple(item.plan.command_id for item in facts.submissions
                                  if isinstance(item.result, ReviewSubmissionConfirmed))
                if len(confirmed) != 1:
                    raise WorkflowAuthorityError("productive review lacks one confirmed submission")
                stale = round.binding.rejection.fence.generation < snapshot.revision.fence.generation
                self._settle_productive_in_tx(db, confirmed[0], snapshot, stale=stale)
                return result
            if isinstance(result, DeferredReviewPreparation):
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (REVIEW_PREPARATION_NAMESPACE, self._writer.repository, round.round_id,
                     _stamp(self._control.now()), encode_workflow((facts, result.failures))),
                )
            accepted = self._write_current_outcome_in_tx(db, facts, snapshot.revision)
            if isinstance(result, DeferredReviewPreparation):
                assert preparation_error is not None and self._preparation_control is not None
                if snapshot.revision.fence.generation == round.binding.rejection.fence.generation:
                    self._preparation_control._record_evidence_in_tx(
                        db, round.binding.rejection.fence.key, facts.plan.head_sha,
                        ReviewEvidenceKind.RUNTIME_PREPARATION, error=preparation_error,
                    )
                else:
                    self._control._transition(db, "review_round", round.round_id, "preparation_superseded", {
                        "settled_generation": snapshot.revision.fence.generation,
                    })
            return accepted

    def _settle_productive_in_tx(
        self, db: sqlite3.Connection, command_id: str, after_rejection: ReviewWorkflowSnapshot,
        *, stale: bool = False, superseded_evidence_id: str | None = None,
        ceiling: CeilingProductivityDecision | None = None,
    ) -> None:
        """Join a confirmed verdict to its sealed round in the baseline's SQL transaction."""
        with self._control.mutation_savepoint(db):
            self._settle_productive_sources_in_tx(
                db, command_id, after_rejection, stale=stale,
                superseded_evidence_id=superseded_evidence_id, ceiling=ceiling,
            )

    def _settle_productive_sources_in_tx(
        self, db: sqlite3.Connection, command_id: str, after_rejection: ReviewWorkflowSnapshot,
        *, stale: bool = False, superseded_evidence_id: str | None = None,
        ceiling: CeilingProductivityDecision | None = None,
    ) -> None:
        self._control._assert_owner_active(db, self._writer.owner_id)
        registration = read_review_submission_registration(db, self._writer.repository, command_id)
        if registration is None:
            raise WorkflowAuthorityError("active review verdict lacks its originating round")
        stored = self._round_record(db, registration.round.round_id)
        if stored is None:
            raise WorkflowAuthorityError("review verdict lost its originating round")
        parent_owner, selection, plan, row = stored
        settled_at = self._control.now()
        if (plan.round != registration.round or registration.submission not in plan.submissions
                or not isinstance(after_rejection.execution, CurrentReviewExecution)
                or not same_execution_episode(after_rejection.execution.binding, plan.round.binding)
                or plan.round.binding.rejection.fence.generation > after_rejection.revision.fence.generation
                or (stale and plan.round.binding.rejection.fence.generation
                    >= after_rejection.revision.fence.generation)):
            raise WorkflowAuthorityError("review verdict round disagrees with active execution")
        if ceiling is not None and (
            stale or superseded_evidence_id is not None or ceiling.escalation.escalate_cause != "ceiling"
            or str(ceiling.escalation.submission.command_id) != command_id
            or ceiling.escalation.submission.binding != plan.round.binding.rejection
            or plan.round.binding.rejection.fence != after_rejection.revision.fence
        ):
            raise WorkflowAuthorityError("ceiling review verdict disagrees with its round binding")
        if ceiling is not None:
            evidence = db.execute(
                "SELECT payload_json FROM workflow_evidence WHERE evidence_id=? AND repository=? "
                "AND target_kind='pr' AND target_number=? AND kind='observation'",
                (ceiling.observation_evidence_id, self._writer.repository,
                 plan.round.binding.rejection.fence.key.target.number),
            ).fetchone()
            if evidence is None:
                raise WorkflowAuthorityError("ceiling review verdict lacks its fresh observation")
            observed = decode_workflow(evidence["payload_json"], RecordedPullRequest)
            age = (settled_at - observed.observation.observed_at).total_seconds()
            if not 0 <= age <= OBSERVATION_MAX_AGE_SECONDS:
                raise WorkflowAuthorityError("ceiling review observation expired before settlement")
        if superseded_evidence_id is not None:
            evidence = db.execute(
                "SELECT payload_json FROM workflow_evidence WHERE evidence_id=? AND repository=? "
                "AND target_kind='pr' AND target_number=? AND kind='observation'",
                (superseded_evidence_id, self._writer.repository,
                 plan.round.binding.rejection.fence.key.target.number),
            ).fetchone()
            if (stale or plan.round.binding.rejection.fence != after_rejection.revision.fence
                    or evidence is None):
                raise WorkflowAuthorityError("superseded review verdict lacks current observation authority")
            observed = decode_workflow(evidence["payload_json"], RecordedPullRequest)
            observation_age = (settled_at - observed.observation.observed_at).total_seconds()
            if (observed.observation.key != after_rejection.revision.fence.key
                    or (observed.observation.head_sha == plan.head_sha
                        and observed.observation.state is PullRequestState.OPEN)
                    or not 0 <= observation_age <= OBSERVATION_MAX_AGE_SECONDS):
                raise WorkflowAuthorityError("review verdict was not superseded by a changed pull request")
        if row["outcome_json"] is not None or row["closure_json"] is not None:
            raise WorkflowAuthorityError("review verdict round already has an outcome")
        planning = decode_workflow(
            row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
        )
        facts = read_review_round_sources(
            db, self._writer.repository, parent_owner, selection, plan, planning,
            after_rejection.revision,
        )
        result = finalize_review_round(facts)
        if (not isinstance(result, FinalizedReviewRound)
                or not isinstance(result.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress))
                or not any(submission.plan.command_id == command_id
                           and isinstance(submission.result, ReviewSubmissionConfirmed)
                           for submission in facts.submissions)):
            raise WorkflowAuthorityError("review verdict has no sealed, settled productive round")
        if isinstance(after_rejection.execution, CurrentReviewExecution):
            if load_review_workflow_row(db, plan.round.binding.rejection.fence.key) != after_rejection:
                raise WorkflowAuthorityError("current productive settlement changed its captured workflow")
            receipt = CurrentReviewOutcomeReceipt(
                facts, result.outcome, after_rejection.revision, self._writer.owner_id,
            )
            if stale:
                submission = next(item for item in facts.submissions if item.plan.command_id == command_id)
                assert isinstance(submission.result, ReviewSubmissionConfirmed)
                witness = CurrentStaleReviewProductivitySettlement(
                    receipt, after_rejection.execution.binding, command_id, submission.result.receipt_id,
                )
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (REVIEW_STALE_PRODUCTIVITY_NAMESPACE, self._writer.repository, plan.round.round_id,
                     _stamp(settled_at), encode_workflow(witness)),
                )
            for namespace, marker in (
                (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, superseded_evidence_id),
                (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, ceiling),
            ):
                if marker is not None:
                    db.execute(
                        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                        (namespace, self._writer.repository, plan.round.round_id, _stamp(settled_at),
                         encode_workflow((marker, after_rejection.revision, settled_at.isoformat()))),
                    )
            self._write_current_outcome_in_tx(db, facts, after_rejection.revision)
            return
    def _runtime_preparation_error(
        self, db: sqlite3.Connection, plan: ReviewRoundPlan, result: DeferredReviewPreparation,
    ) -> str:
        if self._preparation_control is None:
            raise WorkflowAuthorityError("review preparation has no bounded evidence controller")
        summaries: list[str] = []
        prefix = "review-execution-preflight-stop:"
        for failure in result.failures:
            if failure.kind is not ReviewPreparationKind.RUNTIME or not failure.evidence_id.startswith(prefix):
                raise WorkflowAuthorityError("review preparation kind lacks a verified reread path")
            run_id = failure.evidence_id[len(prefix):]
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-preflight-stop' "
                "AND scope=? AND key=?", (self._writer.repository, run_id),
            ).fetchone()
            if row is None:
                raise WorkflowAuthorityError("review runtime preparation lost its exact stop receipt")
            stopped = decode_workflow(row["payload_json"], ReviewRuntimePreflightStop)
            if (stopped.receipt_id != failure.evidence_id or stopped.admission.child.round != plan.round
                    or not stopped.result.summary):
                raise WorkflowAuthorityError("review runtime preparation disagrees with its round")
            summaries.append(stopped.result.summary)
        return "; ".join(dict.fromkeys(summaries))[:512]

    def _stored_round(
        self, db: sqlite3.Connection, request_id: str,
    ) -> tuple[ReviewPreflightSelection, ReviewRoundPlan, ReviewRoundPlan, sqlite3.Row] | None:
        return load_review_preflight_record(db, self._writer.repository, self._writer.owner_id, request_id)

    def _round_record(
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
            raise WorkflowAuthorityError("review round preflight has no captured owner")
        owner_id = captured[1]
        if owner_id not in self._control._owner_lineage(db, self._writer.repository, self._writer.owner_id):
            raise WorkflowAuthorityError("review round owner is outside this deployment lineage")
        stored = load_review_preflight_record(db, self._writer.repository, owner_id, round_id)
        if stored is None:
            raise WorkflowAuthorityError("review round lost its preflight")
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
            if not isinstance(snapshot.execution, CurrentReviewExecution):
                raise WorkflowAuthorityError("review execution is inactive")
            recovery_hold = self._pending_recovery_hold_in_tx(db, snapshot.execution)
            if recovery_hold is not None:
                raise WorkflowAuthorityError(recovery_hold)
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
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
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
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
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
        conclusion: ReviewPlanningConclusion = ReviewPlanningComplete(),
    ) -> ReviewPlanningSealed:
        """Close the parent's stage list without classifying child outcomes."""
        validate_workflow_identifier(round_id, "review round")
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("planning seal requires a captured aggregate revision")
        ReviewPlanningSealed("validation", conclusion)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review planning seal lacks its preflight")
            selection, _, plan, row = stored
            prior = self._completed_seal_receipt(db, expected, selection, plan, row)
            if prior is not None:
                if prior.conclusion != conclusion:
                    raise WorkflowAuthorityError("review planning seal has a conflicting conclusion")
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

        with self._control.transaction() as db, self._control.mutation_savepoint(db):
            self._control._assert_owner_active(db, self._writer.owner_id)
            stored = self._stored_round(db, round_id)
            if stored is None:
                raise WorkflowAuthorityError("review planning seal lost its preflight")
            selection, _, plan, row = stored
            prior = self._completed_seal_receipt(db, expected, selection, plan, row)
            if prior is not None:
                if prior.conclusion != conclusion:
                    raise WorkflowAuthorityError("review planning seal has a conflicting conclusion")
                return prior
            require_review_parent_claim(
                db, self._writer.repository, self._writer.owner_id, selection.claim_occurrence_id,
                selection.claim_target, probed_identity=(parent_pid, parent_start_ticks),
            )
            if isinstance(conclusion, ReviewPlanningComplete) and not (plan.stages or plan.submissions):
                raise WorkflowAuthorityError("completed review planning has no registered work")
            snapshot = load_review_workflow_row(db, expected.fence.key)
            if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
                raise WorkflowAuthorityError("review execution workflow is absent or inactive")
            if snapshot.revision != expected:
                raise WorkflowRevisionConflict(snapshot.revision, "review planning seal aggregate version changed")
            if (snapshot.execution.binding != plan.round.binding
                    or snapshot.binding != plan.round.binding.rejection):
                raise WorkflowAuthorityError("review planning seal captured a stale dual binding")
            payload = encode_workflow((round_id, self._writer.owner_id, selection.claim_occurrence_id,
                                       expected, plan, conclusion))
            sealed = ReviewPlanningSealed(
                "review-round-seal:" + hashlib.sha256(payload.encode()).hexdigest(),
                conclusion,
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
            stored = self._round_record(db, round_id)
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
            if (not isinstance(planning, (ReviewPlanningOpen, ReviewPlanningSealed))
                    or row["closure_json"] is not None or row["outcome_json"] is not None):
                raise WorkflowAuthorityError("review planning already has a conclusion")
            abandoned = (
                review_sealed_parent_abandonment(
                    db, self._writer.repository, parent_owner,
                    selection.claim_occurrence_id, selection.claim_target, plan, planning,
                )
                if isinstance(planning, ReviewPlanningSealed)
                else review_parent_abandonment(
                    db, self._writer.repository, parent_owner,
                    selection.claim_occurrence_id, selection.claim_target, plan,
                )
            )
            with self._control.mutation_savepoint(db):
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
