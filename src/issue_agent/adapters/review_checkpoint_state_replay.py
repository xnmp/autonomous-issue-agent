"""Transaction-local checkpoint state replay from retained SQL sources."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.review_checkpoint_archive import (
    EMPTY_CHECKPOINT_ARCHIVE_SHA256, fold_checkpoint_generation_stream,
    fold_checkpoint_round_stream,
)
from ..domain.review_checkpoint_stream_replay import (
    advance_checkpoint_replay_boundary_round, advance_checkpoint_replay_tail_round,
    begin_checkpoint_stream_replay, finish_checkpoint_replay_boundary,
    finish_checkpoint_stream_replay, start_checkpoint_replay_boundary,
    start_checkpoint_replay_tail,
)
from ..domain.review_execution import (
    MAX_EXECUTION_SUFFIX, ReviewExecutionBinding, ReviewExecutionCheckpoint,
    ReviewRoundOutcome, same_execution_episode,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_checkpoint_application_source import read_review_checkpoint_application_source
from .review_checkpoint_following_source import (
    checkpoint_following_fingerprint, read_checkpoint_following_application_source,
)
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, ObservedReviewGenerationArchivePage,
    read_checkpoint_round_by_sequence, read_review_generation_archive_page,
)
from .review_round_archive_audit import _current_execution
from .review_round_receipt_sweep import read_classified_checkpoint_round_receipt_members
from .workflow_rows import WorkflowAuthorityError, _read_execution_origin, workflow_scope


MAX_CHECKPOINT_STATE_REPLAY_SOURCE_BYTES = 2**27


@dataclass(frozen=True)
class ObservedReviewCheckpointStateReplay:
    """One checked segment, without chain membership or admission authority."""

    checkpoint_receipt_id: str
    checkpoint: ReviewExecutionCheckpoint
    source_bytes: int

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.checkpoint_receipt_id, "checkpoint replay receipt")
        if (not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or type(self.source_bytes) is not int
                or not 0 < self.source_bytes <= MAX_CHECKPOINT_STATE_REPLAY_SOURCE_BYTES):
            raise ValueError("checkpoint replay requires a bounded verified segment")


def _read_replay_round(
    db: sqlite3.Connection, binding: ReviewExecutionBinding, sequence: int,
    round_stream: str, source_bytes: int, max_source_bytes: int,
) -> tuple[ReviewRoundOutcome, str, int]:
    observed = read_checkpoint_round_by_sequence(db, binding, sequence)
    classified = read_classified_checkpoint_round_receipt_members(
        db, observed.source.round,
    )
    if classified.source.source != observed.source:
        raise WorkflowAuthorityError("checkpoint replay round changed during classification")
    next_bytes = source_bytes + observed.source_bytes
    if next_bytes > max_source_bytes:
        raise WorkflowAuthorityError("checkpoint replay exceeds its source byte bound")
    return (
        observed.outcome,
        fold_checkpoint_round_stream(round_stream, observed.source),
        next_bytes,
    )


def observe_review_checkpoint_state_replay(
    db: sqlite3.Connection, key: WorkflowKey, checkpoint_receipt_id: str,
    *, max_source_bytes: int = MAX_CHECKPOINT_STATE_REPLAY_SOURCE_BYTES,
) -> ObservedReviewCheckpointStateReplay:
    """Replay one historical segment; exact chain/census proofs remain separate.

    Each source reader has its own read ceiling. The aggregate ceiling here is
    checked after each bounded read and is not yet a strict cumulative SQL
    read limit or a durable paged audit cursor.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or workflow_scope(key)[1] != "pr"
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_CHECKPOINT_STATE_REPLAY_SOURCE_BYTES):
        raise ValueError("checkpoint state replay requires a transaction and bounded inputs")
    validate_workflow_identifier(checkpoint_receipt_id, "checkpoint replay receipt")
    execution = _current_execution(db, key)
    origin, _ = _read_execution_origin(db, workflow_scope(key), execution)
    recorded = read_review_checkpoint_application_source(
        db, key.repository, checkpoint_receipt_id,
    )
    application, receipt = recorded.application, recorded.checkpoint
    previous, checkpoint = receipt.previous_checkpoint, receipt.checkpoint
    if (not same_execution_episode(origin.checkpoint.state.binding, previous.state.binding)
            or previous.through_sequence < origin.checkpoint.through_sequence
            or previous.state.binding.rejection.fence.generation
            < origin.checkpoint.state.binding.rejection.fence.generation
            or checkpoint.through_sequence > execution.last_sequence
            or checkpoint.state.binding.rejection.fence.generation
            > execution.binding.rejection.fence.generation
            or checkpoint.through_sequence - previous.through_sequence > MAX_EXECUTION_SUFFIX
            or checkpoint.state.binding.rejection.fence.generation
            - previous.state.binding.rejection.fence.generation > MAX_EXECUTION_SUFFIX):
        raise WorkflowAuthorityError("checkpoint replay source is outside current execution")
    following = read_checkpoint_following_application_source(
        db, origin.checkpoint.state.binding,
        execution.binding.rejection.fence.generation, application,
    )
    following_fingerprint = (
        checkpoint_following_fingerprint(following) if following is not None else None
    )
    source_bytes = following.source_bytes if following is not None else 0
    if source_bytes > max_source_bytes:
        raise WorkflowAuthorityError("checkpoint replay exceeds its source byte bound")
    round_stream = EMPTY_CHECKPOINT_ARCHIVE_SHA256
    generation_stream = EMPTY_CHECKPOINT_ARCHIVE_SHA256
    progress = begin_checkpoint_stream_replay(application)

    try:
        while progress.next_generation <= checkpoint.state.binding.rejection.fence.generation:
            generation = progress.next_generation
            page = read_review_generation_archive_page(
                db, origin.checkpoint.state.binding, generation, generation,
                preceding=progress.preceding, limit=1,
            )
            if isinstance(page, BlockedReviewGenerationArchivePage):
                raise WorkflowAuthorityError(
                    f"checkpoint replay generation {generation} is unreadable: {page.reason}"
                )
            if not isinstance(page, ObservedReviewGenerationArchivePage) or len(page.sources) != 1:
                raise WorkflowAuthorityError("checkpoint replay generation omitted its source")
            source = page.sources[0]
            source_bytes += source.source_bytes
            if source_bytes > max_source_bytes:
                raise WorkflowAuthorityError("checkpoint replay exceeds its source byte bound")
            generation_stream = fold_checkpoint_generation_stream(
                generation_stream, generation, checkpoint_following_fingerprint(source),
            )
            pending = start_checkpoint_replay_boundary(progress, source.boundary)
            while pending.progress.next_round_sequence <= source.boundary.through_sequence:
                outcome, round_stream, source_bytes = _read_replay_round(
                    db, origin.checkpoint.state.binding,
                    pending.progress.next_round_sequence,
                    round_stream, source_bytes, max_source_bytes,
                )
                pending = advance_checkpoint_replay_boundary_round(pending, outcome)
            progress = finish_checkpoint_replay_boundary(pending)
        tail = start_checkpoint_replay_tail(
            progress, following.boundary if following is not None else None,
            following_fingerprint,
        )
        while tail.progress.next_round_sequence <= checkpoint.through_sequence:
            outcome, round_stream, source_bytes = _read_replay_round(
                db, origin.checkpoint.state.binding,
                tail.progress.next_round_sequence,
                round_stream, source_bytes, max_source_bytes,
            )
            tail = advance_checkpoint_replay_tail_round(tail, outcome)
        replayed = finish_checkpoint_stream_replay(tail)
        if (round_stream != receipt.round_stream_sha256
                or generation_stream != receipt.generation_stream_sha256
                or tail.progress.preceding != checkpoint.boundary_cursor):
            raise WorkflowAuthorityError("checkpoint replay source streams disagree with receipt")
        return ObservedReviewCheckpointStateReplay(
            checkpoint_receipt_id, replayed, source_bytes,
        )
    except WorkflowAuthorityError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("checkpoint archived state replay is unreadable") from exc
