"""Pure, resumable accounting for finalized review-round receipt membership."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .review_execution import ReviewExecutionBinding, ReviewExecutionOrigin, ReviewRoundIdentity
from .review_execution_activation import MAX_SQL_ROWID
from .review_receipt_attribution import ReviewRoundReceiptNamespace
from .review_round_archive import MAX_ROUND_ARCHIVE_DUE_SECONDS
from .workflow_identity import canonical_workflow_repository, validate_workflow_identifier


MAX_ROUND_RECEIPT_ARCHIVE_STEP = 1
MAX_ROUND_RECEIPT_ARCHIVE_MEMBERS = 128
EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST = "0" * 64
EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST = hashlib.sha256(b"").hexdigest()
ROUND_RECEIPT_NAMESPACES = tuple(sorted(item.value for item in ReviewRoundReceiptNamespace))


@dataclass(frozen=True, order=True)
class ReviewRoundReceiptSweepCursor:
    namespace: str
    scope: str
    key: str

    def __post_init__(self) -> None:
        if self.namespace not in ROUND_RECEIPT_NAMESPACES:
            raise ValueError("round receipt cursor requires a selected namespace")
        if canonical_workflow_repository(self.scope) != self.scope:
            raise ValueError("round receipt cursor requires a canonical repository")
        validate_workflow_identifier(self.key, "round receipt cursor key")


def _digest(value: str, name: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} requires a SHA-256 digest")


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{name} requires an aware time")


def _due(now: datetime, due_seconds: int) -> datetime:
    _aware(now, "round receipt audit time")
    if (type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_ROUND_ARCHIVE_DUE_SECONDS):
        raise ValueError("round receipt audit requires a bounded due window")
    try:
        return now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("round receipt audit due time is unrepresentable") from exc


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveMember:
    round: ReviewRoundIdentity
    round_source_sha256: str
    namespace: str
    key: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity):
            raise ValueError("round receipt archive member requires its round")
        if self.namespace not in ROUND_RECEIPT_NAMESPACES:
            raise ValueError("round receipt archive member requires a selected namespace")
        validate_workflow_identifier(self.key, "round receipt archive key")
        _digest(self.round_source_sha256, "round receipt archive round source")
        _digest(self.source_sha256, "round receipt archive source")


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveSource:
    round: ReviewRoundIdentity
    round_source_sha256: str
    receipt_set_sha256: str
    members: tuple[ReviewRoundReceiptArchiveMember, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.round, ReviewRoundIdentity)
                or not isinstance(self.members, tuple)
                or not 1 <= len(self.members) <= MAX_ROUND_RECEIPT_ARCHIVE_MEMBERS
                or any(not isinstance(member, ReviewRoundReceiptArchiveMember)
                       or member.round != self.round
                       or member.round_source_sha256 != self.round_source_sha256
                       for member in self.members)):
            raise ValueError("round receipt archive source requires exact members")
        _digest(self.round_source_sha256, "round receipt archive row")
        _digest(self.receipt_set_sha256, "round receipt archive receipt set")
        keys = tuple((item.namespace, item.key) for item in self.members)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("round receipt archive source requires unique ordered members")
        receipt_set = hashlib.sha256(json.dumps(
            [(item.namespace, item.key, item.source_sha256) for item in self.members],
            separators=(",", ":"), ensure_ascii=True,
        ).encode()).hexdigest()
        if receipt_set != self.receipt_set_sha256:
            raise ValueError("round receipt archive source disagrees with its receipt set")


def _member_digest(member: ReviewRoundReceiptArchiveMember) -> int:
    encoded = "\0".join((
        str(member.round.sequence), member.round.round_id,
        member.round_source_sha256, member.namespace, member.key,
        member.source_sha256,
    )).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveBaseline:
    through_sequence: int
    rolling_sha256: str

    def __post_init__(self) -> None:
        if (type(self.through_sequence) is not int
                or not 0 <= self.through_sequence <= MAX_SQL_ROWID):
            raise ValueError("round receipt archive baseline requires a bounded frontier")
        _digest(self.rolling_sha256, "round receipt archive baseline")
        if (self.through_sequence == 0
                and self.rolling_sha256 != EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST):
            raise ValueError("empty round receipt archive baseline cannot contain sources")


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveScan:
    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_sequence: int
    next_sequence: int
    rolling_sha256: str
    receipt_count: int
    membership_xor: str
    baseline: ReviewRoundReceiptArchiveBaseline | None
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionBinding)
                or not isinstance(self.origin_receipt, ReviewExecutionOrigin)
                or type(self.through_sequence) is not int
                or not 1 <= self.through_sequence <= MAX_SQL_ROWID
                or type(self.next_sequence) is not int
                or not 1 <= self.next_sequence <= self.through_sequence):
            raise ValueError("round receipt archive scan requires its finalized frontier")
        _digest(self.origin_source_sha256, "round receipt archive origin")
        _digest(self.rolling_sha256, "round receipt archive rolling source")
        _digest(self.membership_xor, "round receipt archive membership")
        if (type(self.receipt_count) is not int or self.receipt_count < 0
                or self.receipt_count == 0
                and self.membership_xor != EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST):
            raise ValueError("round receipt archive scan has invalid membership")
        if (self.baseline is not None
                and (not isinstance(self.baseline, ReviewRoundReceiptArchiveBaseline)
                     or self.baseline.through_sequence > self.through_sequence)):
            raise ValueError("round receipt archive baseline exceeds its frontier")
        _aware(self.started_at, "round receipt archive start")
        _aware(self.due_at, "round receipt archive due")


@dataclass(frozen=True)
class CompletedReviewRoundReceiptArchiveScan:
    origin: ReviewExecutionBinding
    origin_receipt: ReviewExecutionOrigin
    origin_source_sha256: str
    through_sequence: int
    rolling_sha256: str
    receipt_count: int
    membership_xor: str
    completed_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionBinding)
                or not isinstance(self.origin_receipt, ReviewExecutionOrigin)
                or type(self.through_sequence) is not int
                or not 0 <= self.through_sequence <= MAX_SQL_ROWID
                or type(self.receipt_count) is not int or self.receipt_count < 0):
            raise ValueError("completed round receipt archive scan requires its frontier")
        for name, value in (
            ("origin", self.origin_source_sha256),
            ("rolling source", self.rolling_sha256),
            ("membership", self.membership_xor),
        ):
            _digest(value, f"completed round receipt archive {name}")
        if (self.through_sequence == 0 and (
            self.receipt_count != 0
            or self.rolling_sha256 != EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST
            or self.membership_xor != EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST
        ) or self.through_sequence > 0 and self.receipt_count < 2):
            raise ValueError("completed round receipt archive scan lost its sources")
        _aware(self.completed_at, "round receipt archive completion")
        _aware(self.due_at, "round receipt archive due")


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveSweep:
    expected: CompletedReviewRoundReceiptArchiveScan
    after: ReviewRoundReceiptSweepCursor | None = None
    upper: ReviewRoundReceiptSweepCursor | None = None
    receipt_count: int = 0
    membership_xor: str = EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST
    pending_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.expected, CompletedReviewRoundReceiptArchiveScan):
            raise ValueError("round receipt archive sweep requires its expected pass")
        if (self.after is not None and not isinstance(self.after, ReviewRoundReceiptSweepCursor)
                or self.upper is not None and not isinstance(self.upper, ReviewRoundReceiptSweepCursor)
                or self.after is not None and (self.upper is None or self.after > self.upper)):
            raise ValueError("round receipt archive sweep requires ordered cursors")
        if (type(self.receipt_count) is not int
                or not 0 <= self.receipt_count <= self.expected.receipt_count
                or type(self.pending_count) is not int
                or not 0 <= self.pending_count <= MAX_SQL_ROWID):
            raise ValueError("round receipt archive sweep has invalid counts")
        _digest(self.membership_xor, "round receipt archive sweep membership")
        if (self.receipt_count == 0
                and self.membership_xor != EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST):
            raise ValueError("empty round receipt archive sweep cannot contain members")


@dataclass(frozen=True)
class CompletedReviewRoundReceiptArchiveAudit:
    expected: CompletedReviewRoundReceiptArchiveScan
    upper: ReviewRoundReceiptSweepCursor | None
    receipt_count: int
    membership_xor: str
    pending_count: int
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.expected, CompletedReviewRoundReceiptArchiveScan)
                or self.upper is not None and not isinstance(self.upper, ReviewRoundReceiptSweepCursor)
                or type(self.receipt_count) is not int
                or self.receipt_count != self.expected.receipt_count
                or type(self.pending_count) is not int
                or not 0 <= self.pending_count <= MAX_SQL_ROWID
                or self.expected.receipt_count > 0 and self.upper is None):
            raise ValueError("completed round receipt audit requires its source count")
        _digest(self.membership_xor, "completed round receipt audit membership")
        if self.membership_xor != self.expected.membership_xor:
            raise ValueError("completed round receipt audit requires matching members")
        _aware(self.completed_at, "round receipt audit completion")
        _aware(self.next_due_at, "round receipt audit next due")
        if self.completed_at >= self.next_due_at:
            raise ValueError("completed round receipt audit requires a future pass")


@dataclass(frozen=True)
class BlockedReviewRoundReceiptArchiveScan:
    prior: ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan
    reason: str
    sequence: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ReviewRoundReceiptArchiveScan, CompletedReviewRoundReceiptArchiveScan,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or self.sequence is not None and (type(self.sequence) is not int
                    or not 1 <= self.sequence <= MAX_SQL_ROWID)):
            raise ValueError("blocked round receipt source pass requires a discrepancy")
        _aware(self.detected_at, "round receipt source discrepancy")


@dataclass(frozen=True)
class BlockedReviewRoundReceiptArchiveSweep:
    prior: ReviewRoundReceiptArchiveSweep | CompletedReviewRoundReceiptArchiveAudit
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ReviewRoundReceiptArchiveSweep, CompletedReviewRoundReceiptArchiveAudit,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or self.source_rowid is not None and type(self.source_rowid) is not int):
            raise ValueError("blocked round receipt namespace pass requires a discrepancy")
        _aware(self.detected_at, "round receipt namespace discrepancy")


RoundReceiptArchiveState = (
    ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan
    | ReviewRoundReceiptArchiveSweep | CompletedReviewRoundReceiptArchiveAudit
    | BlockedReviewRoundReceiptArchiveScan | BlockedReviewRoundReceiptArchiveSweep
)


@dataclass(frozen=True)
class ReviewRoundReceiptArchiveCursor:
    state: RoundReceiptArchiveState
    round_rows_version: int
    all_round_rows_version: int
    round_receipts_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, (
            ReviewRoundReceiptArchiveScan, CompletedReviewRoundReceiptArchiveScan,
            ReviewRoundReceiptArchiveSweep, CompletedReviewRoundReceiptArchiveAudit,
            BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
        )) or any(
            type(version) is not int or not 0 <= version < MAX_SQL_ROWID
            for version in (self.round_rows_version, self.all_round_rows_version,
                            self.round_receipts_version)
        ):
            raise ValueError("round receipt cursor requires bounded source revisions")


def begin_review_round_receipt_archive_scan(
    origin: ReviewExecutionBinding, origin_receipt: ReviewExecutionOrigin,
    origin_source_sha256: str, through_sequence: int,
    now: datetime, due_seconds: int,
    baseline: ReviewRoundReceiptArchiveBaseline | None = None,
    deadline: datetime | None = None,
) -> ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan:
    if (not isinstance(origin, ReviewExecutionBinding)
            or not isinstance(origin_receipt, ReviewExecutionOrigin)
            or type(through_sequence) is not int
            or not 0 <= through_sequence <= MAX_SQL_ROWID
            or baseline is not None and (not isinstance(baseline, ReviewRoundReceiptArchiveBaseline)
                or baseline.through_sequence > through_sequence)):
        raise ValueError("round receipt archive scan requires a bounded origin and frontier")
    _digest(origin_source_sha256, "round receipt archive origin")
    due = _due(now, due_seconds)
    if deadline is not None:
        _aware(deadline, "round receipt archive deadline")
        due = min(due, deadline)
    if through_sequence == 0:
        return CompletedReviewRoundReceiptArchiveScan(
            origin, origin_receipt, origin_source_sha256, 0,
            EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST, 0,
            EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST, now, due,
        )
    return ReviewRoundReceiptArchiveScan(
        origin, origin_receipt, origin_source_sha256, through_sequence, 1,
        EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST, 0,
        EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST, baseline, now, due,
    )


def advance_review_round_receipt_archive_scan(
    scan: ReviewRoundReceiptArchiveScan,
    source: ReviewRoundReceiptArchiveSource, now: datetime,
) -> ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan | BlockedReviewRoundReceiptArchiveScan:
    if (not isinstance(scan, ReviewRoundReceiptArchiveScan)
            or not isinstance(source, ReviewRoundReceiptArchiveSource)
            or source.round.sequence != scan.next_sequence
            or source.round.binding != scan.origin):
        raise ValueError("round receipt archive page disagrees with its expected sequence")
    _aware(now, "round receipt archive advance")
    if now < scan.started_at:
        raise ValueError("round receipt archive time moved backwards")
    membership = int(scan.membership_xor, 16)
    digest = scan.rolling_sha256
    for member in source.members:
        membership ^= _member_digest(member)
    encoded = "\0".join((
        str(source.round.sequence), source.round.round_id,
        str(source.round.binding.rejection.fence.generation),
        source.round_source_sha256, source.receipt_set_sha256,
    )).encode()
    digest = hashlib.sha256(bytes.fromhex(digest) + encoded).hexdigest()
    if (scan.baseline is not None
            and source.round.sequence == scan.baseline.through_sequence
            and digest != scan.baseline.rolling_sha256):
        return BlockedReviewRoundReceiptArchiveScan(
            scan, "round receipt source changed since its prior pass",
            source.round.sequence, now,
        )
    count = scan.receipt_count + len(source.members)
    next_sequence = scan.next_sequence + 1
    if next_sequence <= scan.through_sequence:
        return ReviewRoundReceiptArchiveScan(
            scan.origin, scan.origin_receipt, scan.origin_source_sha256,
            scan.through_sequence, next_sequence, digest, count,
            f"{membership:064x}", scan.baseline, scan.started_at, scan.due_at,
        )
    return CompletedReviewRoundReceiptArchiveScan(
        scan.origin, scan.origin_receipt, scan.origin_source_sha256,
        scan.through_sequence, digest, count, f"{membership:064x}", now, scan.due_at,
    )


def advance_review_round_receipt_archive_sweep(
    sweep: ReviewRoundReceiptArchiveSweep,
    members: tuple[ReviewRoundReceiptArchiveMember, ...],
    after: ReviewRoundReceiptSweepCursor | None,
    upper: ReviewRoundReceiptSweepCursor | None,
    has_more: bool, pending_count: int, now: datetime, due_seconds: int,
) -> ReviewRoundReceiptArchiveSweep | CompletedReviewRoundReceiptArchiveAudit | BlockedReviewRoundReceiptArchiveSweep:
    if (not isinstance(sweep, ReviewRoundReceiptArchiveSweep)
            or not isinstance(members, tuple)
            or len(members) > MAX_ROUND_RECEIPT_ARCHIVE_STEP
            or any(not isinstance(item, ReviewRoundReceiptArchiveMember) for item in members)
            or type(has_more) is not bool
            or type(pending_count) is not int or not 0 <= pending_count <= 1
            or len(members) + pending_count > 1
            or after is not None and not isinstance(after, ReviewRoundReceiptSweepCursor)
            or upper is not None and not isinstance(upper, ReviewRoundReceiptSweepCursor)
            or sweep.upper is not None and upper != sweep.upper
            or upper is None and (after is not None or members or pending_count or has_more)
            or after is not None and (upper is None or after > upper)
            or sweep.after is not None and (after is None or after < sweep.after
                or after == sweep.after and (members or pending_count or has_more))
            or has_more and after is None):
        raise ValueError("round receipt archive sweep page did not advance its frontier")
    _aware(now, "round receipt archive sweep advance")
    if now < sweep.expected.completed_at:
        raise ValueError("round receipt archive sweep time moved backwards")
    membership = int(sweep.membership_xor, 16)
    for member in members:
        if (member.round.binding != sweep.expected.origin
                or member.round.sequence > sweep.expected.through_sequence):
            raise ValueError("round receipt archive sweep member is outside its prefix")
        membership ^= _member_digest(member)
    count = sweep.receipt_count + len(members)
    if count > sweep.expected.receipt_count:
        return BlockedReviewRoundReceiptArchiveSweep(
            sweep, "round receipt census has extra finalized sources", None, now,
        )
    advanced = ReviewRoundReceiptArchiveSweep(
        sweep.expected, after, upper, count, f"{membership:064x}",
        sweep.pending_count + pending_count,
    )
    if has_more:
        return advanced
    if (count != sweep.expected.receipt_count
            or advanced.membership_xor != sweep.expected.membership_xor):
        return BlockedReviewRoundReceiptArchiveSweep(
            advanced, "round receipt census disagrees with expected members", None, now,
        )
    next_due = _due(now, due_seconds)
    return CompletedReviewRoundReceiptArchiveAudit(
        sweep.expected, upper, count, advanced.membership_xor,
        advanced.pending_count, now, next_due,
    )


def review_round_receipt_archive_overdue(state: RoundReceiptArchiveState, now: datetime) -> bool:
    _aware(now, "round receipt archive due check")
    if isinstance(state, (BlockedReviewRoundReceiptArchiveScan,
                          BlockedReviewRoundReceiptArchiveSweep)):
        return True
    if isinstance(state, ReviewRoundReceiptArchiveScan):
        return now >= state.due_at
    if isinstance(state, CompletedReviewRoundReceiptArchiveScan):
        return now >= state.due_at
    if isinstance(state, ReviewRoundReceiptArchiveSweep):
        return now >= state.expected.due_at
    if isinstance(state, CompletedReviewRoundReceiptArchiveAudit):
        return now >= state.next_due_at
    raise ValueError("round receipt archive due check requires a typed state")
