"""Transaction-local reconstruction of one review round from durable source rows."""

import hashlib
import sqlite3

from ..domain.contracts import CommandId, EffectReceipt, EffectStatus
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.review_rounds import (
    ReviewChildNotAdmitted, ReviewChildPending, ReviewParentAbandoned,
    ReviewPlanningComplete, ReviewPlanningOpen, ReviewPlanningSealed, ReviewRoundFacts, ReviewRoundPlan,
    ReviewSubmissionConfirmed, ReviewSubmissionEvidence, ReviewSubmissionPending,
    ReviewSubmissionRefused,
)
from ..domain.workflow_identity import WorkflowRevision
from .control_store import SQLiteControlStore, _indexed_review_child_admission, _parse_stamp, _receipt_digest
from .workflow_codec import encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, require_review_submission_receipts,
    verified_review_completed_seal, verified_review_parent_abandonment,
)


def read_review_round_sources(
    db: sqlite3.Connection, repository: str, parent_owner: str,
    selection: ReviewPreflightSelection, plan: ReviewRoundPlan,
    planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
    revision: WorkflowRevision,
) -> ReviewRoundFacts:
    """Use the caller's SQL snapshot; never infer a terminal result from absence alone."""
    verified_review_completed_seal(
        db, repository, parent_owner, selection.claim_occurrence_id, plan, planning,
    )
    verified_review_parent_abandonment(
        db, repository, parent_owner, selection.claim_occurrence_id,
        selection.claim_target, plan, planning,
    )
    require_review_submission_receipts(
        db, repository, plan, selection.claim_occurrence_id, revision,
    )
    children = SQLiteControlStore._read_planned_review_child_evidence(
        db, repository, parent_owner, selection, plan, revision,
    )
    if isinstance(planning, ReviewParentAbandoned):
        children = tuple(
            ReviewChildNotAdmitted(item.child, planning.abandonment_receipt)
            if isinstance(item, ReviewChildPending) and _indexed_review_child_admission(
                db, repository, item.child,
            ) is None else item
            for item in children
        )
    elif isinstance(planning, ReviewPlanningSealed) and not isinstance(planning.conclusion, ReviewPlanningComplete):
        children = tuple(
            ReviewChildNotAdmitted(item.child, planning.seal_receipt)
            if isinstance(item, ReviewChildPending) and _indexed_review_child_admission(
                db, repository, item.child,
            ) is None else item
            for item in children
        )
    submissions: list[ReviewSubmissionEvidence] = []
    for submission in plan.submissions:
        command = db.execute(
            "SELECT state,attempts,attempt_owner FROM lifecycle_commands WHERE command_id=?",
            (submission.command_id,),
        ).fetchone()
        receipt = db.execute(
            "SELECT * FROM effect_receipts WHERE command_id=?",
            (submission.command_id,),
        ).fetchone()
        if command is None:
            raise WorkflowAuthorityError("planned review submission lost its command")
        if receipt is not None:
            try:
                source_receipt = EffectReceipt(
                    CommandId(submission.command_id), receipt["executor_identity"],
                    _parse_stamp(receipt["started_at"]), _parse_stamp(receipt["completed_at"]),
                    EffectStatus(receipt["status"]), receipt["remote_object_id"],
                    receipt["remote_revision"], receipt["error"],
                )
            except (TypeError, ValueError) as exc:
                raise WorkflowAuthorityError("review submission effect receipt is malformed") from exc
            if (command["attempt_owner"] != source_receipt.executor_identity
                    or _receipt_digest(source_receipt) != receipt["receipt_digest"]):
                raise WorkflowAuthorityError("review submission effect receipt changed after settlement")
        state = command["state"]
        if state == EffectStatus.SUCCEEDED.value:
            if (receipt is None or receipt["status"] != state
                    or not receipt["remote_object_id"] or command["attempts"] < 1):
                raise WorkflowAuthorityError("confirmed review submission lacks its effect receipt")
            result = ReviewSubmissionConfirmed(_effect_receipt_id(submission.command_id, receipt["receipt_digest"]))
        elif state == EffectStatus.PERMANENT_FAILURE.value:
            if (receipt is None or receipt["status"] != state
                    or receipt["remote_object_id"] is not None or command["attempts"] < 1):
                raise WorkflowAuthorityError("refused review submission lacks its effect receipt")
            result = ReviewSubmissionRefused(_effect_receipt_id(submission.command_id, receipt["receipt_digest"]))
        else:
            if (receipt is not None and receipt["status"] in (
                    EffectStatus.SUCCEEDED.value, EffectStatus.PERMANENT_FAILURE.value)):
                raise WorkflowAuthorityError("pending review submission contradicts its effect receipt")
            result = ReviewSubmissionPending()
        submissions.append(ReviewSubmissionEvidence(submission, result))
    source = (plan, planning, children, tuple(submissions))
    closure_receipt = "review-round-closure:" + hashlib.sha256(encode_workflow(source).encode()).hexdigest()
    return ReviewRoundFacts(closure_receipt, plan, planning, children, tuple(submissions))


def _effect_receipt_id(command_id: str, receipt_digest: str) -> str:
    return "effect-receipt:" + hashlib.sha256(
        encode_workflow((command_id, receipt_digest)).encode(),
    ).hexdigest()
