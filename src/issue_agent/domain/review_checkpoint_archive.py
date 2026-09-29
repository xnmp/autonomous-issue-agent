"""Pure, resumable accounting for retired checkpoint source segments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from .review_checkpoint_chain import (
    ObservedReviewCheckpointChainPage, ObservedReviewCheckpointChainSource,
    ReviewCheckpointChainCursor,
)
from .review_checkpoint_application import (
    ReviewCheckpointApplicationReceipt, application_proposal_from_receipt,
)
from .review_execution import (
    ReviewExecutionBoundaryCursor, ReviewExecutionCompactionRoundSource,
    ReviewExecutionOrigin, ReviewGenerationArchiveFingerprint,
    same_execution_episode,
)
from .review_checkpoint_stream_replay import (
    _CheckpointReplayBetweenBoundaries,
    _CheckpointReplayTail, finish_checkpoint_stream_replay,
)
from .workflow_identity import validate_workflow_identifier


EMPTY_CHECKPOINT_ARCHIVE_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_CHECKPOINT_ARCHIVE_DUE_SECONDS = 86_400


def _digest(value: str, name: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} requires a SHA-256 digest")


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{name} requires an aware time")


def _due(now: datetime, due_seconds: int) -> datetime:
    _aware(now, "checkpoint audit time")
    if (type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_CHECKPOINT_ARCHIVE_DUE_SECONDS):
        raise ValueError("checkpoint audit requires a bounded due window")
    try:
        return now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("checkpoint audit due time is unrepresentable") from exc


@dataclass(frozen=True)
class ReviewCheckpointArchiveBaseline:
    tip_receipt_id: str
    chain_sha256: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.tip_receipt_id, "checkpoint audit baseline tip")
        _digest(self.chain_sha256, "checkpoint audit baseline chain")


@dataclass(frozen=True)
class ReviewCheckpointArchiveAwaiting:
    cursor: ReviewCheckpointChainCursor
    origin_source_sha256: str
    chain_sha256: str
    source_count: int
    baseline: ReviewCheckpointArchiveBaseline | None
    baseline_tail_sha256: str | None
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, ReviewCheckpointChainCursor):
            raise ValueError("checkpoint audit requires a chain cursor")
        _digest(self.origin_source_sha256, "checkpoint audit origin source")
        _digest(self.chain_sha256, "checkpoint audit chain")
        if (type(self.source_count) is not int or self.source_count < 0
                or self.source_count == 0
                and self.chain_sha256 != EMPTY_CHECKPOINT_ARCHIVE_SHA256):
            raise ValueError("checkpoint audit has an invalid source count")
        if (self.baseline is not None
                and not isinstance(self.baseline, ReviewCheckpointArchiveBaseline)):
            raise ValueError("checkpoint audit requires a typed baseline")
        if self.baseline_tail_sha256 is not None:
            if self.baseline is None:
                raise ValueError("checkpoint audit cannot have a tail without a baseline")
            _digest(self.baseline_tail_sha256, "checkpoint audit baseline tail")
        _aware(self.started_at, "checkpoint audit start")
        _aware(self.due_at, "checkpoint audit due")
        if self.started_at >= self.due_at:
            raise ValueError("checkpoint audit requires a future due time")


@dataclass(frozen=True)
class ReviewCheckpointArchiveSegment:
    parent: ReviewCheckpointArchiveAwaiting
    source: ObservedReviewCheckpointChainSource
    application: ReviewCheckpointApplicationReceipt
    application_source_sha256: str
    next_cursor: ReviewCheckpointChainCursor | None
    next_round_sequence: int
    next_generation: int
    preceding: ReviewExecutionBoundaryCursor | None
    round_stream_sha256: str
    generation_stream_sha256: str

    def __post_init__(self) -> None:
        if (not isinstance(self.parent, ReviewCheckpointArchiveAwaiting)
                or not isinstance(self.source, ObservedReviewCheckpointChainSource)
                or self.source.receipt_id != self.parent.cursor.next_receipt_id
                or self.source.receipt.checkpoint != self.parent.cursor.expected_checkpoint
                or not isinstance(self.application, ReviewCheckpointApplicationReceipt)
                or self.application.checkpoint_receipt_id != self.source.receipt_id
                or (self.next_cursor is None)
                != (self.source.receipt.previous_receipt_id is None)
                or self.next_cursor is not None
                and (not isinstance(self.next_cursor, ReviewCheckpointChainCursor)
                     or self.next_cursor.next_receipt_id
                     != self.source.receipt.previous_receipt_id)):
            raise ValueError("checkpoint segment lost its predecessor source")
        application_proposal_from_receipt(self.source.receipt, self.application)
        _digest(self.application_source_sha256, "checkpoint segment application source")
        previous = self.source.receipt.previous_checkpoint
        current = self.source.receipt.checkpoint
        if (type(self.next_round_sequence) is not int
                or not previous.through_sequence + 1 <= self.next_round_sequence
                <= current.through_sequence + 1
                or type(self.next_generation) is not int
                or not previous.state.binding.rejection.fence.generation + 1
                <= self.next_generation <= current.state.binding.rejection.fence.generation + 1
                or self.preceding is not None
                and not isinstance(self.preceding, ReviewExecutionBoundaryCursor)):
            raise ValueError("checkpoint segment requires bounded source frontiers")
        _digest(self.round_stream_sha256, "checkpoint segment round stream")
        _digest(self.generation_stream_sha256, "checkpoint segment generation stream")


@dataclass(frozen=True)
class ReviewCheckpointArchivePendingBoundary:
    progress: _CheckpointReplayBetweenBoundaries
    source: ReviewGenerationArchiveFingerprint
    through_sequence: int

    def __post_init__(self) -> None:
        if (not isinstance(self.progress, _CheckpointReplayBetweenBoundaries)
                or not isinstance(self.source, ReviewGenerationArchiveFingerprint)
                or self.source.cursor.revision.fence.generation
                != self.progress.next_generation
                or self.source.cursor.revision.fence.key
                != self.progress.state.binding.rejection.fence.key
                or type(self.through_sequence) is not int
                or not self.progress.next_round_sequence - 1
                <= self.through_sequence
                <= self.progress.application.checkpoint.through_sequence):
            raise ValueError("checkpoint replay audit requires a bounded pending source")


@dataclass(frozen=True)
class ReviewCheckpointArchiveReplaySegment:
    segment: ReviewCheckpointArchiveSegment
    replay: (_CheckpointReplayBetweenBoundaries | ReviewCheckpointArchivePendingBoundary
             | _CheckpointReplayTail)

    def __post_init__(self) -> None:
        if not isinstance(self.segment, ReviewCheckpointArchiveSegment):
            raise ValueError("checkpoint replay audit requires a typed segment")
        progress = (
            self.replay.progress
            if isinstance(self.replay, (ReviewCheckpointArchivePendingBoundary, _CheckpointReplayTail))
            else self.replay
        )
        if (not isinstance(progress, _CheckpointReplayBetweenBoundaries)
                or progress.application != application_proposal_from_receipt(
                    self.segment.source.receipt, self.segment.application,
                )
                or progress.next_round_sequence != self.segment.next_round_sequence
                or progress.next_generation != self.segment.next_generation
                or progress.preceding != self.segment.preceding):
            raise ValueError("checkpoint replay audit lost its source frontier")
        previous = self.segment.source.receipt.previous_checkpoint
        initial_round = previous.through_sequence + 1
        initial_generation = previous.state.binding.rejection.fence.generation + 1
        if (progress.next_generation == initial_generation
                and isinstance(self.replay, _CheckpointReplayBetweenBoundaries)
                and progress.next_round_sequence != initial_round
                or (progress.next_round_sequence == initial_round)
                != (self.segment.round_stream_sha256 == EMPTY_CHECKPOINT_ARCHIVE_SHA256)
                or (progress.next_generation == initial_generation
                    and not isinstance(self.replay, ReviewCheckpointArchivePendingBoundary))
                != (self.segment.generation_stream_sha256
                    == EMPTY_CHECKPOINT_ARCHIVE_SHA256)):
            raise ValueError("checkpoint replay audit has inconsistent source progress")


@dataclass(frozen=True)
class CompletedReviewCheckpointArchiveAudit:
    tip_receipt_id: str
    origin: ReviewExecutionOrigin
    origin_source_sha256: str
    chain_sha256: str
    source_count: int
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.tip_receipt_id, "completed checkpoint audit tip")
        if not isinstance(self.origin, ReviewExecutionOrigin):
            raise ValueError("completed checkpoint audit requires its origin")
        _digest(self.origin_source_sha256, "completed checkpoint audit origin source")
        _digest(self.chain_sha256, "completed checkpoint audit chain")
        if type(self.source_count) is not int or self.source_count < 1:
            raise ValueError("completed checkpoint audit requires a source")
        _aware(self.completed_at, "completed checkpoint audit time")
        _aware(self.next_due_at, "completed checkpoint audit due")
        if self.completed_at >= self.next_due_at:
            raise ValueError("completed checkpoint audit requires a future pass")


@dataclass(frozen=True)
class ReviewCheckpointArchiveStorageAnchor:
    tip_receipt_id: str
    origin_source_sha256: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.tip_receipt_id, "checkpoint audit storage tip")
        _digest(self.origin_source_sha256, "checkpoint audit storage origin")


@dataclass(frozen=True)
class BlockedReviewCheckpointArchiveAudit:
    prior: (ReviewCheckpointArchiveAwaiting | ReviewCheckpointArchiveSegment
            | ReviewCheckpointArchiveReplaySegment
            | ReviewCheckpointArchiveStorageAnchor
            | CompletedReviewCheckpointArchiveAudit)
    reason: str
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ReviewCheckpointArchiveAwaiting, ReviewCheckpointArchiveSegment,
            ReviewCheckpointArchiveReplaySegment,
            ReviewCheckpointArchiveStorageAnchor,
            CompletedReviewCheckpointArchiveAudit,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked checkpoint audit requires a discrepancy")
        _aware(self.detected_at, "checkpoint audit discrepancy")


ReviewCheckpointArchiveState = (
    ReviewCheckpointArchiveAwaiting | ReviewCheckpointArchiveSegment
    | ReviewCheckpointArchiveReplaySegment
    | CompletedReviewCheckpointArchiveAudit | BlockedReviewCheckpointArchiveAudit
)


def begin_review_checkpoint_archive_scan(
    cursor: ReviewCheckpointChainCursor, origin_source_sha256: str,
    now: datetime, due_seconds: int,
    baseline: ReviewCheckpointArchiveBaseline | None = None,
) -> ReviewCheckpointArchiveAwaiting:
    if not isinstance(cursor, ReviewCheckpointChainCursor):
        raise ValueError("checkpoint audit requires a typed tip")
    _digest(origin_source_sha256, "checkpoint audit origin source")
    return ReviewCheckpointArchiveAwaiting(
        cursor, origin_source_sha256, EMPTY_CHECKPOINT_ARCHIVE_SHA256,
        0, baseline, None, now, _due(now, due_seconds),
    )


def start_review_checkpoint_archive_segment(
    state: ReviewCheckpointArchiveAwaiting,
    page: ObservedReviewCheckpointChainPage,
    application: ReviewCheckpointApplicationReceipt,
    application_source_sha256: str,
) -> ReviewCheckpointArchiveSegment:
    if (not isinstance(state, ReviewCheckpointArchiveAwaiting)
            or not isinstance(page, ObservedReviewCheckpointChainPage)
            or len(page.sources) != 1):
        raise ValueError("checkpoint audit requires one predecessor source")
    source = page.sources[0]
    previous = source.receipt.previous_checkpoint
    return ReviewCheckpointArchiveSegment(
        state, source, application, application_source_sha256, page.next_cursor,
        previous.through_sequence + 1,
        previous.state.binding.rejection.fence.generation + 1,
        previous.boundary_cursor,
        EMPTY_CHECKPOINT_ARCHIVE_SHA256, EMPTY_CHECKPOINT_ARCHIVE_SHA256,
    )


def fold_checkpoint_round_stream(
    stream_sha256: str, source: ReviewExecutionCompactionRoundSource,
) -> str:
    _digest(stream_sha256, "checkpoint round stream")
    if not isinstance(source, ReviewExecutionCompactionRoundSource):
        raise ValueError("checkpoint round stream requires a typed source")
    encoded = "\0".join((
        str(source.round.sequence), source.round.round_id,
        str(source.round.binding.rejection.fence.generation),
        source.row_sha256, source.receipt_set_sha256,
    )).encode()
    return hashlib.sha256(bytes.fromhex(stream_sha256) + encoded).hexdigest()


def advance_review_checkpoint_archive_round(
    segment: ReviewCheckpointArchiveSegment,
    source: ReviewExecutionCompactionRoundSource,
) -> ReviewCheckpointArchiveSegment:
    if (not isinstance(segment, ReviewCheckpointArchiveSegment)
            or not isinstance(source, ReviewExecutionCompactionRoundSource)
            or source.round.sequence != segment.next_round_sequence
            or not same_execution_episode(
                segment.source.receipt.checkpoint.state.binding, source.round.binding,
            ) or not segment.source.receipt.previous_checkpoint.state.binding.rejection.fence.generation
            <= source.round.binding.rejection.fence.generation
            <= segment.source.receipt.checkpoint.state.binding.rejection.fence.generation):
        raise ValueError("checkpoint audit round disagrees with its segment")
    return ReviewCheckpointArchiveSegment(
        segment.parent, segment.source, segment.application,
        segment.application_source_sha256, segment.next_cursor,
        segment.next_round_sequence + 1, segment.next_generation, segment.preceding,
        fold_checkpoint_round_stream(segment.round_stream_sha256, source),
        segment.generation_stream_sha256,
    )


def fold_checkpoint_generation_stream(
    stream_sha256: str, generation: int, source: ReviewGenerationArchiveFingerprint,
) -> str:
    _digest(stream_sha256, "checkpoint generation stream")
    if (type(generation) is not int or generation < 1
            or not isinstance(source, ReviewGenerationArchiveFingerprint)
            or source.cursor.revision.fence.generation != generation):
        raise ValueError("checkpoint generation stream requires its typed source")
    encoded = "\0".join((
        str(generation), source.cursor.receipt_id,
        str(source.cursor.revision.version), source.receipt_sha256,
        source.accepted_namespace, source.accepted_key, source.accepted_sha256,
    )).encode()
    return hashlib.sha256(bytes.fromhex(stream_sha256) + encoded).hexdigest()


def advance_review_checkpoint_archive_generation(
    segment: ReviewCheckpointArchiveSegment,
    source: ReviewGenerationArchiveFingerprint,
) -> ReviewCheckpointArchiveSegment:
    if (not isinstance(segment, ReviewCheckpointArchiveSegment)
            or not isinstance(source, ReviewGenerationArchiveFingerprint)
            or source.cursor.revision.fence.generation != segment.next_generation
            or source.cursor.revision.fence.key
            != segment.source.receipt.checkpoint.state.binding.rejection.fence.key):
        raise ValueError("checkpoint audit generation disagrees with its segment")
    return ReviewCheckpointArchiveSegment(
        segment.parent, segment.source, segment.application,
        segment.application_source_sha256, segment.next_cursor,
        segment.next_round_sequence, segment.next_generation + 1, source.cursor,
        segment.round_stream_sha256,
        fold_checkpoint_generation_stream(
            segment.generation_stream_sha256, segment.next_generation, source,
        ),
    )


def finish_review_checkpoint_archive_segment(
    segment: ReviewCheckpointArchiveSegment, now: datetime, due_seconds: int,
) -> ReviewCheckpointArchiveAwaiting | CompletedReviewCheckpointArchiveAudit | BlockedReviewCheckpointArchiveAudit:
    if not isinstance(segment, ReviewCheckpointArchiveSegment):
        raise ValueError("checkpoint audit completion requires a typed segment")
    _aware(now, "checkpoint audit completion")
    if now < segment.parent.started_at:
        raise ValueError("checkpoint audit time moved backwards")
    receipt = segment.source.receipt
    if (segment.next_round_sequence != receipt.checkpoint.through_sequence + 1
            or segment.next_generation
            != receipt.checkpoint.state.binding.rejection.fence.generation + 1):
        raise ValueError("checkpoint audit segment has unread sources")
    if (segment.round_stream_sha256 != receipt.round_stream_sha256
            or segment.generation_stream_sha256 != receipt.generation_stream_sha256
            or segment.preceding != receipt.checkpoint.boundary_cursor):
        return BlockedReviewCheckpointArchiveAudit(
            segment, "checkpoint retired source streams disagree with receipt", now,
        )
    encoded = "\0".join((
        segment.source.receipt_id, segment.source.source_sha256,
        segment.application_source_sha256, segment.round_stream_sha256,
        segment.generation_stream_sha256,
    )).encode()
    parent = segment.parent
    chain = hashlib.sha256(bytes.fromhex(parent.chain_sha256) + encoded).hexdigest()
    tail = parent.baseline_tail_sha256
    if parent.baseline is not None and (
        tail is not None or segment.source.receipt_id == parent.baseline.tip_receipt_id
    ):
        tail = hashlib.sha256(bytes.fromhex(tail or EMPTY_CHECKPOINT_ARCHIVE_SHA256)
                              + encoded).hexdigest()
    if segment.next_cursor is not None:
        return ReviewCheckpointArchiveAwaiting(
            segment.next_cursor, parent.origin_source_sha256, chain,
            parent.source_count + 1, parent.baseline, tail,
            parent.started_at, parent.due_at,
        )
    if parent.baseline is not None and tail != parent.baseline.chain_sha256:
        return BlockedReviewCheckpointArchiveAudit(
            segment, "checkpoint predecessor chain changed since its prior pass", now,
        )
    return CompletedReviewCheckpointArchiveAudit(
        parent.cursor.tip_receipt_id, parent.cursor.origin,
        parent.origin_source_sha256, chain, parent.source_count + 1,
        now, _due(now, due_seconds),
    )


def finish_review_checkpoint_archive_replay_segment(
    state: ReviewCheckpointArchiveReplaySegment, now: datetime, due_seconds: int,
) -> ReviewCheckpointArchiveAwaiting | CompletedReviewCheckpointArchiveAudit | BlockedReviewCheckpointArchiveAudit:
    if not isinstance(state, ReviewCheckpointArchiveReplaySegment):
        raise ValueError("checkpoint replay audit requires a typed segment")
    if not isinstance(state.replay, _CheckpointReplayTail):
        raise ValueError("checkpoint replay audit has an unfinished source")
    finish_checkpoint_stream_replay(state.replay)
    return finish_review_checkpoint_archive_segment(state.segment, now, due_seconds)


def review_checkpoint_archive_overdue(state: ReviewCheckpointArchiveState, now: datetime) -> bool:
    _aware(now, "checkpoint audit due check")
    if isinstance(state, BlockedReviewCheckpointArchiveAudit):
        return True
    if isinstance(state, ReviewCheckpointArchiveAwaiting):
        return now >= state.due_at
    if isinstance(state, ReviewCheckpointArchiveSegment):
        return now >= state.parent.due_at
    if isinstance(state, ReviewCheckpointArchiveReplaySegment):
        return now >= state.segment.parent.due_at
    if isinstance(state, CompletedReviewCheckpointArchiveAudit):
        return now >= state.next_due_at
    raise ValueError("checkpoint audit due check requires a typed state")
