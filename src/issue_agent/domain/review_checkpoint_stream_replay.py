"""One-source-at-a-time checkpoint replay over caller-verified archive members.

The reducer checks projection semantics but cannot prove source membership,
fingerprints or durable ancestry from caller-supplied objects by itself. Its
intermediate states are persisted only inside a versioned archive-audit cursor;
the adapter must recheck its source anchors when resuming.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace

from .review_checkpoint_application import (
    ApplyAllCheckpointTailOutcomes, ApplyCheckpointTailByFollowingBoundary,
    ReviewCheckpointApplicationProposal, _applied_digest,
    validate_checkpoint_following_boundary,
)
from .review_execution import (
    MAX_EXECUTION_SUFFIX, ReviewExecutionBoundaryCursor,
    ReviewExecutionGenerationBoundary,
    ReviewExecutionCheckpoint, ReviewExecutionState, ReviewGenerationArchiveFingerprint,
    ReviewRoundOutcome, apply_review_execution_replay_outcome,
    same_execution_episode,
)


@dataclass(frozen=True)
class _CheckpointReplayBetweenBoundaries:
    application: ReviewCheckpointApplicationProposal
    state: ReviewExecutionState
    next_round_sequence: int
    next_generation: int
    preceding: ReviewExecutionBoundaryCursor | None

    def __post_init__(self) -> None:
        if (not isinstance(self.application, ReviewCheckpointApplicationProposal)
                or not isinstance(self.state, ReviewExecutionState)):
            raise ValueError("checkpoint replay requires typed application and state")
        previous, checkpoint = self.application.previous_checkpoint, self.application.checkpoint
        if (not same_execution_episode(previous.state.binding, self.state.binding)
                or checkpoint.through_sequence - previous.through_sequence > MAX_EXECUTION_SUFFIX
                or checkpoint.state.binding.rejection.fence.generation
                - previous.state.binding.rejection.fence.generation > MAX_EXECUTION_SUFFIX
                or type(self.next_round_sequence) is not int
                or not previous.through_sequence + 1 <= self.next_round_sequence
                <= checkpoint.through_sequence + 1
                or type(self.next_generation) is not int
                or not previous.state.binding.rejection.fence.generation + 1
                <= self.next_generation
                <= checkpoint.state.binding.rejection.fence.generation + 1
                or self.state.binding.rejection.fence.generation != self.next_generation - 1
                or self.preceding is not None
                and (not isinstance(self.preceding, ReviewExecutionBoundaryCursor)
                     or self.preceding.revision.fence
                     != self.state.binding.rejection.fence)
                or self.next_generation
                == previous.state.binding.rejection.fence.generation + 1
                and self.preceding != previous.boundary_cursor):
            raise ValueError("checkpoint replay has an invalid generation frontier")


@dataclass(frozen=True)
class _CheckpointReplayWithinBoundary:
    progress: _CheckpointReplayBetweenBoundaries
    boundary: ReviewExecutionGenerationBoundary

    def __post_init__(self) -> None:
        if (not isinstance(self.progress, _CheckpointReplayBetweenBoundaries)
                or not isinstance(self.boundary, ReviewExecutionGenerationBoundary)):
            raise ValueError("checkpoint replay requires a typed pending boundary")
        progress, boundary = self.progress, self.boundary
        checkpoint = progress.application.checkpoint
        if (boundary.before.binding != progress.state.binding
                or boundary.from_revision.fence != progress.state.binding.rejection.fence
                or boundary.to_revision.fence.generation != progress.next_generation
                or boundary.through_sequence > checkpoint.through_sequence
                or progress.next_round_sequence > boundary.through_sequence + 1
                or progress.preceding is not None
                and boundary.from_revision.version < progress.preceding.revision.version):
            raise ValueError("checkpoint replay boundary disagrees with its frontier")


@dataclass(frozen=True)
class _CheckpointReplayTail:
    progress: _CheckpointReplayBetweenBoundaries
    applied_sequences: tuple[int, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.progress, _CheckpointReplayBetweenBoundaries)
                or not isinstance(self.applied_sequences, tuple)):
            raise ValueError("checkpoint replay requires a typed tail")
        application = self.progress.application
        if (self.progress.next_generation
                != application.checkpoint.state.binding.rejection.fence.generation + 1
                or self.progress.preceding != application.checkpoint.boundary_cursor
                or self.progress.next_round_sequence <= application.tail_start_sequence):
            raise ValueError("checkpoint replay tail lost its retired frontier")
        if isinstance(application.tail, ApplyAllCheckpointTailOutcomes):
            if self.applied_sequences:
                raise ValueError("all checkpoint tail outcomes cannot have a selection")
        elif (not isinstance(application.tail, ApplyCheckpointTailByFollowingBoundary)
              or _applied_digest(self.applied_sequences)
              != application.tail.applied_sequences_sha256):
            raise ValueError("checkpoint replay tail selection changed")


def begin_checkpoint_stream_replay(
    application: ReviewCheckpointApplicationProposal,
) -> _CheckpointReplayBetweenBoundaries:
    if not isinstance(application, ReviewCheckpointApplicationProposal):
        raise ValueError("checkpoint replay requires a typed application")
    previous = application.previous_checkpoint
    return _CheckpointReplayBetweenBoundaries(
        application, previous.state, previous.through_sequence + 1,
        previous.state.binding.rejection.fence.generation + 1,
        previous.boundary_cursor,
    )


def start_checkpoint_replay_boundary(
    progress: _CheckpointReplayBetweenBoundaries,
    boundary: ReviewExecutionGenerationBoundary,
) -> _CheckpointReplayWithinBoundary:
    if not isinstance(progress, _CheckpointReplayBetweenBoundaries):
        raise ValueError("checkpoint replay requires a generation frontier")
    if progress.next_generation > progress.application.checkpoint.state.binding.rejection.fence.generation:
        raise ValueError("checkpoint replay has no retired boundary remaining")
    pending = _CheckpointReplayWithinBoundary(progress, boundary)
    initial_generation = (
        progress.application.previous_checkpoint.state.binding.rejection.fence.generation + 1
    )
    if any(
        sequence < progress.next_round_sequence
        and not (progress.next_generation == initial_generation
                 and sequence <= progress.application.previous_checkpoint.through_sequence)
        for sequence in boundary.applied_sequences
    ):
        raise ValueError("checkpoint replay boundary reuses an earlier outcome")
    return pending


def advance_checkpoint_replay_boundary_round(
    pending: _CheckpointReplayWithinBoundary,
    outcome: ReviewRoundOutcome,
) -> _CheckpointReplayWithinBoundary:
    if not isinstance(pending, _CheckpointReplayWithinBoundary):
        raise ValueError("checkpoint replay requires a pending boundary")
    progress = pending.progress
    if (not isinstance(outcome, ReviewRoundOutcome)
            or progress.next_round_sequence > pending.boundary.through_sequence
            or outcome.round.sequence != progress.next_round_sequence
            or outcome.round.binding != progress.state.binding):
        raise ValueError("checkpoint replay boundary round disagrees with allocation")
    sequences = pending.boundary.applied_sequences
    position = bisect_left(sequences, progress.next_round_sequence)
    selected = position < len(sequences) and sequences[position] == progress.next_round_sequence
    state = (
        apply_review_execution_replay_outcome(
            progress.state, outcome, progress.application.checkpoint.policy,
        ) if selected else progress.state
    )
    return _CheckpointReplayWithinBoundary(
        replace(progress, state=state, next_round_sequence=progress.next_round_sequence + 1),
        pending.boundary,
    )


def finish_checkpoint_replay_boundary(
    pending: _CheckpointReplayWithinBoundary,
) -> _CheckpointReplayBetweenBoundaries:
    if not isinstance(pending, _CheckpointReplayWithinBoundary):
        raise ValueError("checkpoint replay requires a pending boundary")
    progress, boundary = pending.progress, pending.boundary
    if (progress.next_round_sequence != boundary.through_sequence + 1
            or progress.state != boundary.before):
        raise ValueError("checkpoint replay boundary differs from archived outcomes")
    return _CheckpointReplayBetweenBoundaries(
        progress.application, boundary.after, progress.next_round_sequence,
        progress.next_generation + 1,
        ReviewExecutionBoundaryCursor(boundary.receipt_id, boundary.to_revision),
    )


def start_checkpoint_replay_tail(
    progress: _CheckpointReplayBetweenBoundaries,
    following_boundary: ReviewExecutionGenerationBoundary | None,
    following_source: ReviewGenerationArchiveFingerprint | None,
) -> _CheckpointReplayTail:
    if not isinstance(progress, _CheckpointReplayBetweenBoundaries):
        raise ValueError("checkpoint replay requires a generation frontier")
    application = progress.application
    if progress.next_round_sequence != application.tail_start_sequence + 1:
        raise ValueError("checkpoint replay tail lost its applied frontier")
    previous = application.previous_checkpoint
    if (progress.next_generation
            == previous.state.binding.rejection.fence.generation + 1
            and (progress.state != previous.state
                 or progress.next_round_sequence != previous.through_sequence + 1
                 or progress.preceding != previous.boundary_cursor)):
        raise ValueError("checkpoint replay tail skipped its previous checkpoint")
    validate_checkpoint_following_boundary(
        application, following_boundary, following_source,
    )
    sequences = (
        following_boundary.applied_sequences
        if isinstance(application.tail, ApplyCheckpointTailByFollowingBoundary)
        and following_boundary is not None else ()
    )
    return _CheckpointReplayTail(progress, sequences)


def advance_checkpoint_replay_tail_round(
    tail: _CheckpointReplayTail,
    outcome: ReviewRoundOutcome,
) -> _CheckpointReplayTail:
    if not isinstance(tail, _CheckpointReplayTail):
        raise ValueError("checkpoint replay requires a tail frontier")
    progress = tail.progress
    if (not isinstance(outcome, ReviewRoundOutcome)
            or progress.next_round_sequence > progress.application.checkpoint.through_sequence
            or outcome.round.sequence != progress.next_round_sequence
            or outcome.round.binding != progress.state.binding):
        raise ValueError("checkpoint replay tail round disagrees with allocation")
    sequences = tail.applied_sequences
    position = bisect_left(sequences, progress.next_round_sequence)
    selected = (
        isinstance(progress.application.tail, ApplyAllCheckpointTailOutcomes)
        or position < len(sequences) and sequences[position] == progress.next_round_sequence
    )
    state = (
        apply_review_execution_replay_outcome(
            progress.state, outcome, progress.application.checkpoint.policy,
        ) if selected else progress.state
    )
    return _CheckpointReplayTail(
        replace(progress, state=state, next_round_sequence=progress.next_round_sequence + 1),
        sequences,
    )


def finish_checkpoint_stream_replay(tail: _CheckpointReplayTail) -> ReviewExecutionCheckpoint:
    if not isinstance(tail, _CheckpointReplayTail):
        raise ValueError("checkpoint replay requires a completed tail")
    progress = tail.progress
    checkpoint = progress.application.checkpoint
    if (progress.next_round_sequence != checkpoint.through_sequence + 1
            or progress.state != checkpoint.state):
        raise ValueError("checkpoint state differs from archived outcome replay")
    return checkpoint
