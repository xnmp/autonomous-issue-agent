"""Immutable progress for the global, paged archive identity projection."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ARCHIVE_IDENTITY_TABLES = ("dedupe_keys", "review_execution_rounds", "dispatch_claims")
ArchiveIdentityTable = Literal["dedupe_keys", "review_execution_rounds", "dispatch_claims"]


def _versions(value: tuple[int, int]) -> None:
    if (not isinstance(value, tuple) or len(value) != 2
            or any(type(item) is not int or not 0 <= item < 2**63 for item in value)):
        raise ValueError("archive identity projection requires two source revisions")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("archive identity projection requires aware times")


@dataclass(frozen=True)
class ScanningReviewArchiveIdentities:
    source_versions: tuple[int, int]
    projection_epoch: int
    table: ArchiveIdentityTable
    after_rowid: int | None
    upper_rowid: int | None
    counts: tuple[int, int, int]
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        _versions(self.source_versions)
        if type(self.projection_epoch) is not int or not 0 <= self.projection_epoch < 2**63:
            raise ValueError("archive identity projection epoch is invalid")
        if self.table not in ARCHIVE_IDENTITY_TABLES:
            raise ValueError("archive identity projection requires a selected table")
        for value in (self.after_rowid, self.upper_rowid):
            if value is not None and (type(value) is not int or not -2**63 <= value < 2**63):
                raise ValueError("archive identity projection cursor is invalid")
        if (self.after_rowid is not None and (self.upper_rowid is None
                                             or self.after_rowid > self.upper_rowid)):
            raise ValueError("archive identity projection cursor exceeds its frontier")
        if (not isinstance(self.counts, tuple) or len(self.counts) != 3
                or any(type(count) is not int or not 0 <= count < 2**63 for count in self.counts)):
            raise ValueError("archive identity projection counts are invalid")
        _aware(self.started_at)
        _aware(self.due_at)
        if self.started_at >= self.due_at:
            raise ValueError("archive identity projection requires a future deadline")


@dataclass(frozen=True)
class CompletedReviewArchiveIdentities:
    source_versions: tuple[int, int]
    projection_epoch: int
    counts: tuple[int, int, int]
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        _versions(self.source_versions)
        if type(self.projection_epoch) is not int or not 0 <= self.projection_epoch < 2**63:
            raise ValueError("archive identity projection epoch is invalid")
        if (not isinstance(self.counts, tuple) or len(self.counts) != 3
                or any(type(count) is not int or not 0 <= count < 2**63 for count in self.counts)):
            raise ValueError("archive identity projection counts are invalid")
        _aware(self.completed_at)
        _aware(self.next_due_at)
        if self.completed_at >= self.next_due_at:
            raise ValueError("archive identity projection requires a future deadline")


@dataclass(frozen=True)
class BlockedReviewArchiveIdentities:
    prior: ScanningReviewArchiveIdentities
    reason: str
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, ScanningReviewArchiveIdentities)
                or not isinstance(self.reason, str) or not self.reason.strip()
                or len(self.reason) > 512):
            raise ValueError("archive identity discrepancy requires bounded provenance")
        _aware(self.detected_at)
        if self.detected_at < self.prior.started_at:
            raise ValueError("archive identity discrepancy predates its scan")


ReviewArchiveIdentityState = (
    ScanningReviewArchiveIdentities | CompletedReviewArchiveIdentities | BlockedReviewArchiveIdentities
)


@dataclass(frozen=True)
class ReviewArchiveIdentityCursor:
    state: ReviewArchiveIdentityState
    projection_version: int

    def __post_init__(self) -> None:
        if (not isinstance(self.state, (ScanningReviewArchiveIdentities,
                                      CompletedReviewArchiveIdentities, BlockedReviewArchiveIdentities))
                or type(self.projection_version) is not int
                or not 0 <= self.projection_version < 2**63):
            raise ValueError("archive identity cursor requires a typed state and projection revision")
