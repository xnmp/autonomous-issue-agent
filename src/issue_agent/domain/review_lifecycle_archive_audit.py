"""Pure, resumable membership accounting for review lifecycle sources."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION
from .review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from .review_lifecycle_archive import (
    ObservedReviewLifecycleEffectPage, ObservedReviewLifecycleSourcePage,
)
from .workflow_identity import canonical_workflow_repository


MAX_LIFECYCLE_AUDIT_DUE_SECONDS = 86400
EMPTY_MEMBERSHIP = "0" * 64


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} requires an aware time")


def _rowid(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or not MIN_SQL_ROWID <= value <= MAX_SQL_ROWID):
        raise ValueError(f"{name} requires a bounded rowid")


def _version(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_ARCHIVE_SOURCE_VERSION:
        raise ValueError("lifecycle audit requires a bounded source revision")


def _repository(value: str) -> None:
    if canonical_workflow_repository(value) != value:
        raise ValueError("lifecycle audit requires a canonical repository")


def _member(*parts: object) -> int:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


@dataclass(frozen=True)
class ReviewLifecycleMembership:
    count: int
    xor_sha256: str

    def __post_init__(self) -> None:
        if (type(self.count) is not int or not 0 <= self.count <= MAX_SQL_ROWID
                or not isinstance(self.xor_sha256, str)
                or len(self.xor_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.xor_sha256)
                or (self.count == 0 and self.xor_sha256 != EMPTY_MEMBERSHIP)):
            raise ValueError("lifecycle membership requires a bounded count and digest")

    @classmethod
    def empty(cls) -> ReviewLifecycleMembership:
        return cls(0, EMPTY_MEMBERSHIP)

    def add(self, *parts: object) -> ReviewLifecycleMembership:
        if self.count == MAX_SQL_ROWID:
            raise ValueError("lifecycle membership count exceeds its SQL bound")
        return ReviewLifecycleMembership(
            self.count + 1, f"{int(self.xor_sha256, 16) ^ _member(*parts):064x}",
        )


def _scan_progress(after_rowid: int | None, upper_rowid: int | None) -> None:
    _rowid(after_rowid, "lifecycle scan cursor")
    _rowid(upper_rowid, "lifecycle scan upper rowid")
    if after_rowid is not None and (upper_rowid is None or after_rowid > upper_rowid):
        raise ValueError("lifecycle scan cursor exceeds its fixed upper rowid")


@dataclass(frozen=True)
class ScanningReviewLifecycleCommands:
    repository: str
    source_version: int
    after_rowid: int | None
    upper_rowid: int | None
    commands: ReviewLifecycleMembership
    joined_effects: ReviewLifecycleMembership
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        _repository(self.repository)
        _version(self.source_version)
        _scan_progress(self.after_rowid, self.upper_rowid)
        if (not isinstance(self.commands, ReviewLifecycleMembership)
                or not isinstance(self.joined_effects, ReviewLifecycleMembership)
                or self.joined_effects.count > self.commands.count):
            raise ValueError("command scan has invalid membership")
        _aware(self.started_at, "command scan start")
        _aware(self.due_at, "command scan due")


@dataclass(frozen=True)
class ScanningReviewLifecycleEffects:
    repository: str
    source_version: int
    command_upper_rowid: int | None
    commands: ReviewLifecycleMembership
    joined_effects: ReviewLifecycleMembership
    after_rowid: int | None
    upper_rowid: int | None
    effects: ReviewLifecycleMembership
    effect_links: ReviewLifecycleMembership
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        _repository(self.repository)
        _version(self.source_version)
        _rowid(self.command_upper_rowid, "lifecycle command frontier")
        _scan_progress(self.after_rowid, self.upper_rowid)
        if (not all(isinstance(value, ReviewLifecycleMembership) for value in (
            self.commands, self.joined_effects, self.effects, self.effect_links,
        )) or self.joined_effects.count > self.commands.count
                or self.effect_links.count != self.effects.count):
            raise ValueError("effect scan has invalid membership")
        _aware(self.started_at, "effect scan start")
        _aware(self.due_at, "effect scan due")


@dataclass(frozen=True)
class CompletedReviewLifecycleArchiveAudit:
    repository: str
    source_version: int
    command_upper_rowid: int | None
    effect_upper_rowid: int | None
    commands: ReviewLifecycleMembership
    joined_effects: ReviewLifecycleMembership
    effects: ReviewLifecycleMembership
    effect_links: ReviewLifecycleMembership
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        _repository(self.repository)
        _version(self.source_version)
        _rowid(self.command_upper_rowid, "completed lifecycle command frontier")
        _rowid(self.effect_upper_rowid, "completed lifecycle effect frontier")
        if (not all(isinstance(value, ReviewLifecycleMembership) for value in (
            self.commands, self.joined_effects, self.effects, self.effect_links,
        )) or self.joined_effects != self.effect_links
                or self.effects.count != self.effect_links.count
                or self.effects.count > self.commands.count
                or self.commands.count and self.command_upper_rowid is None
                or self.effects.count and self.effect_upper_rowid is None):
            raise ValueError("completed lifecycle audit has unreconciled membership")
        _aware(self.completed_at, "lifecycle audit completion")
        _aware(self.next_due_at, "lifecycle audit next due")
        if self.completed_at >= self.next_due_at:
            raise ValueError("completed lifecycle audit requires a future pass")


@dataclass(frozen=True)
class BlockedReviewLifecycleArchiveAudit:
    prior: (
        ScanningReviewLifecycleCommands | ScanningReviewLifecycleEffects
        | CompletedReviewLifecycleArchiveAudit
    )
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ScanningReviewLifecycleCommands, ScanningReviewLifecycleEffects,
            CompletedReviewLifecycleArchiveAudit,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked lifecycle audit requires typed prior state and reason")
        _rowid(self.source_rowid, "blocked lifecycle source")
        _aware(self.detected_at, "blocked lifecycle detection")


ReviewLifecycleAuditState = (
    ScanningReviewLifecycleCommands | ScanningReviewLifecycleEffects
    | CompletedReviewLifecycleArchiveAudit | BlockedReviewLifecycleArchiveAudit
)


def begin_review_lifecycle_archive_audit(
    repository: str, source_version: int, now: datetime,
    due_seconds: int, deadline: datetime | None = None,
) -> ScanningReviewLifecycleCommands:
    _repository(repository)
    _version(source_version)
    _aware(now, "lifecycle audit start")
    if type(due_seconds) is not int or not 1 <= due_seconds <= MAX_LIFECYCLE_AUDIT_DUE_SECONDS:
        raise ValueError("lifecycle audit requires a bounded due interval")
    if deadline is not None:
        _aware(deadline, "lifecycle audit deadline")
    try:
        due_at = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("lifecycle audit due time exceeds representable time") from exc
    if deadline is not None and deadline < due_at:
        due_at = deadline
    return ScanningReviewLifecycleCommands(
        repository, source_version, None, None,
        ReviewLifecycleMembership.empty(), ReviewLifecycleMembership.empty(),
        now, due_at,
    )


def _validate_page_progress(
    previous: int | None, fixed_upper: int | None,
    next_rowid: int | None, upper_rowid: int | None, has_more: bool,
) -> None:
    if (fixed_upper is not None and upper_rowid != fixed_upper
            or previous is not None and (next_rowid is None or next_rowid < previous)
            or previous is not None and next_rowid == previous and has_more
            or next_rowid is not None and (upper_rowid is None or next_rowid > upper_rowid)
            or has_more and next_rowid is None):
        raise ValueError("lifecycle audit page did not advance its fixed frontier")


def advance_review_lifecycle_command_scan(
    scan: ScanningReviewLifecycleCommands,
    page: ObservedReviewLifecycleSourcePage, now: datetime,
) -> ScanningReviewLifecycleCommands | ScanningReviewLifecycleEffects:
    if not isinstance(scan, ScanningReviewLifecycleCommands) or not isinstance(
        page, ObservedReviewLifecycleSourcePage,
    ):
        raise ValueError("lifecycle command scan requires a typed page")
    _aware(now, "lifecycle command scan time")
    if now < scan.started_at or page.repository != scan.repository or page.lifecycle_source_version != scan.source_version:
        raise ValueError("lifecycle command page disagrees with its scan")
    _validate_page_progress(
        scan.after_rowid, scan.upper_rowid, page.next_rowid, page.upper_rowid, page.has_more,
    )
    commands, links = scan.commands, scan.joined_effects
    for source in page.sources:
        if (scan.after_rowid is not None and source.source_rowid <= scan.after_rowid
                or page.next_rowid is None or source.source_rowid > page.next_rowid):
            raise ValueError("lifecycle command source is outside its page")
        commands = commands.add("command", source.source_rowid, source.source_digest)
        if source.receipt_fields is not None:
            links = links.add("effect-link", source.source_rowid, source.receipt_fields)
    advanced = replace(
        scan, after_rowid=page.next_rowid, upper_rowid=page.upper_rowid,
        commands=commands, joined_effects=links,
    )
    if page.has_more:
        return advanced
    return ScanningReviewLifecycleEffects(
        scan.repository, scan.source_version, page.upper_rowid,
        commands, links, None, None,
        ReviewLifecycleMembership.empty(), ReviewLifecycleMembership.empty(),
        scan.started_at, scan.due_at,
    )


def advance_review_lifecycle_effect_scan(
    scan: ScanningReviewLifecycleEffects,
    page: ObservedReviewLifecycleEffectPage, now: datetime, due_seconds: int,
) -> ScanningReviewLifecycleEffects | CompletedReviewLifecycleArchiveAudit | BlockedReviewLifecycleArchiveAudit:
    if not isinstance(scan, ScanningReviewLifecycleEffects) or not isinstance(
        page, ObservedReviewLifecycleEffectPage,
    ) or type(due_seconds) is not int or not 1 <= due_seconds <= MAX_LIFECYCLE_AUDIT_DUE_SECONDS:
        raise ValueError("lifecycle effect scan requires a typed page and due interval")
    _aware(now, "lifecycle effect scan time")
    if now < scan.started_at or page.repository != scan.repository or page.lifecycle_source_version != scan.source_version:
        raise ValueError("lifecycle effect page disagrees with its scan")
    _validate_page_progress(
        scan.after_rowid, scan.upper_rowid, page.next_rowid, page.upper_rowid, page.has_more,
    )
    effects, links = scan.effects, scan.effect_links
    for source in page.sources:
        if (scan.after_rowid is not None and source.source_rowid <= scan.after_rowid
                or page.next_rowid is None or source.source_rowid > page.next_rowid):
            raise ValueError("lifecycle effect source is outside its page")
        effects = effects.add("effect", source.source_rowid, source.source_digest)
        links = links.add("effect-link", source.command_rowid, source.receipt_fields)
        if effects.count > scan.joined_effects.count:
            return BlockedReviewLifecycleArchiveAudit(
                scan, "lifecycle effect census exceeds joined command receipts",
                source.source_rowid, now,
            )
    advanced = replace(
        scan, after_rowid=page.next_rowid, upper_rowid=page.upper_rowid,
        effects=effects, effect_links=links,
    )
    if page.has_more:
        return advanced
    if effects.count != scan.joined_effects.count or links != scan.joined_effects:
        return BlockedReviewLifecycleArchiveAudit(
            advanced, "lifecycle effect membership disagrees with command receipts", None, now,
        )
    try:
        next_due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("lifecycle audit due time exceeds representable time") from exc
    return CompletedReviewLifecycleArchiveAudit(
        scan.repository, scan.source_version,
        scan.command_upper_rowid, page.upper_rowid,
        scan.commands, scan.joined_effects, effects, links,
        now, next_due,
    )
