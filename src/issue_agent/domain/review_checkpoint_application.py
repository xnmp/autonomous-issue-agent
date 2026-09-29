"""Captured outcome applicability for a proposed review checkpoint."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .review_execution import (
    ActivatedReviewExecution, ReviewExecutionBoundaryCursor,
    ReviewExecutionCheckpoint, ReviewExecutionCompactionPlan,
    ReviewGenerationArchiveFingerprint, plan_review_execution_compaction,
    same_execution_episode,
)
from .workflow_identity import WorkflowRevision


def _applied_digest(sequences: tuple[int, ...]) -> str:
    return hashlib.sha256("\0".join(str(sequence) for sequence in sequences).encode()).hexdigest()


@dataclass(frozen=True)
class ApplyAllCheckpointTailOutcomes:
    """No following boundary existed in the captured compaction plan."""


@dataclass(frozen=True)
class ApplyCheckpointTailByFollowingBoundary:
    """The first unretired generation boundary selects tail outcomes."""

    source: ReviewGenerationArchiveFingerprint
    applied_sequences_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, ReviewGenerationArchiveFingerprint):
            raise ValueError("checkpoint tail selection requires a retained boundary source")
        value = self.applied_sequences_sha256
        if (not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("checkpoint tail selection requires an applied-sequence digest")


CheckpointTailApplication = (
    ApplyAllCheckpointTailOutcomes | ApplyCheckpointTailByFollowingBoundary
)


@dataclass(frozen=True)
class ReviewCheckpointApplicationProposal:
    """A writer input, not proof that a checkpoint was minted from these sources."""

    previous_checkpoint: ReviewExecutionCheckpoint
    checkpoint: ReviewExecutionCheckpoint
    captured_revision: WorkflowRevision
    tail_start_sequence: int
    tail: CheckpointTailApplication

    def __post_init__(self) -> None:
        if (not isinstance(self.previous_checkpoint, ReviewExecutionCheckpoint)
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or not isinstance(self.captured_revision, WorkflowRevision)
                or type(self.tail_start_sequence) is not int
                or not self.previous_checkpoint.through_sequence <= self.tail_start_sequence
                <= self.checkpoint.through_sequence
                or not same_execution_episode(
                    self.previous_checkpoint.state.binding, self.checkpoint.state.binding,
                )
                or self.previous_checkpoint.policy != self.checkpoint.policy
                or self.captured_revision.fence.key
                != self.checkpoint.state.binding.rejection.fence.key
                or (self.checkpoint.boundary_cursor is not None
                    and self.captured_revision.version
                    < self.checkpoint.boundary_cursor.revision.version)):
            raise ValueError("checkpoint application requires matching captured frontiers")
        if isinstance(self.tail, ApplyAllCheckpointTailOutcomes):
            if self.captured_revision.fence != self.checkpoint.state.binding.rejection.fence:
                raise ValueError("all checkpoint tail outcomes require the captured generation")
        elif isinstance(self.tail, ApplyCheckpointTailByFollowingBoundary):
            cursor = self.tail.source.cursor
            if (cursor.revision.fence.key != self.captured_revision.fence.key
                    or cursor.revision.fence.generation
                    != self.checkpoint.state.binding.rejection.fence.generation + 1
                    or cursor.revision.version > self.captured_revision.version
                    or self.captured_revision.fence.generation
                    < cursor.revision.fence.generation):
                raise ValueError("following boundary is outside the captured revision")
        else:
            raise ValueError("checkpoint application requires a typed tail selection")


def build_review_checkpoint_application_proposal(
    execution: ActivatedReviewExecution,
    plan: ReviewExecutionCompactionPlan,
    captured_revision: WorkflowRevision,
    following_source: ReviewGenerationArchiveFingerprint | None,
) -> ReviewCheckpointApplicationProposal:
    """Bind the compaction planner's exact first remaining boundary."""
    if (not isinstance(execution, ActivatedReviewExecution)
            or not isinstance(plan, ReviewExecutionCompactionPlan)
            or not isinstance(captured_revision, WorkflowRevision)
            or captured_revision.fence != execution.state.binding.rejection.fence
            or plan_review_execution_compaction(
                execution, plan.retired_outcomes + plan.remaining_outcomes,
                plan.checkpoint.through_sequence,
            ) != plan):
        raise ValueError("checkpoint application requires a captured compaction plan")
    tail_start = (
        plan.retired_boundaries[-1].through_sequence
        if plan.retired_boundaries else execution.checkpoint.through_sequence
    )
    if plan.remaining_boundaries:
        following = plan.remaining_boundaries[0]
        if (not isinstance(following_source, ReviewGenerationArchiveFingerprint)
                or following_source.cursor != ReviewExecutionBoundaryCursor(
                    following.receipt_id, following.to_revision,
                )
                or following.through_sequence < plan.checkpoint.through_sequence):
            raise ValueError("checkpoint application lost its following boundary source")
        tail: CheckpointTailApplication = ApplyCheckpointTailByFollowingBoundary(
            following_source, _applied_digest(following.applied_sequences),
        )
    else:
        if following_source is not None:
            raise ValueError("checkpoint application cannot invent a following boundary")
        tail = ApplyAllCheckpointTailOutcomes()
    return ReviewCheckpointApplicationProposal(
        execution.checkpoint, plan.checkpoint, captured_revision, tail_start, tail,
    )
