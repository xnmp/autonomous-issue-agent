"""One bounded pre-proposal maintenance step for review execution compaction."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from ..domain.review_execution import ActivatedReviewExecution, MAX_EXECUTION_SUFFIX
from ..domain.review_execution_archive_bootstrap import (
    BlockedReviewExecutionArchiveBootstrap, PendingReviewExecutionArchiveBootstrap,
)
from ..domain.review_execution_compaction_maintenance import (
    BlockedReviewExecutionMaintenance, PendingReviewExecutionMaintenance,
    ReadyReviewExecutionMaintenance, ReviewExecutionMaintenanceResult,
    ReviewExecutionMaintenanceStage,
)
from ..domain.review_execution_archive_gate import BlockedReviewExecutionArchive
from ..domain.workflow_identity import PullRequestRef, WorkflowKey
from .review_execution_archive_admission_audit import advance_review_execution_archive_admission_audit
from .review_execution_archive_bootstrap import advance_review_execution_archive_bootstrap
from .review_execution_archive_evidence import observe_review_execution_archive_evidence
from .review_execution_archive_gate import require_review_execution_archive_admission
from .review_execution_checkpoint_commit import commit_review_execution_checkpoint
from .sql_read_budget import MAX_SQL_READ_BYTES, SqlReadBudgetExceeded, bounded_sql_source_reads
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES
from .workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, load_review_workflow_row,
)


def _finalized_frontier(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution,
) -> int:
    start = execution.checkpoint.through_sequence
    expected = execution.last_sequence - start
    if expected > MAX_EXECUTION_SUFFIX:
        raise WorkflowAuthorityError("review execution suffix exceeds its bounded capacity")
    binding = execution.binding
    rows = db.execute(
        "SELECT sequence,typeof(sequence),typeof(outcome_json) "
        "FROM review_execution_rounds WHERE repository=? AND target_kind='pr' "
        "AND target_number=? AND execution_epoch=? AND execution_episode=? "
        "AND sequence>? ORDER BY sequence LIMIT ?",
        (key.repository, key.target.number, binding.execution_epoch,
         binding.execution_episode, start, MAX_EXECUTION_SUFFIX + 1),
    ).fetchall()
    if len(rows) != expected:
        raise WorkflowAuthorityError("review execution allocated round suffix has a gap")
    frontier = start
    open_seen = False
    for sequence, row in enumerate(rows, start + 1):
        if row[1] != "integer" or row[0] != sequence or row[2] not in ("null", "text"):
            raise WorkflowAuthorityError("review execution round suffix identity is malformed")
        if row[2] == "null":
            open_seen = True
        elif not open_seen:
            frontier = sequence
    return frontier


def _advance(
    db: sqlite3.Connection, key: WorkflowKey, now: datetime,
    round_threshold: int, generation_threshold: int, payload_threshold: int,
    max_source_bytes: int,
) -> ReviewExecutionMaintenanceResult:
    snapshot = load_review_workflow_row(db, key)
    if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
        raise WorkflowAuthorityError("review execution maintenance requires active authority")
    execution = snapshot.execution
    round_suffix = execution.last_sequence - execution.checkpoint.through_sequence
    generation_suffix = len(execution.boundaries)
    payload_bytes = len(encode_workflow_episodes(snapshot).encode("utf-8"))
    triggered = (round_suffix >= round_threshold or generation_suffix >= generation_threshold
                 or payload_bytes >= payload_threshold)

    if execution.checkpoint_receipt is None:
        if not triggered:
            return ReadyReviewExecutionMaintenance(key, snapshot.revision, now)
        source = advance_review_execution_archive_bootstrap(
            db, key, now=now, max_source_bytes=max_source_bytes,
        )
        if isinstance(source, BlockedReviewExecutionArchiveBootstrap):
            return BlockedReviewExecutionMaintenance(key, source.reason, now)
        if isinstance(source, PendingReviewExecutionArchiveBootstrap):
            return PendingReviewExecutionMaintenance(
                key, ReviewExecutionMaintenanceStage.INITIAL_ARCHIVE, now,
            )
    else:
        gate = advance_review_execution_archive_admission_audit(
            db, key, now=now, max_source_bytes=max_source_bytes,
        )
        if isinstance(gate, BlockedReviewExecutionArchive):
            return BlockedReviewExecutionMaintenance(key, gate.reason, now)
        try:
            require_review_execution_archive_admission(db, key, execution, now=now)
        except WorkflowAuthorityError as exc:
            if "overdue" in str(exc):
                return PendingReviewExecutionMaintenance(
                    key, ReviewExecutionMaintenanceStage.RETIRED_ARCHIVE, now,
                )
            return BlockedReviewExecutionMaintenance(key, str(exc)[:512], now)
        if not triggered:
            return ReadyReviewExecutionMaintenance(key, snapshot.revision, now)
        try:
            observe_review_execution_archive_evidence(db, key, now=now)
        except WorkflowAuthorityError as exc:
            if "awaits" in str(exc) or "stale" in str(exc):
                return PendingReviewExecutionMaintenance(
                    key, ReviewExecutionMaintenanceStage.RETIRED_ARCHIVE, now,
                )
            return BlockedReviewExecutionMaintenance(key, str(exc)[:512], now)

    frontier = _finalized_frontier(db, key, execution)
    if (frontier == execution.checkpoint.through_sequence
            and not any(boundary.through_sequence <= frontier for boundary in execution.boundaries)):
        return BlockedReviewExecutionMaintenance(
            key, "open prefix or absent finalized boundary leaves no compactable capacity", now,
        )
    commit_review_execution_checkpoint(
        db, key, snapshot.revision, frontier, now=now, max_source_bytes=max_source_bytes,
    )
    after = load_review_workflow_row(db, key)
    if after is None or not isinstance(after.execution, ActivatedReviewExecution):
        raise WorkflowAuthorityError("review execution maintenance lost its published authority")
    return ReadyReviewExecutionMaintenance(key, after.revision, now)


def advance_review_execution_compaction_maintenance(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
    round_threshold: int = 128, generation_threshold: int = 32,
    payload_threshold: int = 65536, max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ReviewExecutionMaintenanceResult:
    """Advance bounded source proof before a caller derives its next proposal."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(round_threshold) is not int or not 1 <= round_threshold <= MAX_EXECUTION_SUFFIX
            or type(generation_threshold) is not int or not 1 <= generation_threshold <= MAX_EXECUTION_SUFFIX
            or type(payload_threshold) is not int or not 1 <= payload_threshold <= MAX_WORKFLOW_PAYLOAD_BYTES
            or type(max_source_bytes) is not int or not 1 <= max_source_bytes <= MAX_SQL_READ_BYTES):
        raise ValueError("review execution maintenance requires bounded PR authority and aware time")
    db.execute("SAVEPOINT review_execution_compaction_maintenance")
    try:
        with bounded_sql_source_reads(db, max_source_bytes):
            result = _advance(db, key, now, round_threshold, generation_threshold,
                              payload_threshold, max_source_bytes)
    except BaseException as exc:
        try:
            db.execute("ROLLBACK TO review_execution_compaction_maintenance")
        finally:
            db.execute("RELEASE review_execution_compaction_maintenance")
        if isinstance(exc, (WorkflowAuthorityError, SqlReadBudgetExceeded, sqlite3.Error,
                            UnicodeError, ValueError)):
            return BlockedReviewExecutionMaintenance(
                key, (str(exc) or "review execution maintenance source unavailable")[:512], now,
            )
        raise
    db.execute("RELEASE review_execution_compaction_maintenance")
    return result
