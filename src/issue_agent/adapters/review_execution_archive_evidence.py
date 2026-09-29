"""Read-only join of the four completed execution archive observations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..domain.review_checkpoint_archive import CompletedReviewCheckpointArchiveAudit
from ..domain.review_execution import CompletedReviewGenerationArchiveAudit
from ..domain.review_execution_archive_evidence import ObservedReviewExecutionArchiveEvidence
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
from .review_archive_source_versions import read_review_archive_source_versions
from .review_checkpoint_application_source import read_review_checkpoint_application_source
from .review_checkpoint_following_source import read_checkpoint_following_application_source
from .review_round_archive_audit import (
    _audit_key as _round_key, _current_execution, _read_state as _round_state,
)
from .review_round_receipt_archive_audit import (
    _audit_key as _receipt_key, _read_state as _receipt_state,
)
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError,
    _check_execution_checkpoint_receipt, _read_execution_origin, workflow_scope,
)


def observe_review_execution_archive_evidence(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
) -> ObservedReviewExecutionArchiveEvidence:
    """Align completed SQL audit cursors; callers must not treat this as admission."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        raise ValueError("execution archive evidence requires a transaction and aware time")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("execution archive evidence requires a PR workflow")
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
    _, rounds, round_rows_version = _round_state(
        db, key.repository, _round_key(key, epoch, episode),
    )
    _, receipts, receipt_versions = _receipt_state(
        db, key.repository, _receipt_key(key, epoch, episode),
    )
    _, generations, generation_sources_version, _ = _generation_state(
        db, key.repository, _generation_key(key, epoch, episode),
    )
    if (not isinstance(checkpoint, CompletedReviewCheckpointArchiveAudit)
            or not isinstance(rounds, CompletedReviewRoundArchiveScan)
            or not isinstance(receipts, CompletedReviewRoundReceiptArchiveAudit)
            or not isinstance(generations, CompletedReviewGenerationArchiveAudit)):
        raise WorkflowAuthorityError("execution archive awaits complete source and census passes")
    current_versions = read_review_archive_source_versions(db, key.repository)
    if round_rows_version != current_versions.round_rows:
        raise WorkflowAuthorityError("execution archive round-row census is stale")
    if receipt_versions != (current_versions.round_rows, current_versions.all_round_rows,
                            current_versions.round_receipts):
        raise WorkflowAuthorityError("execution archive receipt census is stale")
    if generation_sources_version != current_versions.generation_sources:
        raise WorkflowAuthorityError("execution archive generation census is stale")
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
            execution, checkpoint, rounds, receipts, generations, now,
        )
        if evidence.rounds.origin_source_sha256 != origin_digest:
            raise ValueError("execution archive origin source changed since its passes")
        return evidence
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("execution archive completed passes disagree") from exc
