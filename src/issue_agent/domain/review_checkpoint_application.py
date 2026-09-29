"""Captured outcome applicability for a proposed review checkpoint."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .review_execution import (
    ActivatedReviewExecution, MAX_EXECUTION_SUFFIX, ReviewExecutionBoundaryCursor,
    ReviewExecutionCheckpoint, ReviewExecutionCheckpointReceipt,
    ReviewExecutionCompactionPlan,
    ReviewExecutionGenerationBoundary, ReviewGenerationArchiveFingerprint,
    ReviewRoundOutcome, plan_review_execution_compaction,
    replay_review_execution_generations, same_execution_episode,
)
from .workflow_identity import WorkflowRevision, validate_workflow_identifier


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


@dataclass(frozen=True)
class ReviewCheckpointApplicationReceipt:
    """Small durable tail choice keyed by its separately minted checkpoint receipt."""

    checkpoint_receipt_id: str
    captured_revision: WorkflowRevision
    tail_start_sequence: int
    tail: CheckpointTailApplication

    def __post_init__(self) -> None:
        validate_workflow_identifier(
            self.checkpoint_receipt_id, "checkpoint application receipt identity",
        )
        if (not isinstance(self.captured_revision, WorkflowRevision)
                or type(self.tail_start_sequence) is not int
                or self.tail_start_sequence < 0
                or not isinstance(self.tail, (
                    ApplyAllCheckpointTailOutcomes,
                    ApplyCheckpointTailByFollowingBoundary,
                ))):
            raise ValueError("checkpoint application receipt requires a typed tail choice")


def application_receipt_from_proposal(
    checkpoint_receipt_id: str,
    proposal: ReviewCheckpointApplicationProposal,
) -> ReviewCheckpointApplicationReceipt:
    if not isinstance(proposal, ReviewCheckpointApplicationProposal):
        raise ValueError("checkpoint application receipt requires a typed proposal")
    return ReviewCheckpointApplicationReceipt(
        checkpoint_receipt_id, proposal.captured_revision,
        proposal.tail_start_sequence, proposal.tail,
    )


def application_proposal_from_receipt(
    checkpoint: ReviewExecutionCheckpointReceipt,
    application: ReviewCheckpointApplicationReceipt,
) -> ReviewCheckpointApplicationProposal:
    if (not isinstance(checkpoint, ReviewExecutionCheckpointReceipt)
            or not isinstance(application, ReviewCheckpointApplicationReceipt)):
        raise ValueError("checkpoint application requires typed paired receipts")
    return ReviewCheckpointApplicationProposal(
        checkpoint.previous_checkpoint, checkpoint.checkpoint,
        application.captured_revision, application.tail_start_sequence,
        application.tail,
    )


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


def validate_checkpoint_following_boundary(
    application: ReviewCheckpointApplicationProposal,
    following_boundary: ReviewExecutionGenerationBoundary | None,
    following_source: ReviewGenerationArchiveFingerprint | None,
) -> None:
    """Check the first retained boundary against a recorded checkpoint tail choice."""
    if not isinstance(application, ReviewCheckpointApplicationProposal):
        raise ValueError("checkpoint application requires a typed proposal")
    checkpoint = application.checkpoint
    if following_boundary is not None and (
        not isinstance(following_boundary, ReviewExecutionGenerationBoundary)
        or not isinstance(following_source, ReviewGenerationArchiveFingerprint)
        or following_source.cursor != ReviewExecutionBoundaryCursor(
            following_boundary.receipt_id, following_boundary.to_revision,
        ) or following_boundary.before.binding != checkpoint.state.binding
        or following_boundary.to_revision.fence.generation
        != checkpoint.state.binding.rejection.fence.generation + 1
        or following_boundary.through_sequence < checkpoint.through_sequence
    ):
        raise ValueError("checkpoint replay has an invalid following boundary source")
    if following_boundary is None and following_source is not None:
        raise ValueError("checkpoint replay has an unpaired following source")
    if isinstance(application.tail, ApplyAllCheckpointTailOutcomes):
        if (following_boundary is not None
                and following_boundary.to_revision.version
                <= application.captured_revision.version):
            raise ValueError("checkpoint replay missed a captured following boundary")
    elif (following_boundary is None
          or following_source != application.tail.source
          or following_boundary.to_revision.version
          > application.captured_revision.version
          or _applied_digest(following_boundary.applied_sequences)
          != application.tail.applied_sequences_sha256):
        raise ValueError("checkpoint replay changed its captured tail selection")


def replay_review_checkpoint_application(
    application: ReviewCheckpointApplicationProposal,
    retired_outcomes: tuple[ReviewRoundOutcome, ...],
    retired_boundaries: tuple[ReviewExecutionGenerationBoundary, ...],
    following_boundary: ReviewExecutionGenerationBoundary | None,
    following_source: ReviewGenerationArchiveFingerprint | None,
) -> ReviewExecutionCheckpoint:
    """Check projected checkpoint state from a complete, source-verified segment.

    The caller must prove exact archive membership, source fingerprints and
    the absence of an earlier following boundary. This pure check grants no
    authority from caller-supplied objects by itself.
    """
    if (not isinstance(application, ReviewCheckpointApplicationProposal)
            or not isinstance(retired_outcomes, tuple)
            or not isinstance(retired_boundaries, tuple)
            or len(retired_outcomes) > MAX_EXECUTION_SUFFIX
            or len(retired_boundaries) > MAX_EXECUTION_SUFFIX
            or any(not isinstance(outcome, ReviewRoundOutcome) for outcome in retired_outcomes)
            or any(not isinstance(boundary, ReviewExecutionGenerationBoundary)
                   for boundary in retired_boundaries)):
        raise ValueError("checkpoint replay requires typed archived sources")
    previous, checkpoint = application.previous_checkpoint, application.checkpoint
    if (checkpoint.through_sequence - previous.through_sequence > MAX_EXECUTION_SUFFIX
            or checkpoint.state.binding.rejection.fence.generation
            - previous.state.binding.rejection.fence.generation > MAX_EXECUTION_SUFFIX):
        raise ValueError("checkpoint replay exceeds its archived segment bound")
    if tuple(outcome.round.sequence for outcome in retired_outcomes) != tuple(
        range(previous.through_sequence + 1, checkpoint.through_sequence + 1)
    ):
        raise ValueError("checkpoint replay requires every retired outcome in order")
    if tuple(boundary.to_revision.fence.generation for boundary in retired_boundaries) != tuple(
        range(previous.state.binding.rejection.fence.generation + 1,
              checkpoint.state.binding.rejection.fence.generation + 1)
    ):
        raise ValueError("checkpoint replay requires every retired boundary in order")
    last_cursor = (
        ReviewExecutionBoundaryCursor(
            retired_boundaries[-1].receipt_id, retired_boundaries[-1].to_revision,
        ) if retired_boundaries else previous.boundary_cursor
    )
    tail_start = (
        retired_boundaries[-1].through_sequence
        if retired_boundaries else previous.through_sequence
    )
    if (checkpoint.boundary_cursor != last_cursor
            or application.tail_start_sequence != tail_start):
        raise ValueError("checkpoint replay lost its retired frontier")
    validate_checkpoint_following_boundary(application, following_boundary, following_source)
    if isinstance(application.tail, ApplyAllCheckpointTailOutcomes):
        selected = retired_outcomes
    else:
        if following_boundary is None:
            raise ValueError("checkpoint replay changed its captured tail selection")
        applied = frozenset(following_boundary.applied_sequences)
        selected = tuple(
            outcome for outcome in retired_outcomes
            if outcome.round.sequence <= tail_start or outcome.round.sequence in applied
        )
    replayed = replay_review_execution_generations(previous, selected, retired_boundaries)
    expected = ReviewExecutionCheckpoint(
        checkpoint.through_sequence, replayed, previous.policy, last_cursor,
    )
    if expected != checkpoint:
        raise ValueError("checkpoint state differs from archived outcome replay")
    return expected
