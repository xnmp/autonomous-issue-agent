"""Bounded, read-only source for a checkpoint's durable tail choice."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..domain.review_checkpoint_application import (
    ReviewCheckpointApplicationProposal, ReviewCheckpointApplicationReceipt,
    application_proposal_from_receipt,
)
from ..domain.review_execution import ReviewExecutionCheckpointReceipt
from ..domain.workflow_identity import (
    canonical_workflow_repository, validate_workflow_identifier,
)
from .review_execution_activation import _read_recording_source
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError, _read_checkpoint_receipt_row


REVIEW_CHECKPOINT_APPLICATION_NAMESPACE = "review-checkpoint-application"


@dataclass(frozen=True)
class ObservedReviewCheckpointApplicationSource:
    checkpoint_receipt_id: str
    checkpoint: ReviewExecutionCheckpointReceipt
    application: ReviewCheckpointApplicationProposal
    claimed_at: datetime
    source_sha256: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(
            self.checkpoint_receipt_id, "checkpoint application source identity",
        )
        if (not isinstance(self.checkpoint, ReviewExecutionCheckpointReceipt)
                or not isinstance(self.application, ReviewCheckpointApplicationProposal)
                or self.application.previous_checkpoint != self.checkpoint.previous_checkpoint
                or self.application.checkpoint != self.checkpoint.checkpoint
                or not isinstance(self.claimed_at, datetime)
                or self.claimed_at.tzinfo is None
                or self.claimed_at.utcoffset() is None
                or not isinstance(self.source_sha256, str)
                or len(self.source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.source_sha256)):
            raise ValueError("checkpoint application source requires paired typed receipts")


def read_review_checkpoint_application_source(
    db: sqlite3.Connection, repository: str, checkpoint_receipt_id: str,
) -> ObservedReviewCheckpointApplicationSource:
    """Bind a retained tail choice to its exact checkpoint receipt in one snapshot.

    This only observes recorded sources. It cannot establish that the choice
    was minted atomically or that all archived outcomes match the checkpoint.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("checkpoint application source requires a transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("checkpoint application source requires a canonical repository")
    validate_workflow_identifier(checkpoint_receipt_id, "checkpoint application source identity")
    checkpoint, checkpoint_claimed = _read_checkpoint_receipt_row(
        db, repository, checkpoint_receipt_id,
    )
    if checkpoint.checkpoint.state.binding.rejection.fence.key.repository != repository:
        raise WorkflowAuthorityError("checkpoint application source has a foreign repository")
    try:
        fields, source_digest = _read_recording_source(
            db, REVIEW_CHECKPOINT_APPLICATION_NAMESPACE,
            repository, checkpoint_receipt_id,
        )
        values = dict(fields)
        raw = values["payload_json"]
        claimed_raw = values["claimed_at"]
        if values["reusable_after"] is not None or raw is None or claimed_raw is None:
            raise ValueError("checkpoint application must be a permanent claim")
        claimed = datetime.fromisoformat(claimed_raw.replace("Z", "+00:00"))
        if (claimed.tzinfo is None or claimed.utcoffset() is None
                or claimed != checkpoint_claimed):
            raise ValueError("checkpoint application claim differs from its checkpoint")
        recorded = decode_workflow(raw, ReviewCheckpointApplicationReceipt)
        if (encode_workflow(recorded) != raw
                or recorded.checkpoint_receipt_id != checkpoint_receipt_id):
            raise ValueError("checkpoint application payload is noncanonical or foreign")
        application = application_proposal_from_receipt(checkpoint, recorded)
        return ObservedReviewCheckpointApplicationSource(
            checkpoint_receipt_id, checkpoint, application, claimed, source_digest,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("checkpoint application source is unreadable") from exc
