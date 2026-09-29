"""Bounded observations of lifecycle rows for an eventual review lineage census."""

from dataclasses import dataclass

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION
from .review_execution_activation import (
    MAX_SQL_ROWID, MIN_SQL_ROWID, LegacyReviewSubmissionSource,
)
from .workflow_identity import canonical_workflow_repository


MAX_REVIEW_LIFECYCLE_SOURCE_PAGE = 64
MAX_REVIEW_LIFECYCLE_PAGE_BYTES = 2**23


@dataclass(frozen=True)
class ObservedReviewLifecycleSourcePage:
    """One global command-rowid page; sources are the selected review submissions."""

    repository: str
    sources: tuple[LegacyReviewSubmissionSource, ...]
    next_rowid: int | None
    upper_rowid: int | None
    has_more: bool
    scanned_rows: int
    source_bytes: int
    lifecycle_source_version: int

    def __post_init__(self) -> None:
        rowids = tuple(source.source_rowid for source in self.sources)
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.sources, tuple)
                or any(not isinstance(source, LegacyReviewSubmissionSource)
                       or dict(source.command_fields)["repository"] != self.repository
                       for source in self.sources)
                or rowids != tuple(sorted(set(rowids)))
                or type(self.has_more) is not bool
                or type(self.scanned_rows) is not int
                or not 0 <= self.scanned_rows <= MAX_REVIEW_LIFECYCLE_SOURCE_PAGE
                or len(self.sources) > self.scanned_rows
                or type(self.source_bytes) is not int
                or not 0 <= self.source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES
                or type(self.lifecycle_source_version) is not int
                or not 0 <= self.lifecycle_source_version <= MAX_ARCHIVE_SOURCE_VERSION
                or (self.upper_rowid is None
                    and (self.next_rowid is not None or self.scanned_rows or self.has_more))
                or (self.upper_rowid is not None
                    and (type(self.upper_rowid) is not int
                         or not MIN_SQL_ROWID <= self.upper_rowid <= MAX_SQL_ROWID))
                or (self.next_rowid is not None
                    and (type(self.next_rowid) is not int
                         or not MIN_SQL_ROWID <= self.next_rowid <= MAX_SQL_ROWID
                         or self.upper_rowid is None or self.next_rowid > self.upper_rowid))
                or (self.scanned_rows and self.next_rowid is None)
                or (rowids and (self.next_rowid is None or rowids[-1] > self.next_rowid))
                or (self.has_more and self.next_rowid is None)):
            raise ValueError("lifecycle source page requires bounded rowid progress")


@dataclass(frozen=True)
class BlockedReviewLifecycleSourcePage:
    reason: str
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.reason, str) or not self.reason or len(self.reason) > 512
                or (self.source_rowid is not None
                    and (type(self.source_rowid) is not int
                         or not MIN_SQL_ROWID <= self.source_rowid <= MAX_SQL_ROWID))):
            raise ValueError("blocked lifecycle source page requires a bounded reason")
