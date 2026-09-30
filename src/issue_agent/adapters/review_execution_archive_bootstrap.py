"""Bounded observational bootstrap for an execution's first checkpoint."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from ..domain.review_archive_identity import BlockedReviewArchiveIdentities, CompletedReviewArchiveIdentities
from ..domain.review_execution import (
    ActivatedReviewExecution, BlockedReviewGenerationArchiveScan,
    BlockedReviewGenerationArchiveSweep, CompletedReviewGenerationArchiveAudit,
)
from ..domain.review_execution_archive_bootstrap import (
    BlockedReviewExecutionArchiveBootstrap, PendingReviewExecutionArchiveBootstrap,
    ReviewExecutionArchiveBootstrapResult, ReviewExecutionArchiveBootstrapStage,
)
from ..domain.review_initial_checkpoint_absence import (
    BlockedInitialCheckpointAbsence, CompletedInitialCheckpointAbsence,
)
from ..domain.review_lifecycle_archive_audit import (
    BlockedReviewLifecycleArchiveAudit, CompletedReviewLifecycleArchiveAudit,
)
from ..domain.review_round_archive import BlockedReviewRoundArchiveScan, CompletedReviewRoundArchiveScan
from ..domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit,
)
from ..domain.workflow_identity import PullRequestRef, WorkflowKey
from .review_archive_identity import advance_review_archive_identity_audit, require_review_archive_identity_audit
from .review_execution_archive_audit import advance_review_generation_archive_audit
from .review_execution_archive_evidence import observe_initial_review_execution_archive_evidence
from .review_initial_checkpoint_absence import advance_initial_checkpoint_absence_audit
from .review_lifecycle_archive_audit import advance_review_lifecycle_archive_audit
from .review_round_archive_audit import advance_review_round_archive_audit
from .review_round_receipt_archive_audit import advance_review_round_receipt_archive_audit
from .sql_read_budget import MAX_SQL_READ_BYTES, SqlReadBudgetExceeded, bounded_sql_source_reads
from .workflow_rows import WorkflowAuthorityError, load_review_workflow_row


def _advance(
    db: sqlite3.Connection, key: WorkflowKey, now: datetime, due_seconds: int,
) -> ReviewExecutionArchiveBootstrapResult:
    snapshot = load_review_workflow_row(db, key)
    if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
        raise WorkflowAuthorityError("archive bootstrap requires active execution authority")
    execution = snapshot.execution
    if (execution.checkpoint_receipt is not None or execution.checkpoint.through_sequence != 0
            or execution.checkpoint.boundary_cursor is not None):
        raise WorkflowAuthorityError("archive bootstrap requires an uncompacted execution")

    identities = advance_review_archive_identity_audit(db, now=now, due_seconds=due_seconds)
    if isinstance(identities, BlockedReviewArchiveIdentities):
        return BlockedReviewExecutionArchiveBootstrap(key, identities.reason[:512], now)
    if not isinstance(identities, CompletedReviewArchiveIdentities):
        return PendingReviewExecutionArchiveBootstrap(
            key, ReviewExecutionArchiveBootstrapStage.IDENTITIES, now,
        )

    states = (
        advance_review_round_archive_audit(db, key, now=now, due_seconds=due_seconds),
        advance_review_round_receipt_archive_audit(db, key, now=now, due_seconds=due_seconds),
        advance_review_generation_archive_audit(db, key, now=now, due_seconds=due_seconds),
        advance_review_lifecycle_archive_audit(db, key.repository, now=now, due_seconds=due_seconds),
        advance_initial_checkpoint_absence_audit(
            db, execution.checkpoint.state.binding, now=now, due_seconds=due_seconds,
        ),
    )
    blocked_types = (
        BlockedReviewRoundArchiveScan, BlockedReviewRoundReceiptArchiveScan,
        BlockedReviewRoundReceiptArchiveSweep, BlockedReviewGenerationArchiveScan,
        BlockedReviewGenerationArchiveSweep, BlockedReviewLifecycleArchiveAudit,
        BlockedInitialCheckpointAbsence,
    )
    blocked = next((state for state in states if isinstance(state, blocked_types)), None)
    if blocked is not None:
        return BlockedReviewExecutionArchiveBootstrap(key, blocked.reason[:512], now)
    completed_types = (
        CompletedReviewRoundArchiveScan, CompletedReviewRoundReceiptArchiveAudit,
        CompletedReviewGenerationArchiveAudit, CompletedReviewLifecycleArchiveAudit,
        CompletedInitialCheckpointAbsence,
    )
    if not all(isinstance(state, kind) for state, kind in zip(states, completed_types, strict=True)):
        return PendingReviewExecutionArchiveBootstrap(
            key, ReviewExecutionArchiveBootstrapStage.EPISODE_SOURCES, now,
        )
    require_review_archive_identity_audit(db, now=now)
    return observe_initial_review_execution_archive_evidence(db, key, now=now)


def advance_review_execution_archive_bootstrap(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
    due_seconds: int = 3600, max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ReviewExecutionArchiveBootstrapResult:
    """Advance at most one page per source family in the caller's transaction."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int or not 1 <= due_seconds <= 86400
            or type(max_source_bytes) is not int or not 1 <= max_source_bytes <= MAX_SQL_READ_BYTES):
        raise ValueError("archive bootstrap requires a PR key, metered transaction and bounded aware request")
    db.execute("SAVEPOINT review_execution_archive_bootstrap")
    try:
        with bounded_sql_source_reads(db, max_source_bytes):
            result = _advance(db, key, now, due_seconds)
    except BaseException as exc:
        try:
            db.execute("ROLLBACK TO review_execution_archive_bootstrap")
        finally:
            db.execute("RELEASE review_execution_archive_bootstrap")
        if isinstance(exc, (WorkflowAuthorityError, SqlReadBudgetExceeded, sqlite3.Error,
                            UnicodeError, ValueError)):
            return BlockedReviewExecutionArchiveBootstrap(
                key, (str(exc) or "archive bootstrap source unavailable")[:512], now,
            )
        raise
    db.execute("RELEASE review_execution_archive_bootstrap")
    return result
