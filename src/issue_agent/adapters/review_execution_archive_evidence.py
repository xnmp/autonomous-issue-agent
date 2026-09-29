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
    _v2_discrepancy,
)
from .review_execution_archive_audit import (
    _audit_key as _generation_key, _read_state as _generation_state,
)
from .review_execution_activation import _read_recording_source
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
            or _v2_discrepancy(db, key.repository, checkpoint_key)):
        raise WorkflowAuthorityError("execution archive has an unresolved prior checkpoint cursor")
    _, checkpoint = _checkpoint_state(db, key.repository, checkpoint_key)
    _, rounds = _round_state(db, key.repository, _round_key(key, epoch, episode))
    _, receipts = _receipt_state(db, key.repository, _receipt_key(key, epoch, episode))
    _, generations = _generation_state(
        db, key.repository, _generation_key(key, epoch, episode),
    )
    if (not isinstance(checkpoint, CompletedReviewCheckpointArchiveAudit)
            or not isinstance(rounds, CompletedReviewRoundArchiveScan)
            or not isinstance(receipts, CompletedReviewRoundReceiptArchiveAudit)
            or not isinstance(generations, CompletedReviewGenerationArchiveAudit)):
        raise WorkflowAuthorityError("execution archive awaits complete source and census passes")
    origin, _ = _read_execution_origin(db, scope, execution)
    _, origin_digest = _read_recording_source(
        db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
        origin.origin.evidence_id,
    )
    _check_execution_checkpoint_receipt(db, key.repository, execution, origin)
    try:
        evidence = ObservedReviewExecutionArchiveEvidence(
            execution, checkpoint, rounds, receipts, generations, now,
        )
        if evidence.rounds.origin_source_sha256 != origin_digest:
            raise ValueError("execution archive origin source changed since its passes")
        return evidence
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("execution archive completed passes disagree") from exc
