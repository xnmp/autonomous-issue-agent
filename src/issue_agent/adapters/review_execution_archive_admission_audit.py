"""Advance bounded archive passes and publish their admission result atomically."""

from dataclasses import replace
from datetime import datetime
import sqlite3

from ..domain.review_execution_archive_gate import (
    BlockedReviewExecutionArchive, CertifiedReviewExecutionArchive,
    ReviewExecutionArchiveGate, certified_review_execution_archive,
)
from ..domain.workflow_identity import WorkflowKey
from ..domain.review_archive_identity import BlockedReviewArchiveIdentities, CompletedReviewArchiveIdentities
from ..domain.review_checkpoint_archive import BlockedReviewCheckpointArchiveAudit, CompletedReviewCheckpointArchiveAudit
from ..domain.review_execution import (
    BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep,
    CompletedReviewGenerationArchiveAudit,
)
from ..domain.review_lifecycle_archive_audit import (
    BlockedReviewLifecycleArchiveAudit, CompletedReviewLifecycleArchiveAudit,
)
from ..domain.review_round_archive import BlockedReviewRoundArchiveScan, CompletedReviewRoundArchiveScan
from ..domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit,
)
from .review_archive_source_versions import (
    read_review_archive_source_versions, read_review_lifecycle_source_version,
)
from .review_checkpoint_archive_audit import advance_review_checkpoint_archive_audit
from .review_execution_archive_audit import advance_review_generation_archive_audit
from .review_execution_archive_evidence import observe_review_execution_archive_evidence
from .review_archive_identity import advance_review_archive_identity_audit
from .review_execution_archive_gate import (
    _read_review_execution_archive_gate_record, persist_review_execution_archive_gate,
    read_review_execution_archive_gate,
)
from .review_lifecycle_archive_audit import advance_review_lifecycle_archive_audit
from .review_round_archive_audit import _current_execution, advance_review_round_archive_audit
from .review_round_receipt_archive_audit import advance_review_round_receipt_archive_audit
from .sql_read_budget import MAX_SQL_READ_BYTES, bounded_sql_source_reads
from .workflow_rows import WorkflowAuthorityError


def advance_review_execution_archive_admission_audit(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
    due_seconds: int = 3600, max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ReviewExecutionArchiveGate:
    """One page per source family, never a historical scan on an admission read."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey) or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int or not 1 <= due_seconds <= 86400):
        raise ValueError("execution archive admission audit requires a transaction and bounded aware request")
    with bounded_sql_source_reads(db, max_source_bytes):
        execution = _current_execution(db, key)
        gate = _read_review_execution_archive_gate_record(db, key, execution)
        prior = certified_review_execution_archive(gate, execution)
        if now < prior.certified_at:
            raise ValueError("execution archive audit predates its certification")
        try:
            read_review_execution_archive_gate(db, key, execution)
            identities = advance_review_archive_identity_audit(db, now=now, due_seconds=due_seconds)
            if isinstance(identities, BlockedReviewArchiveIdentities):
                result = BlockedReviewExecutionArchive(prior, identities.reason[:512], now)
                persist_review_execution_archive_gate(db, key, execution, result, now=now)
                return result
            states = (
                advance_review_round_archive_audit(db, key, now=now, due_seconds=due_seconds),
                advance_review_round_receipt_archive_audit(db, key, now=now, due_seconds=due_seconds),
                advance_review_generation_archive_audit(db, key, now=now, due_seconds=due_seconds),
                (advance_review_checkpoint_archive_audit(db, key, now=now, due_seconds=due_seconds)
                 if isinstance(identities, CompletedReviewArchiveIdentities) else None),
                advance_review_lifecycle_archive_audit(db, key.repository, now=now, due_seconds=due_seconds),
            )
            blocked_types = (
                BlockedReviewRoundArchiveScan, BlockedReviewRoundReceiptArchiveScan,
                BlockedReviewRoundReceiptArchiveSweep, BlockedReviewGenerationArchiveScan,
                BlockedReviewGenerationArchiveSweep, BlockedReviewCheckpointArchiveAudit,
                BlockedReviewLifecycleArchiveAudit,
            )
            blocked = next((state for state in states if isinstance(state, blocked_types)), None)
            if blocked is not None:
                result = BlockedReviewExecutionArchive(prior, blocked.reason[:512], now)
            else:
                completed_types = (
                    CompletedReviewRoundArchiveScan, CompletedReviewRoundReceiptArchiveAudit,
                    CompletedReviewGenerationArchiveAudit, CompletedReviewCheckpointArchiveAudit,
                    CompletedReviewLifecycleArchiveAudit,
                )
                if not all(isinstance(state, kind) for state, kind in zip(states, completed_types, strict=True)):
                    return gate
                evidence = observe_review_execution_archive_evidence(db, key, now=now)
                due = min(evidence.checkpoints.next_due_at, evidence.rounds.next_due_at,
                          evidence.receipts.next_due_at, evidence.generations.next_due_at,
                          evidence.lifecycle.next_due_at, identities.next_due_at)
                result = replace(
                    prior, certified_at=now, next_due_at=due,
                    source_versions=read_review_archive_source_versions(db, key.repository),
                    lifecycle_source_version=read_review_lifecycle_source_version(db),
                )
        except WorkflowAuthorityError as exc:
            result = BlockedReviewExecutionArchive(prior, (str(exc) or "archived source discrepancy")[:512], now)
        persist_review_execution_archive_gate(db, key, execution, result, now=now)
        return result
