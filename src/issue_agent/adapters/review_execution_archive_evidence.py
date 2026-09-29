"""Read-only join of completed execution and lifecycle archive observations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..domain.review_checkpoint_archive import CompletedReviewCheckpointArchiveAudit
from ..domain.review_execution import ActivatedReviewExecution, CompletedReviewGenerationArchiveAudit
from ..domain.review_execution_archive_evidence import (
    ObservedInitialReviewExecutionArchiveEvidence,
    ObservedReviewExecutionArchiveEvidence,
)
from ..domain.review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from ..domain.review_initial_checkpoint_absence import CompletedInitialCheckpointAbsence
from ..domain.review_round_archive import CompletedReviewRoundArchiveScan
from ..domain.review_round_receipt_archive import CompletedReviewRoundReceiptArchiveAudit
from ..domain.workflow_identity import WorkflowKey
from .review_checkpoint_archive_audit import (
    _audit_key as _checkpoint_key, _legacy_discrepancy, _read_state as _checkpoint_state,
    _v2_discrepancy, _v3_discrepancy,
)
from .review_execution_archive_audit import (
    _audit_key as _generation_key, _read_state as _generation_state,
)
from .review_execution_activation import _read_recording_source
from .review_archive_source_versions import (
    read_review_archive_source_versions, read_review_lifecycle_source_version,
)
from .review_checkpoint_application_source import read_review_checkpoint_application_source
from .review_checkpoint_following_source import read_checkpoint_following_application_source
from .review_round_archive_audit import (
    _audit_key as _round_key, _current_execution, _read_state as _round_state,
)
from .review_round_receipt_archive_audit import (
    _audit_key as _receipt_key, _read_state as _receipt_state,
)
from .review_lifecycle_archive_audit import read_review_lifecycle_archive_state
from .review_initial_checkpoint_absence import read_initial_checkpoint_absence_state
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError,
    _check_execution_checkpoint_receipt, _read_execution_origin, workflow_scope,
)


def _validate_request(db: sqlite3.Connection, key: WorkflowKey, now: datetime) -> tuple[str, str, int]:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        raise ValueError("execution archive evidence requires a transaction and aware time")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("execution archive evidence requires a PR workflow")
    return scope


def _read_completed_source_passes(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution,
):
    epoch, episode = execution.binding.execution_epoch, execution.binding.execution_episode
    _, rounds, round_rows_version = _round_state(
        db, key.repository, _round_key(key, epoch, episode),
    )
    _, receipts, receipt_versions = _receipt_state(
        db, key.repository, _receipt_key(key, epoch, episode),
    )
    _, generations, generation_sources_version, _ = _generation_state(
        db, key.repository, _generation_key(key, epoch, episode),
    )
    lifecycle = read_review_lifecycle_archive_state(db, key.repository)
    if (not isinstance(rounds, CompletedReviewRoundArchiveScan)
            or not isinstance(receipts, CompletedReviewRoundReceiptArchiveAudit)
            or not isinstance(generations, CompletedReviewGenerationArchiveAudit)
            or not isinstance(lifecycle, CompletedReviewLifecycleArchiveAudit)):
        raise WorkflowAuthorityError("execution archive awaits complete source and census passes")
    current_versions = read_review_archive_source_versions(db, key.repository)
    if round_rows_version != current_versions.round_rows:
        raise WorkflowAuthorityError("execution archive round-row census is stale")
    if receipt_versions != (current_versions.round_rows, current_versions.all_round_rows,
                            current_versions.round_receipts):
        raise WorkflowAuthorityError("execution archive receipt census is stale")
    if generation_sources_version != current_versions.generation_sources:
        raise WorkflowAuthorityError("execution archive generation census is stale")
    if lifecycle.source_version != read_review_lifecycle_source_version(db):
        raise WorkflowAuthorityError("execution archive lifecycle census is stale")
    return rounds, receipts, generations, lifecycle, current_versions


def observe_initial_review_execution_archive_evidence(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
) -> ObservedInitialReviewExecutionArchiveEvidence:
    """Align source censuses before the first checkpoint; observational only."""
    scope = _validate_request(db, key, now)
    execution = _current_execution(db, key)
    if (execution.checkpoint_receipt is not None
            or execution.checkpoint.through_sequence != 0
            or execution.checkpoint.boundary_cursor is not None):
        raise WorkflowAuthorityError("initial archive evidence requires an uncompacted execution")
    checkpoint_key = _checkpoint_key(
        key, execution.binding.execution_epoch, execution.binding.execution_episode,
    )
    if (_legacy_discrepancy(db, key.repository, checkpoint_key)
            or _v2_discrepancy(db, key.repository, checkpoint_key)
            or _v3_discrepancy(db, key.repository, checkpoint_key)):
        raise WorkflowAuthorityError("initial archive has an unresolved prior checkpoint cursor")
    checkpoint_rowid, _, _, _ = _checkpoint_state(db, key.repository, checkpoint_key)
    if checkpoint_rowid is not None:
        raise WorkflowAuthorityError("initial archive has an unexpected checkpoint cursor")
    rounds, receipts, generations, lifecycle, current_versions = _read_completed_source_passes(
        db, key, execution,
    )
    checkpoint_absence = read_initial_checkpoint_absence_state(
        db, execution.checkpoint.state.binding,
    )
    if not isinstance(checkpoint_absence, CompletedInitialCheckpointAbsence):
        raise WorkflowAuthorityError("initial archive awaits checkpoint receipt absence pass")
    if checkpoint_absence.source_version != current_versions.checkpoint_sources:
        raise WorkflowAuthorityError("initial archive checkpoint absence pass is stale")
    origin, _ = _read_execution_origin(db, scope, execution)
    _, origin_digest = _read_recording_source(
        db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
        origin.origin.evidence_id,
    )
    try:
        evidence = ObservedInitialReviewExecutionArchiveEvidence(
            execution, rounds, receipts, generations, lifecycle,
            checkpoint_absence, now,
        )
        if evidence.rounds.origin_source_sha256 != origin_digest:
            raise ValueError("execution archive origin source changed since its passes")
        return evidence
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("execution archive completed passes disagree") from exc


def observe_review_execution_archive_evidence(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
) -> ObservedReviewExecutionArchiveEvidence:
    """Align completed checkpoint and source cursors; not admission authority."""
    scope = _validate_request(db, key, now)
    execution = _current_execution(db, key)
    epoch, episode = execution.binding.execution_epoch, execution.binding.execution_episode
    checkpoint_key = _checkpoint_key(key, epoch, episode)
    if (_legacy_discrepancy(db, key.repository, checkpoint_key)
            or _v2_discrepancy(db, key.repository, checkpoint_key)
            or _v3_discrepancy(db, key.repository, checkpoint_key)):
        raise WorkflowAuthorityError("execution archive has an unresolved prior checkpoint cursor")
    _, checkpoint, checkpoint_versions, _ = _checkpoint_state(
        db, key.repository, checkpoint_key,
    )
    rounds, receipts, generations, lifecycle, current_versions = _read_completed_source_passes(
        db, key, execution,
    )
    if not isinstance(checkpoint, CompletedReviewCheckpointArchiveAudit):
        raise WorkflowAuthorityError("execution archive awaits complete source and census passes")
    origin, _ = _read_execution_origin(db, scope, execution)
    _, origin_digest = _read_recording_source(
        db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
        origin.origin.evidence_id,
    )
    _check_execution_checkpoint_receipt(db, key.repository, execution, origin)
    if execution.checkpoint_receipt is None:
        raise WorkflowAuthorityError("execution archive checkpoint has no receipt")
    application = read_review_checkpoint_application_source(
        db, key.repository, execution.checkpoint_receipt,
    )
    if (application.checkpoint.checkpoint != execution.checkpoint
            or application.source_sha256 != checkpoint.tip_application_source_sha256):
        raise WorkflowAuthorityError("execution archive application changed its checkpoint")
    read_checkpoint_following_application_source(
        db, origin.checkpoint.state.binding,
        execution.binding.rejection.fence.generation, application.application,
    )
    if checkpoint_versions != current_versions:
        raise WorkflowAuthorityError("execution archive checkpoint census is stale")
    try:
        evidence = ObservedReviewExecutionArchiveEvidence(
            execution, checkpoint, rounds, receipts, generations, lifecycle, now,
        )
        if evidence.rounds.origin_source_sha256 != origin_digest:
            raise ValueError("execution archive origin source changed since its passes")
        return evidence
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("execution archive completed passes disagree") from exc
