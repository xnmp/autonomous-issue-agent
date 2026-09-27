"""Transaction-local review dispatch holds shared by admission and preflight."""

import sqlite3

from ..domain.contracts import CommandId, CommandKind, EffectStatus
from ..domain.review_rearm import ReleaseProjection, ReleaseStatus
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.review_workflow_policy import review_release_command_id
from ..domain.workflow import RetiredSettledCommand, RetiredUncertainCommand
from ..domain.workflow_identity import WorkflowBinding, WorkflowWriter
from .control_store import SQLiteControlStore, _command_from_row
from .workflow_commit import owns_command
from .workflow_rows import WorkflowAuthorityError

RECORDED_REVIEW_NAMESPACE = "review-recorded-submission"
RELEASE_RESOLVED_NAMESPACE = "review-release-resolved"
SETTLED_SUBMISSION_NAMESPACE = "review-submission-settled"


def review_submission_settled(db: sqlite3.Connection, repository: str, command_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM dedupe_keys WHERE namespace IN (?, ?) AND scope=? AND key=?",
        (SETTLED_SUBMISSION_NAMESPACE, RECORDED_REVIEW_NAMESPACE, repository, command_id),
    ).fetchone() is not None


def unresolved_review_submissions(
    db: sqlite3.Connection, control: SQLiteControlStore, binding: WorkflowBinding,
) -> tuple[CommandId, ...]:
    """Current-bound submissions whose outcome or workflow consequence is unresolved."""
    key = binding.fence.key
    unresolved: list[CommandId] = []
    for row in db.execute(
        "SELECT * FROM lifecycle_commands WHERE repository=? AND target=? AND kind=? "
        "AND state IN ('executing','unknown_outcome','retryable_failure','succeeded') "
        "ORDER BY created_at, command_id",
        (key.repository, f"pr:{key.target.number}", CommandKind.SUBMIT_REVIEW.value),
    ).fetchall():
        command = _command_from_row(row)
        if command.workflow != binding:
            continue
        if row["state"] == EffectStatus.SUCCEEDED.value:
            if not review_submission_settled(db, key.repository, str(command.command_id)):
                unresolved.append(command.command_id)
        elif not isinstance(control._command_reconciliation(row), (RetiredSettledCommand, RetiredUncertainCommand)):
            unresolved.append(command.command_id)
    return tuple(unresolved)


def review_release_projection(
    db: sqlite3.Connection, writer: WorkflowWriter, state: ReviewWorkflowSnapshot,
) -> ReleaseProjection | None:
    """Current generation's release is verified only by its durable effect fact."""
    generation = state.revision.fence.generation
    if generation < 1:
        return None
    command_id = review_release_command_id(state.revision.fence.key, generation)
    row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
    if row is None:
        return None
    command = _command_from_row(row)
    if command.workflow is None or command.workflow.fence != state.revision.fence or not owns_command(
        db, writer, command.executor_identity,
    ):
        raise WorkflowAuthorityError("release projection belongs to another workflow authority")
    receipt = db.execute("SELECT status FROM effect_receipts WHERE command_id=?", (str(command_id),)).fetchone()
    status = receipt["status"] if receipt is not None and receipt["status"] == row["state"] else None
    resolved = db.execute(
        "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (RELEASE_RESOLVED_NAMESPACE, writer.repository, str(command_id)),
    ).fetchone() is not None
    if status == EffectStatus.SUCCEEDED.value or resolved:
        return ReleaseProjection(command_id, ReleaseStatus.VERIFIED)
    if status == EffectStatus.PERMANENT_FAILURE.value:
        return ReleaseProjection(command_id, ReleaseStatus.REFUSED)
    return ReleaseProjection(command_id, ReleaseStatus.PENDING)
