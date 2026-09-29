"""Resumable expected-command membership from recorded review registrations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION
from .review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from .review_lifecycle_archive_audit import (
    CompletedReviewLifecycleArchiveAudit, ReviewLifecycleMembership,
)
from .review_lifecycle_registration import (
    LinkedReviewLifecycleRegistration, ObservedReviewLifecycleRegistrationPage,
)
from .workflow_identity import canonical_workflow_repository


MAX_REGISTRATION_AUDIT_DUE_SECONDS = 86400


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registration audit requires an aware time")


def _rowid(value: int | None) -> None:
    if value is not None and (type(value) is not int or not MIN_SQL_ROWID <= value <= MAX_SQL_ROWID):
        raise ValueError("registration audit requires a bounded rowid")


def _versions(repository: str, round_rows: int, all_round_rows: int,
              receipts: int, lifecycle: int) -> None:
    if (canonical_workflow_repository(repository) != repository
            or type(round_rows) is not int or not 0 <= round_rows <= MAX_ARCHIVE_SOURCE_VERSION
            or type(all_round_rows) is not int
            or not 0 <= all_round_rows <= MAX_ARCHIVE_SOURCE_VERSION
            or type(receipts) is not int or not 0 <= receipts <= MAX_ARCHIVE_SOURCE_VERSION
            or type(lifecycle) is not int or not 0 <= lifecycle <= MAX_ARCHIVE_SOURCE_VERSION):
        raise ValueError("registration audit requires a canonical scope and source revisions")


@dataclass(frozen=True)
class ScanningReviewRegistrationMembership:
    repository: str
    round_rows_version: int
    all_round_rows_version: int
    round_receipts_version: int
    lifecycle_source_version: int
    after_rowid: int | None
    upper_rowid: int | None
    expected_commands: ReviewLifecycleMembership
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        _versions(self.repository, self.round_rows_version, self.all_round_rows_version,
                  self.round_receipts_version, self.lifecycle_source_version)
        _rowid(self.after_rowid)
        _rowid(self.upper_rowid)
        if (self.after_rowid is not None
                and (self.upper_rowid is None or self.after_rowid > self.upper_rowid)
                or not isinstance(self.expected_commands, ReviewLifecycleMembership)):
            raise ValueError("registration scan requires bounded progress and membership")
        _aware(self.started_at)
        _aware(self.due_at)
        if self.due_at <= self.started_at:
            raise ValueError("registration scan requires a future due time")


@dataclass(frozen=True)
class CompletedReviewRegistrationMembership:
    repository: str
    round_rows_version: int
    all_round_rows_version: int
    round_receipts_version: int
    lifecycle_source_version: int
    upper_rowid: int | None
    expected_commands: ReviewLifecycleMembership
    completed_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        _versions(self.repository, self.round_rows_version, self.all_round_rows_version,
                  self.round_receipts_version, self.lifecycle_source_version)
        _rowid(self.upper_rowid)
        if not isinstance(self.expected_commands, ReviewLifecycleMembership):
            raise ValueError("completed registration audit requires membership")
        _aware(self.completed_at)
        _aware(self.next_due_at)
        if self.next_due_at <= self.completed_at:
            raise ValueError("completed registration audit requires a future pass")


@dataclass(frozen=True)
class BlockedReviewRegistrationMembership:
    prior: ScanningReviewRegistrationMembership | CompletedReviewRegistrationMembership
    reason: str
    source_rowid: int | None
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, (
            ScanningReviewRegistrationMembership, CompletedReviewRegistrationMembership,
        )) or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked registration audit requires typed prior state and reason")
        _rowid(self.source_rowid)
        _aware(self.detected_at)


ReviewRegistrationMembershipState = (
    ScanningReviewRegistrationMembership | CompletedReviewRegistrationMembership
    | BlockedReviewRegistrationMembership
)


@dataclass(frozen=True)
class MatchedReviewRegistrationMembership:
    registration: CompletedReviewRegistrationMembership
    lifecycle: CompletedReviewLifecycleArchiveAudit
    observed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.observed_at)
        if (not isinstance(self.registration, CompletedReviewRegistrationMembership)
                or not isinstance(self.lifecycle, CompletedReviewLifecycleArchiveAudit)
                or self.registration.repository != self.lifecycle.repository
                or self.registration.lifecycle_source_version != self.lifecycle.source_version
                or self.registration.expected_commands != self.lifecycle.commands
                or self.observed_at >= self.registration.next_due_at
                or self.observed_at >= self.lifecycle.next_due_at
                or self.observed_at < self.registration.completed_at
                or self.observed_at < self.lifecycle.completed_at):
            raise ValueError("registration and lifecycle membership have no current exact match")


def begin_review_registration_membership(
    repository: str, round_rows_version: int, all_round_rows_version: int,
    round_receipts_version: int, lifecycle_source_version: int, now: datetime,
    due_seconds: int,
) -> ScanningReviewRegistrationMembership:
    _versions(repository, round_rows_version, all_round_rows_version,
              round_receipts_version, lifecycle_source_version)
    _aware(now)
    if (type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_REGISTRATION_AUDIT_DUE_SECONDS):
        raise ValueError("registration audit requires a bounded due interval")
    try:
        due_at = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("registration audit due time exceeds representable time") from exc
    return ScanningReviewRegistrationMembership(
        repository, round_rows_version, all_round_rows_version,
        round_receipts_version, lifecycle_source_version,
        None, None, ReviewLifecycleMembership.empty(), now, due_at,
    )


def advance_review_registration_membership(
    scan: ScanningReviewRegistrationMembership,
    page: ObservedReviewLifecycleRegistrationPage,
    links: tuple[LinkedReviewLifecycleRegistration, ...],
    now: datetime, due_seconds: int,
) -> ScanningReviewRegistrationMembership | CompletedReviewRegistrationMembership:
    if (not isinstance(scan, ScanningReviewRegistrationMembership)
            or not isinstance(page, ObservedReviewLifecycleRegistrationPage)
            or not isinstance(links, tuple)
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_REGISTRATION_AUDIT_DUE_SECONDS):
        raise ValueError("registration audit requires typed bounded page and links")
    _aware(now)
    if (now < scan.started_at or page.repository != scan.repository
            or page.round_receipts_version != scan.round_receipts_version
            or page.upper_rowid != scan.upper_rowid and scan.upper_rowid is not None
            or scan.after_rowid is not None and (
                page.next_rowid is None or page.next_rowid <= scan.after_rowid
            ) or page.has_more and page.next_rowid is None
            or tuple(link.registration_source for link in links) != page.sources):
        raise ValueError("registration page disagrees with its fixed scan")
    membership = scan.expected_commands
    for link in links:
        rowid = link.registration_source.source_rowid
        if (scan.after_rowid is not None and rowid <= scan.after_rowid
                or page.next_rowid is None or rowid > page.next_rowid):
            raise ValueError("registration link is outside its page")
        command = link.command_source
        membership = membership.add("command", command.source_rowid, command.source_digest)
    if page.has_more:
        return replace(
            scan, after_rowid=page.next_rowid, upper_rowid=page.upper_rowid,
            expected_commands=membership,
        )
    try:
        next_due = now + timedelta(seconds=due_seconds)
    except OverflowError as exc:
        raise ValueError("registration audit due time exceeds representable time") from exc
    return CompletedReviewRegistrationMembership(
        scan.repository, scan.round_rows_version, scan.all_round_rows_version,
        scan.round_receipts_version, scan.lifecycle_source_version,
        page.upper_rowid, membership, now, next_due,
    )
