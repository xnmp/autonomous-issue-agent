"""Pure, resumable membership accounting for retained review round rows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .review_execution import ReviewExecutionBinding, ReviewExecutionOrigin
from .review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from .workflow_identity import validate_workflow_identifier


MAX_ROUND_ARCHIVE_STEP = 16
MAX_ROUND_ARCHIVE_DUE_SECONDS = 86400
EMPTY_ROUND_ARCHIVE_SET = "0" * 64


def _digest(value: str, name: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} requires a SHA-256 digest")


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{name} requires an aware time")


def _rowid(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or not MIN_SQL_ROWID <= value <= MAX_SQL_ROWID):
        raise ValueError(f"{name} requires a bounded rowid")


@dataclass(frozen=True)
class ReviewRoundArchiveFingerprint:
    source_rowid: int
    sequence: int
    round_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        if type(self.source_rowid) is not int:
            raise ValueError("round archive source requires an integer rowid")
        _rowid(self.source_rowid, "round archive source")
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SQL_ROWID:
            raise ValueError("round archive source requires a positive sequence")
        validate_workflow_identifier(self.round_id, "round archive source identity")
        _digest(self.source_sha256, "round archive source")


@dataclass(frozen=True)
class ReviewRoundArchiveBaseline:
    through_sequence: int
    membership_xor: str

    def __post_init__(self) -> None:
        if type(self.through_sequence) is not int or not 0 <= self.through_sequence <= MAX_SQL_ROWID:
            raise ValueError("round archive baseline requires a bounded frontier")
        _digest(self.membership_xor, "round archive baseline")
        if self.through_sequence == 0 and self.membership_xor != EMPTY_ROUND_ARCHIVE_SET:
            raise ValueError("empty round archive baseline cannot contain a source")


@dataclass(frozen=True)
class ReviewRoundArchiveScan:
    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_sequence: int
    after_rowid: int | None
    upper_rowid: int | None
    source_count: int
    membership_xor: str
    baseline: ReviewRoundArchiveBaseline | None
    prior_count: int
    prior_membership_xor: str
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.origin, ReviewExecutionBinding) or not isinstance(
            self.origin_receipt, ReviewExecutionOrigin,
        ):
            raise ValueError("round archive scan requires its execution origin")
        _digest(self.origin_source_sha256, "round archive origin")
        if type(self.through_sequence) is not int or not 0 <= self.through_sequence <= MAX_SQL_ROWID:
            raise ValueError("round archive scan requires a bounded sequence frontier")
        _rowid(self.after_rowid, "round archive scan cursor")
        _rowid(self.upper_rowid, "round archive scan upper cursor")
        if (self.after_rowid is not None
                and (self.upper_rowid is None or self.after_rowid > self.upper_rowid)):
            raise ValueError("round archive scan cursor exceeds its fixed upper rowid")
        if (type(self.source_count) is not int
                or not 0 <= self.source_count <= self.through_sequence):
            raise ValueError("round archive scan source count exceeds its frontier")
        _digest(self.membership_xor, "round archive scan membership")
        if self.source_count == 0 and self.membership_xor != EMPTY_ROUND_ARCHIVE_SET:
            raise ValueError("empty round archive scan cannot contain a source")
        if (self.baseline is not None and (not isinstance(self.baseline, ReviewRoundArchiveBaseline)
                or self.baseline.through_sequence > self.through_sequence)):
            raise ValueError("round archive baseline exceeds the current frontier")
        if (type(self.prior_count) is not int or self.prior_count < 0
                or self.prior_count > (self.baseline.through_sequence if self.baseline else 0)):
            raise ValueError("round archive prior count exceeds its baseline")
        _digest(self.prior_membership_xor, "round archive prior membership")
        if self.prior_count == 0 and self.prior_membership_xor != EMPTY_ROUND_ARCHIVE_SET:
            raise ValueError("empty round archive prior prefix cannot contain a source")
        _aware(self.started_at, "round archive scan start")
        _aware(self.due_at, "round archive scan due")


@dataclass(frozen=True)
class CompletedReviewRoundArchiveScan:
    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_sequence: int
    upper_rowid: int | None
    membership_xor: str
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.origin, ReviewExecutionBinding) or not isinstance(
            self.origin_receipt, ReviewExecutionOrigin,
        ):
            raise ValueError("completed round archive scan requires its origin")
        _digest(self.origin_source_sha256, "completed round archive origin")
        if type(self.through_sequence) is not int or not 0 <= self.through_sequence <= MAX_SQL_ROWID:
            raise ValueError("completed round archive scan requires a bounded frontier")
        _rowid(self.upper_rowid, "completed round archive upper cursor")
        _digest(self.membership_xor, "completed round archive membership")
        if (self.through_sequence == 0 and self.membership_xor != EMPTY_ROUND_ARCHIVE_SET
                or self.through_sequence > 0 and self.upper_rowid is None):
            raise ValueError("completed round archive scan lost its source frontier")
        _aware(self.completed_at, "completed round archive time")
        _aware(self.next_due_at, "completed round archive due")
        if self.completed_at >= self.next_due_at:
            raise ValueError("completed round archive scan requires a future pass")


@dataclass(frozen=True)
class BlockedReviewRoundArchiveScan:
    prior: ReviewRoundArchiveScan | CompletedReviewRoundArchiveScan
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (ReviewRoundArchiveScan, CompletedReviewRoundArchiveScan))
                or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked round archive scan requires its prior state and reason")
        _rowid(self.source_rowid, "blocked round archive source")
        _aware(self.detected_at, "blocked round archive detection")


RoundArchiveState = (
    ReviewRoundArchiveScan | CompletedReviewRoundArchiveScan | BlockedReviewRoundArchiveScan
)


def begin_review_round_archive_scan(
    origin: ReviewExecutionBinding, origin_receipt: ReviewExecutionOrigin,
    origin_source_sha256: str, through_sequence: int,
    now: datetime, due_seconds: int,
    baseline: ReviewRoundArchiveBaseline | None = None,
    deadline: datetime | None = None,
) -> ReviewRoundArchiveScan:
    if (not isinstance(origin, ReviewExecutionBinding)
            or not isinstance(origin_receipt, ReviewExecutionOrigin)
            or type(through_sequence) is not int
            or not 0 <= through_sequence <= MAX_SQL_ROWID
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_ROUND_ARCHIVE_DUE_SECONDS):
        raise ValueError("round archive scan requires bounded origin and frontier")
    _digest(origin_source_sha256, "round archive origin")
    _aware(now, "round archive scan start")
    if baseline is not None and (not isinstance(baseline, ReviewRoundArchiveBaseline)
                                 or baseline.through_sequence > through_sequence):
        raise ValueError("round archive scan baseline exceeds its frontier")
    if deadline is not None:
        _aware(deadline, "round archive scan deadline")
    try:
        due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("round archive scan due time exceeds representable time") from exc
    if deadline is not None and deadline < due:
        due = deadline
    return ReviewRoundArchiveScan(
        origin, origin_receipt, origin_source_sha256, through_sequence,
        None, None, 0, EMPTY_ROUND_ARCHIVE_SET, baseline,
        0, EMPTY_ROUND_ARCHIVE_SET, now, due,
    )


def _member(source: ReviewRoundArchiveFingerprint) -> int:
    encoded = "\0".join((str(source.sequence), source.round_id, source.source_sha256)).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


def advance_review_round_archive_scan(
    scan: ReviewRoundArchiveScan,
    sources: tuple[ReviewRoundArchiveFingerprint, ...],
    after_rowid: int | None, upper_rowid: int | None,
    has_more: bool, now: datetime, due_seconds: int,
) -> RoundArchiveState:
    """Fold one bounded repository page; no result is admission authority."""
    if (not isinstance(scan, ReviewRoundArchiveScan)
            or not isinstance(sources, tuple) or len(sources) > MAX_ROUND_ARCHIVE_STEP
            or type(has_more) is not bool or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_ROUND_ARCHIVE_DUE_SECONDS):
        raise ValueError("round archive advance requires a bounded typed page")
    _aware(now, "round archive advance time")
    if now < scan.started_at:
        raise ValueError("round archive advance cannot precede its start")
    _rowid(after_rowid, "round archive page cursor")
    _rowid(upper_rowid, "round archive page upper cursor")
    if (scan.upper_rowid is not None and upper_rowid != scan.upper_rowid
            or upper_rowid is None and (after_rowid is not None or sources or has_more)
        or after_rowid is not None and (upper_rowid is None or after_rowid > upper_rowid)
        or scan.after_rowid is not None and (after_rowid is None
            or after_rowid < scan.after_rowid
            or after_rowid == scan.after_rowid and (sources or has_more))
            or has_more and after_rowid is None
            or sources and after_rowid is None):
        raise ValueError("round archive page did not advance its fixed repository frontier")
    membership = int(scan.membership_xor, 16)
    prior_membership = int(scan.prior_membership_xor, 16)
    count = scan.source_count
    prior_count = scan.prior_count
    seen: set[int] = set()
    for source in sources:
        if not isinstance(source, ReviewRoundArchiveFingerprint):
            raise ValueError("round archive page has an invalid source")
        if source.sequence in seen or source.sequence > scan.through_sequence:
            return BlockedReviewRoundArchiveScan(
                scan, "round archive census contains an unclaimed episode row",
                source.source_rowid, now,
            )
        if (after_rowid is None or source.source_rowid > after_rowid
                or scan.after_rowid is not None and source.source_rowid <= scan.after_rowid):
            raise ValueError("round archive page has a source outside its cursor range")
        seen.add(source.sequence)
        member = _member(source)
        membership ^= member
        count += 1
        if scan.baseline is not None and source.sequence <= scan.baseline.through_sequence:
            prior_membership ^= member
            prior_count += 1
    if count > scan.through_sequence:
        return BlockedReviewRoundArchiveScan(
            scan, "round archive census contains extra episode rows", None, now,
        )
    if scan.baseline is not None and prior_count > scan.baseline.through_sequence:
        return BlockedReviewRoundArchiveScan(
            scan, "round archive census contains extra prior episode rows", None, now,
        )
    advanced = replace(
        scan, after_rowid=after_rowid, upper_rowid=upper_rowid,
        source_count=count, membership_xor=f"{membership:064x}",
        prior_count=prior_count, prior_membership_xor=f"{prior_membership:064x}",
    )
    if has_more:
        return advanced
    if count != scan.through_sequence:
        return BlockedReviewRoundArchiveScan(
            advanced, "round archive census lacks allocated episode rows", None, now,
        )
    if scan.baseline is not None and (
        prior_count != scan.baseline.through_sequence
        or f"{prior_membership:064x}" != scan.baseline.membership_xor
    ):
        return BlockedReviewRoundArchiveScan(
            advanced, "round archive source changed since its prior pass", None, now,
        )
    try:
        next_due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("round archive due time exceeds representable time") from exc
    return CompletedReviewRoundArchiveScan(
        scan.origin, scan.origin_receipt, scan.origin_source_sha256,
        scan.through_sequence, upper_rowid, advanced.membership_xor,
        now, next_due,
    )


def review_round_archive_scan_overdue(state: RoundArchiveState, now: datetime) -> bool:
    _aware(now, "round archive due check")
    if isinstance(state, BlockedReviewRoundArchiveScan):
        return True
    if isinstance(state, ReviewRoundArchiveScan):
        return now >= state.due_at
    if isinstance(state, CompletedReviewRoundArchiveScan):
        return now >= state.next_due_at
    raise ValueError("round archive due check requires a typed state")
