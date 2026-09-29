"""Bounded observations of a review execution checkpoint predecessor chain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .review_execution import (
    ReviewExecutionCheckpoint, ReviewExecutionCheckpointReceipt,
    ReviewExecutionOrigin,
)
from .workflow_identity import validate_workflow_identifier


MAX_REVIEW_CHECKPOINT_CHAIN_PAGE = 16


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{name} requires an aware time")


@dataclass(frozen=True)
class ReviewCheckpointChainCursor:
    tip_receipt_id: str
    tip_checkpoint: ReviewExecutionCheckpoint
    origin: ReviewExecutionOrigin
    next_receipt_id: str
    expected_checkpoint: ReviewExecutionCheckpoint
    newer_claimed_at: datetime | None

    def __post_init__(self) -> None:
        for name, value in (("tip", self.tip_receipt_id),
                            ("next", self.next_receipt_id)):
            validate_workflow_identifier(value, f"checkpoint chain {name} receipt")
        if (not isinstance(self.tip_checkpoint, ReviewExecutionCheckpoint)
                or not isinstance(self.expected_checkpoint, ReviewExecutionCheckpoint)
                or not isinstance(self.origin, ReviewExecutionOrigin)):
            raise ValueError("checkpoint chain cursor requires typed source identities")
        if self.newer_claimed_at is not None:
            _aware(self.newer_claimed_at, "checkpoint chain claim")


@dataclass(frozen=True)
class ObservedReviewCheckpointChainSource:
    receipt_id: str
    receipt: ReviewExecutionCheckpointReceipt
    claimed_at: datetime
    source_sha256: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.receipt_id, "checkpoint chain source identity")
        if not isinstance(self.receipt, ReviewExecutionCheckpointReceipt):
            raise ValueError("checkpoint chain source requires a typed receipt")
        _aware(self.claimed_at, "checkpoint chain source claim")
        if (not isinstance(self.source_sha256, str) or len(self.source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.source_sha256)):
            raise ValueError("checkpoint chain source requires its retained row digest")


@dataclass(frozen=True)
class ObservedReviewCheckpointChainPage:
    sources: tuple[ObservedReviewCheckpointChainSource, ...]
    next_cursor: ReviewCheckpointChainCursor | None

    def __post_init__(self) -> None:
        if (not isinstance(self.sources, tuple)
                or not 1 <= len(self.sources) <= MAX_REVIEW_CHECKPOINT_CHAIN_PAGE
                or any(not isinstance(item, ObservedReviewCheckpointChainSource)
                       for item in self.sources)
                or self.next_cursor is not None
                and not isinstance(self.next_cursor, ReviewCheckpointChainCursor)):
            raise ValueError("checkpoint chain page requires bounded typed sources")
