"""Pure consistency check for completed observational execution archive passes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .review_checkpoint_archive import CompletedReviewCheckpointArchiveAudit
from .review_execution import (
    ActivatedReviewExecution, CompletedReviewGenerationArchiveAudit,
    ReviewExecutionBoundaryCursor, same_execution_episode,
)
from .review_round_archive import CompletedReviewRoundArchiveScan
from .review_round_receipt_archive import CompletedReviewRoundReceiptArchiveAudit


@dataclass(frozen=True)
class ObservedReviewExecutionArchiveEvidence:
    """Completed audit cursors aligned to one execution, without admission authority."""

    execution: ActivatedReviewExecution
    checkpoints: CompletedReviewCheckpointArchiveAudit
    rounds: CompletedReviewRoundArchiveScan
    receipts: CompletedReviewRoundReceiptArchiveAudit
    generations: CompletedReviewGenerationArchiveAudit
    observed_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.execution, ActivatedReviewExecution)
                or not isinstance(self.checkpoints, CompletedReviewCheckpointArchiveAudit)
                or not isinstance(self.rounds, CompletedReviewRoundArchiveScan)
                or not isinstance(self.receipts, CompletedReviewRoundReceiptArchiveAudit)
                or not isinstance(self.generations, CompletedReviewGenerationArchiveAudit)
                or not isinstance(self.observed_at, datetime)
                or self.observed_at.tzinfo is None
                or self.observed_at.utcoffset() is None):
            raise ValueError("execution archive evidence requires completed typed passes and time")
        execution = self.execution
        receipt_scan = self.receipts.expected
        generation_scan = self.generations.generation_scan
        origin = self.rounds.origin
        digest = self.rounds.origin_source_sha256
        final_boundary = (
            ReviewExecutionBoundaryCursor(
                execution.boundaries[-1].receipt_id,
                execution.boundaries[-1].to_revision,
            ) if execution.boundaries else execution.checkpoint.boundary_cursor
        )
        if (execution.checkpoint_receipt is None
                or self.checkpoints.tip_receipt_id != execution.checkpoint_receipt
                or self.checkpoints.origin != execution.origin
                or self.rounds.origin_receipt != execution.origin
                or receipt_scan.origin_receipt != execution.origin
                or generation_scan.origin_receipt != execution.origin
                or not same_execution_episode(origin, execution.binding)
                or receipt_scan.origin != origin
                or generation_scan.origin != origin
                or self.checkpoints.origin_source_sha256 != digest
                or receipt_scan.origin_source_sha256 != digest
                or generation_scan.origin_source_sha256 != digest):
            raise ValueError("execution archive passes disagree on origin or checkpoint tip")
        if (self.rounds.through_sequence != execution.last_sequence
                or not execution.checkpoint.through_sequence
                <= receipt_scan.through_sequence <= execution.last_sequence
                or generation_scan.through_generation
                != execution.binding.rejection.fence.generation
                or generation_scan.final_cursor != final_boundary):
            raise ValueError("execution archive passes disagree on source frontiers")
        completed = (
            self.checkpoints.completed_at, self.rounds.completed_at,
            self.receipts.completed_at, self.generations.completed_at,
        )
        due = (
            self.checkpoints.next_due_at, self.rounds.next_due_at,
            self.receipts.next_due_at, self.generations.next_due_at,
        )
        if any(time > self.observed_at for time in completed) or any(
            self.observed_at >= time for time in due
        ):
            raise ValueError("execution archive pass is premature or overdue")
