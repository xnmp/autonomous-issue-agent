"""Pure, versioned absence proof for an execution's first checkpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION
from .review_execution import (
    ReviewExecutionBinding, ReviewExecutionCheckpointReceipt, same_execution_episode,
)
from .review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID


MAX_INITIAL_CHECKPOINT_PAGE = 64
MAX_INITIAL_CHECKPOINT_DUE_SECONDS = 86400


def _rowid(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or not MIN_SQL_ROWID <= value <= MAX_SQL_ROWID):
        raise ValueError(f"{name} requires a bounded rowid")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} requires an aware time")


def _version(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_ARCHIVE_SOURCE_VERSION:
        raise ValueError("initial checkpoint scan requires a bounded source revision")


@dataclass(frozen=True)
class ObservedInitialCheckpointReceiptPage:
    receipts: tuple[tuple[int, ReviewExecutionCheckpointReceipt], ...]
    next_rowid: int | None
    upper_rowid: int | None
    has_more: bool
    scanned_rows: int
    source_version: int

    def __post_init__(self) -> None:
        _rowid(self.next_rowid, "initial checkpoint page cursor")
        _rowid(self.upper_rowid, "initial checkpoint page frontier")
        _version(self.source_version)
        rowids = tuple(item[0] for item in self.receipts) if (
            isinstance(self.receipts, tuple)
            and all(isinstance(item, tuple) and len(item) == 2 for item in self.receipts)
        ) else ()
        if (not isinstance(self.receipts, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2
                       or not isinstance(item[1], ReviewExecutionCheckpointReceipt)
                       for item in self.receipts)
                or any(type(rowid) is not int or not MIN_SQL_ROWID <= rowid <= MAX_SQL_ROWID
                       for rowid in rowids)
                or rowids != tuple(sorted(set(rowids)))
                or type(self.has_more) is not bool
                or type(self.scanned_rows) is not int
                or not 0 <= self.scanned_rows <= MAX_INITIAL_CHECKPOINT_PAGE
                or len(self.receipts) > self.scanned_rows
                or self.upper_rowid is None and (
                    self.next_rowid is not None or self.scanned_rows or self.has_more
                )
                or self.next_rowid is not None and (
                    self.upper_rowid is None or self.next_rowid > self.upper_rowid
                )
                or self.scanned_rows and self.next_rowid is None
                or rowids and (self.next_rowid is None or rowids[-1] > self.next_rowid)
                or self.has_more and self.next_rowid is None):
            raise ValueError("initial checkpoint page requires bounded ordered progress")


@dataclass(frozen=True)
class BlockedInitialCheckpointReceiptPage:
    reason: str
    source_rowid: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512:
            raise ValueError("blocked checkpoint receipt page requires a reason")
        _rowid(self.source_rowid, "blocked checkpoint receipt source")


@dataclass(frozen=True)
class ScanningInitialCheckpointAbsence:
    binding: ReviewExecutionBinding
    source_version: int
    after_rowid: int | None
    upper_rowid: int | None
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewExecutionBinding):
            raise ValueError("initial checkpoint scan requires an execution binding")
        _version(self.source_version)
        _rowid(self.after_rowid, "initial checkpoint scan cursor")
        _rowid(self.upper_rowid, "initial checkpoint scan frontier")
        if self.after_rowid is not None and (
            self.upper_rowid is None or self.after_rowid > self.upper_rowid
        ):
            raise ValueError("initial checkpoint scan cursor exceeds its frontier")
        _aware(self.started_at, "initial checkpoint scan start")
        _aware(self.due_at, "initial checkpoint scan due")


@dataclass(frozen=True)
class CompletedInitialCheckpointAbsence:
    binding: ReviewExecutionBinding
    source_version: int
    upper_rowid: int | None
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewExecutionBinding):
            raise ValueError("completed initial checkpoint scan requires its binding")
        _version(self.source_version)
        _rowid(self.upper_rowid, "completed initial checkpoint frontier")
        _aware(self.completed_at, "initial checkpoint completion")
        _aware(self.next_due_at, "initial checkpoint next due")
        if self.completed_at >= self.next_due_at:
            raise ValueError("completed initial checkpoint scan requires a future pass")


@dataclass(frozen=True)
class BlockedInitialCheckpointAbsence:
    prior: ScanningInitialCheckpointAbsence | CompletedInitialCheckpointAbsence
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ScanningInitialCheckpointAbsence, CompletedInitialCheckpointAbsence,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked initial checkpoint scan requires typed prior and reason")
        _rowid(self.source_rowid, "blocked initial checkpoint source")
        _aware(self.detected_at, "blocked initial checkpoint detection")


InitialCheckpointAbsenceState = (
    ScanningInitialCheckpointAbsence | CompletedInitialCheckpointAbsence
    | BlockedInitialCheckpointAbsence
)


def begin_initial_checkpoint_absence(
    binding: ReviewExecutionBinding, source_version: int,
    now: datetime, due_seconds: int,
) -> ScanningInitialCheckpointAbsence:
    if not isinstance(binding, ReviewExecutionBinding):
        raise ValueError("initial checkpoint scan requires an execution binding")
    _version(source_version)
    _aware(now, "initial checkpoint scan start")
    if type(due_seconds) is not int or not 1 <= due_seconds <= MAX_INITIAL_CHECKPOINT_DUE_SECONDS:
        raise ValueError("initial checkpoint scan requires a bounded due interval")
    try:
        due_at = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("initial checkpoint due time exceeds representable time") from exc
    return ScanningInitialCheckpointAbsence(binding, source_version, None, None, now, due_at)


def advance_initial_checkpoint_absence(
    scan: ScanningInitialCheckpointAbsence,
    page: ObservedInitialCheckpointReceiptPage,
    now: datetime, due_seconds: int,
) -> InitialCheckpointAbsenceState:
    if (not isinstance(scan, ScanningInitialCheckpointAbsence)
            or not isinstance(page, ObservedInitialCheckpointReceiptPage)):
        raise ValueError("initial checkpoint scan requires a typed page")
    _aware(now, "initial checkpoint scan time")
    if (now < scan.started_at or page.source_version != scan.source_version
            or scan.upper_rowid is not None and page.upper_rowid != scan.upper_rowid
            or scan.after_rowid is not None and (
                page.next_rowid is None or page.next_rowid < scan.after_rowid
                or page.next_rowid == scan.after_rowid and page.has_more
            )):
        raise ValueError("initial checkpoint page disagrees with its scan")
    for rowid, receipt in page.receipts:
        if (scan.after_rowid is not None and rowid <= scan.after_rowid
                or page.next_rowid is None or rowid > page.next_rowid):
            raise ValueError("initial checkpoint receipt is outside its page")
        if same_execution_episode(
            scan.binding, receipt.previous_checkpoint.state.binding,
        ):
            return BlockedInitialCheckpointAbsence(
                scan, "execution already has a checkpoint receipt", rowid, now,
            )
    if page.has_more:
        return replace(scan, after_rowid=page.next_rowid, upper_rowid=page.upper_rowid)
    if type(due_seconds) is not int or not 1 <= due_seconds <= MAX_INITIAL_CHECKPOINT_DUE_SECONDS:
        raise ValueError("initial checkpoint scan requires a bounded due interval")
    try:
        next_due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("initial checkpoint due time exceeds representable time") from exc
    return CompletedInitialCheckpointAbsence(
        scan.binding, scan.source_version, page.upper_rowid, now, next_due,
    )
