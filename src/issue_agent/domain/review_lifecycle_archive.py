"""Bounded observations of lifecycle rows for an eventual review lineage census."""

from dataclasses import dataclass
import hashlib
import json

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION
from .review_execution_activation import (
    LEGACY_SUBMISSION_RECEIPT_COLUMNS, MAX_SQL_ROWID, MIN_SQL_ROWID,
    LegacyReviewSubmissionSource, _rowid, _submission_fields,
)
from .workflow_identity import canonical_workflow_repository


MAX_REVIEW_LIFECYCLE_SOURCE_PAGE = 64
MAX_REVIEW_LIFECYCLE_PAGE_BYTES = 2**23


def _validate_page_progress(
    next_rowid: int | None, upper_rowid: int | None, has_more: bool,
    scanned_rows: int, source_bytes: int, lifecycle_source_version: int,
    source_rowids: tuple[int, ...],
) -> None:
    if (source_rowids != tuple(sorted(set(source_rowids)))
            or type(has_more) is not bool
            or type(scanned_rows) is not int
            or not 0 <= scanned_rows <= MAX_REVIEW_LIFECYCLE_SOURCE_PAGE
            or len(source_rowids) > scanned_rows
            or type(source_bytes) is not int
            or not 0 <= source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES
            or type(lifecycle_source_version) is not int
            or not 0 <= lifecycle_source_version <= MAX_ARCHIVE_SOURCE_VERSION
            or (upper_rowid is None
                and (next_rowid is not None or scanned_rows or has_more))
            or (upper_rowid is not None
                and (type(upper_rowid) is not int
                     or not MIN_SQL_ROWID <= upper_rowid <= MAX_SQL_ROWID))
            or (next_rowid is not None
                and (type(next_rowid) is not int
                     or not MIN_SQL_ROWID <= next_rowid <= MAX_SQL_ROWID
                     or upper_rowid is None or next_rowid > upper_rowid))
            or (scanned_rows and next_rowid is None)
            or (source_rowids and (next_rowid is None or source_rowids[-1] > next_rowid))
            or (has_more and next_rowid is None)):
        raise ValueError("lifecycle source page requires bounded rowid progress")


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
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.sources, tuple)
                or not self.sources and self.source_bytes != 0
                or any(not isinstance(source, LegacyReviewSubmissionSource)
                       or dict(source.command_fields)["repository"] != self.repository
                       for source in self.sources)):
            raise ValueError("lifecycle source page requires bounded rowid progress")
        _validate_page_progress(
            self.next_rowid, self.upper_rowid, self.has_more,
            self.scanned_rows, self.source_bytes, self.lifecycle_source_version,
            tuple(source.source_rowid for source in self.sources),
        )


@dataclass(frozen=True)
class ReviewLifecycleEffectSource:
    """One effect row and its matched command rowid, retained without interpretation."""

    source_rowid: int
    command_rowid: int
    repository: str
    receipt_fields: tuple[tuple[str, str | int | None], ...]

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "lifecycle effect source rowid")
        _rowid(self.command_rowid, "lifecycle effect command rowid")
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("lifecycle effect source requires a canonical repository")
        _submission_fields(self.receipt_fields, LEGACY_SUBMISSION_RECEIPT_COLUMNS)
        if not dict(self.receipt_fields)["command_id"]:
            raise ValueError("lifecycle effect source requires a command identity")

    @property
    def command_id(self) -> str:
        value = dict(self.receipt_fields)["command_id"]
        assert isinstance(value, str)
        return value

    @property
    def source_digest(self) -> str:
        encoded = json.dumps(
            (self.source_rowid, self.command_rowid, self.repository, self.receipt_fields),
            separators=(",", ":"), ensure_ascii=True,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ObservedReviewLifecycleEffectPage:
    """One global effect-rowid page; sources belong to selected review commands."""

    repository: str
    sources: tuple[ReviewLifecycleEffectSource, ...]
    next_rowid: int | None
    upper_rowid: int | None
    has_more: bool
    scanned_rows: int
    source_bytes: int
    lifecycle_source_version: int

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.sources, tuple)
                or not self.sources and self.source_bytes != 0
                or any(not isinstance(source, ReviewLifecycleEffectSource)
                       or source.repository != self.repository
                       for source in self.sources)):
            raise ValueError("lifecycle effect page requires typed sources")
        _validate_page_progress(
            self.next_rowid, self.upper_rowid, self.has_more,
            self.scanned_rows, self.source_bytes, self.lifecycle_source_version,
            tuple(source.source_rowid for source in self.sources),
        )


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
