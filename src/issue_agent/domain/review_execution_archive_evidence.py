"""Pure consistency check for completed observational execution archive passes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .review_checkpoint_archive import CompletedReviewCheckpointArchiveAudit
from .review_initial_checkpoint_absence import CompletedInitialCheckpointAbsence
from .review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from .review_execution import (
    ActivatedReviewExecution, CompletedReviewGenerationArchiveAudit,
    ReviewExecutionBoundaryCursor, same_execution_episode,
)
from .review_round_archive import CompletedReviewRoundArchiveScan
from .review_round_receipt_archive import CompletedReviewRoundReceiptArchiveAudit


def _check_shared_archive_evidence(
    execution: ActivatedReviewExecution,
    rounds: CompletedReviewRoundArchiveScan,
    receipts: CompletedReviewRoundReceiptArchiveAudit,
    generations: CompletedReviewGenerationArchiveAudit,
    lifecycle: CompletedReviewLifecycleArchiveAudit,
    observed_at: datetime,
) -> None:
    if (not isinstance(execution, ActivatedReviewExecution)
            or not isinstance(rounds, CompletedReviewRoundArchiveScan)
            or not isinstance(receipts, CompletedReviewRoundReceiptArchiveAudit)
            or not isinstance(generations, CompletedReviewGenerationArchiveAudit)
            or not isinstance(lifecycle, CompletedReviewLifecycleArchiveAudit)
            or not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None or observed_at.utcoffset() is None):
        raise ValueError("execution archive evidence requires completed typed passes and time")
    receipt_scan = receipts.expected
    generation_scan = generations.generation_scan
    origin = rounds.origin
    digest = rounds.origin_source_sha256
    final_boundary = (
        ReviewExecutionBoundaryCursor(
            execution.boundaries[-1].receipt_id,
            execution.boundaries[-1].to_revision,
        ) if execution.boundaries else execution.checkpoint.boundary_cursor
    )
    if (rounds.origin_receipt != execution.origin
            or receipt_scan.origin_receipt != execution.origin
            or generation_scan.origin_receipt != execution.origin
            or not same_execution_episode(origin, execution.binding)
            or receipt_scan.origin != origin
            or generation_scan.origin != origin
            or receipt_scan.origin_source_sha256 != digest
            or generation_scan.origin_source_sha256 != digest):
        raise ValueError("execution archive passes disagree on origin or checkpoint tip")
    if lifecycle.repository != execution.binding.rejection.fence.key.repository:
        raise ValueError("execution archive lifecycle census belongs to another repository")
    if (rounds.through_sequence != execution.last_sequence
            or not execution.checkpoint.through_sequence
            <= receipt_scan.through_sequence <= execution.last_sequence
            or generation_scan.through_generation
            != execution.binding.rejection.fence.generation
            or generation_scan.final_cursor != final_boundary):
        raise ValueError("execution archive passes disagree on source frontiers")
    completed = (
        rounds.completed_at, receipts.completed_at, generations.completed_at,
        lifecycle.completed_at,
    )
    due = (
        rounds.next_due_at, receipts.next_due_at, generations.next_due_at,
        lifecycle.next_due_at,
    )
    if any(time > observed_at for time in completed) or any(
        observed_at >= time for time in due
    ):
        raise ValueError("execution archive pass is premature or overdue")


@dataclass(frozen=True)
class ObservedInitialReviewExecutionArchiveEvidence:
    """Current pre-compaction source censuses, without checkpoint or admission authority."""

    execution: ActivatedReviewExecution
    rounds: CompletedReviewRoundArchiveScan
    receipts: CompletedReviewRoundReceiptArchiveAudit
    generations: CompletedReviewGenerationArchiveAudit
    lifecycle: CompletedReviewLifecycleArchiveAudit
    checkpoint_absence: CompletedInitialCheckpointAbsence
    observed_at: datetime

    def __post_init__(self) -> None:
        _check_shared_archive_evidence(
            self.execution, self.rounds, self.receipts,
            self.generations, self.lifecycle, self.observed_at,
        )
        if (self.execution.checkpoint_receipt is not None
                or self.execution.checkpoint.through_sequence != 0
                or self.execution.checkpoint.boundary_cursor is not None):
            raise ValueError("initial execution archive evidence requires the origin checkpoint")
        if (not isinstance(self.checkpoint_absence, CompletedInitialCheckpointAbsence)
                or self.checkpoint_absence.binding != self.execution.checkpoint.state.binding):
            raise ValueError("initial execution archive evidence requires checkpoint absence")
        if (self.checkpoint_absence.completed_at > self.observed_at
                or self.observed_at >= self.checkpoint_absence.next_due_at):
            raise ValueError("initial checkpoint absence pass is premature or overdue")


@dataclass(frozen=True)
class ObservedReviewExecutionArchiveEvidence:
    """Completed checkpoint and source censuses, without admission authority."""

    execution: ActivatedReviewExecution
    checkpoints: CompletedReviewCheckpointArchiveAudit
    rounds: CompletedReviewRoundArchiveScan
    receipts: CompletedReviewRoundReceiptArchiveAudit
    generations: CompletedReviewGenerationArchiveAudit
    lifecycle: CompletedReviewLifecycleArchiveAudit
    observed_at: datetime

    def __post_init__(self) -> None:
        _check_shared_archive_evidence(
            self.execution, self.rounds, self.receipts,
            self.generations, self.lifecycle, self.observed_at,
        )
        if not isinstance(self.checkpoints, CompletedReviewCheckpointArchiveAudit):
            raise ValueError("execution archive evidence requires a completed checkpoint pass")
        execution = self.execution
        digest = self.rounds.origin_source_sha256
        if (execution.checkpoint_receipt is None
                or self.checkpoints.tip_receipt_id != execution.checkpoint_receipt
                or self.checkpoints.origin != execution.origin
                or self.checkpoints.origin_source_sha256 != digest
                ):
            raise ValueError("execution archive passes disagree on origin or checkpoint tip")
        if (self.checkpoints.completed_at > self.observed_at
                or self.observed_at >= self.checkpoints.next_due_at):
            raise ValueError("execution archive pass is premature or overdue")
