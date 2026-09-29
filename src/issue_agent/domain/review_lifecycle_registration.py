"""Bounded source pages for review submission registrations."""

from dataclasses import dataclass

from .review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from .review_lifecycle_archive import (
    MAX_REVIEW_LIFECYCLE_PAGE_BYTES, _validate_page_progress,
)
from .review_rounds import ReviewSubmissionRegistration
from .workflow_identity import canonical_workflow_repository


@dataclass(frozen=True)
class ReviewLifecycleRegistrationSource:
    source_rowid: int
    registration: ReviewSubmissionRegistration
    source_sha256: str
    source_bytes: int

    def __post_init__(self) -> None:
        if (type(self.source_rowid) is not int
                or not MIN_SQL_ROWID <= self.source_rowid <= MAX_SQL_ROWID
                or not isinstance(self.registration, ReviewSubmissionRegistration)
                or not isinstance(self.source_sha256, str)
                or len(self.source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.source_sha256)
                or type(self.source_bytes) is not int
                or not 1 <= self.source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
            raise ValueError("registration source requires a bounded identity and digest")


@dataclass(frozen=True)
class ObservedReviewLifecycleRegistrationPage:
    repository: str
    sources: tuple[ReviewLifecycleRegistrationSource, ...]
    next_rowid: int | None
    upper_rowid: int | None
    has_more: bool
    scanned_rows: int
    source_bytes: int
    round_receipts_version: int

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.sources, tuple)
                or any(not isinstance(source, ReviewLifecycleRegistrationSource)
                       or source.registration.round.binding.rejection.fence.key.repository
                       != self.repository for source in self.sources)
                or self.source_bytes != sum(source.source_bytes for source in self.sources)):
            raise ValueError("registration page requires canonical scoped sources")
        _validate_page_progress(
            self.next_rowid, self.upper_rowid, self.has_more, self.scanned_rows,
            self.source_bytes, self.round_receipts_version,
            tuple(source.source_rowid for source in self.sources),
        )


@dataclass(frozen=True)
class BlockedReviewLifecycleRegistrationPage:
    reason: str
    source_rowid: int | None

    def __post_init__(self) -> None:
        if (not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or self.source_rowid is not None
                and (type(self.source_rowid) is not int
                     or not MIN_SQL_ROWID <= self.source_rowid <= MAX_SQL_ROWID)):
            raise ValueError("blocked registration page requires a reason and source")
