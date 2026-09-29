"""Durable expected-command census for recorded review registrations."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime

from ..domain.review_lifecycle_archive import MAX_REVIEW_LIFECYCLE_PAGE_BYTES
from ..domain.review_registration_membership_audit import (
    MAX_REGISTRATION_AUDIT_DUE_SECONDS, BlockedReviewRegistrationMembership,
    CompletedReviewRegistrationMembership, MatchedReviewRegistrationMembership,
    ReviewRegistrationMembershipState, ScanningReviewRegistrationMembership,
    advance_review_registration_membership, begin_review_registration_membership,
)
from ..domain.review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from ..domain.review_lifecycle_registration import BlockedReviewLifecycleRegistrationPage
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import (
    read_review_archive_source_versions, read_review_lifecycle_source_version,
)
from .review_lifecycle_archive import _size_expression
from .review_lifecycle_archive_audit import read_review_lifecycle_archive_state
from .review_lifecycle_registration import (
    RegistrationLinkByteLimit, read_review_lifecycle_registration_link,
    read_review_lifecycle_registration_page,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError


_NAMESPACE = "review-registration-membership-scan-v1"
_KEY = "expected-review-commands"


def _read_state(
    db: sqlite3.Connection, repository: str,
) -> tuple[int | None, ReviewRegistrationMembershipState | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        f"{_size_expression('payload_json')} AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_NAMESPACE, _NAMESPACE, repository, repository, _KEY, _KEY),
    ).fetchall()
    if not rows:
        return None, None
    row = rows[0]
    if (len(rows) != 1 or any(row[f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(row["payload_bytes"]) is not int
            or row["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("registration membership cursor is aliased or unreadable")
    retained = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? "
        "AND scope=? AND key=? "
        f"AND {_size_expression('payload_json')}<=?",
        (row["rowid"], _NAMESPACE, repository, _KEY, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if retained is None:
        raise WorkflowAuthorityError("registration membership cursor changed during read")
    try:
        raw = retained["payload_json"]
        decoded = decode_workflow(raw, object)
        if (not isinstance(decoded, (
            ScanningReviewRegistrationMembership, CompletedReviewRegistrationMembership,
            BlockedReviewRegistrationMembership,
        )) or encode_workflow(decoded) != raw):
            raise ValueError("registration membership cursor is not canonical")
        owner = decoded.prior.repository if isinstance(
            decoded, BlockedReviewRegistrationMembership,
        ) else decoded.repository
        if owner != repository:
            raise ValueError("registration membership cursor belongs to another repository")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("registration membership cursor is malformed") from exc
    return row["rowid"], decoded


def _persist_state(
    db: sqlite3.Connection, repository: str, rowid: int | None,
    state: ReviewRegistrationMembershipState, now: datetime,
) -> None:
    encoded = encode_workflow(state)
    if rowid is None:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            (_NAMESPACE, repository, _KEY, encoded, now.isoformat()),
        )
    elif db.execute(
        "UPDATE level_state SET payload_json=?,updated_at=? WHERE rowid=? "
        "AND namespace=? AND scope=? AND key=?",
        (encoded, now.isoformat(), rowid, _NAMESPACE, repository, _KEY),
    ).rowcount != 1:
        raise WorkflowAuthorityError("registration membership cursor changed during update")


def _request(db: sqlite3.Connection, repository: str, now: datetime | None = None) -> None:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or now is not None and (
                not isinstance(now, datetime) or now.tzinfo is None
                or now.utcoffset() is None
            )):
        raise ValueError("registration membership requires a transaction and canonical repository")


def read_review_registration_membership_state(
    db: sqlite3.Connection, repository: str,
) -> ReviewRegistrationMembershipState | None:
    """Read one persisted observation; the caller must recheck its revisions."""
    _request(db, repository)
    return _read_state(db, repository)[1]


def advance_review_registration_membership_audit(
    db: sqlite3.Connection, repository: str, *, now: datetime,
    due_seconds: int = 3600,
    max_link_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> ReviewRegistrationMembershipState:
    """Persist one bounded registration page after checking its command and round links."""
    _request(db, repository, now)
    if (type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_REGISTRATION_AUDIT_DUE_SECONDS
            or type(max_link_source_bytes) is not int
            or not 1 <= max_link_source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
        raise ValueError("registration membership requires bounded time and source bytes")
    archive_versions = read_review_archive_source_versions(db, repository)
    round_rows_version = archive_versions.round_rows
    all_round_rows_version = archive_versions.all_round_rows
    receipt_version = archive_versions.round_receipts
    lifecycle_version = read_review_lifecycle_source_version(db)
    rowid, state = _read_state(db, repository)
    if isinstance(state, BlockedReviewRegistrationMembership):
        if now < state.detected_at:
            raise ValueError("registration membership time moved backwards")
        if (state.prior.round_rows_version == round_rows_version
                and state.prior.all_round_rows_version == all_round_rows_version
                and state.prior.round_receipts_version == receipt_version
                and state.prior.lifecycle_source_version == lifecycle_version):
            return state
        state = state.prior
    if isinstance(state, ScanningReviewRegistrationMembership):
        if now < state.started_at:
            raise ValueError("registration membership time moved backwards")
    elif isinstance(state, CompletedReviewRegistrationMembership) and now < state.completed_at:
        raise ValueError("registration membership time moved backwards")
    if (state is None or state.round_rows_version != round_rows_version
            or state.all_round_rows_version != all_round_rows_version
            or state.round_receipts_version != receipt_version
            or state.lifecycle_source_version != lifecycle_version
            or isinstance(state, CompletedReviewRegistrationMembership)
            and now >= state.next_due_at):
        state = begin_review_registration_membership(
            repository, round_rows_version, all_round_rows_version,
            receipt_version, lifecycle_version, now, due_seconds,
        )
    elif isinstance(state, CompletedReviewRegistrationMembership):
        return state
    page = read_review_lifecycle_registration_page(
        db, repository, after_rowid=state.after_rowid,
        upper_rowid=state.upper_rowid,
    )
    if isinstance(page, BlockedReviewLifecycleRegistrationPage):
        result: ReviewRegistrationMembershipState = BlockedReviewRegistrationMembership(
            state, page.reason, page.source_rowid, now,
        )
    else:
        links = []
        blocked = None
        remaining = max_link_source_bytes
        for source in page.sources:
            if not remaining and links:
                break
            try:
                link = read_review_lifecycle_registration_link(
                    db, source, max_source_bytes=remaining,
                )
            except RegistrationLinkByteLimit:
                if links:
                    break
                if max_link_source_bytes < MAX_REVIEW_LIFECYCLE_PAGE_BYTES:
                    raise ValueError(
                        "registration link exceeds the caller's audit step byte budget"
                    )
                blocked = BlockedReviewRegistrationMembership(
                    state, "registration link exceeds the audit step byte bound",
                    source.source_rowid, now,
                )
                break
            except WorkflowAuthorityError as exc:
                blocked = BlockedReviewRegistrationMembership(
                    state, str(exc)[:512], source.source_rowid, now,
                )
                break
            links.append(link)
            remaining -= link.source_bytes
        if blocked is None and len(links) < len(page.sources):
            last = links[-1].registration_source.source_rowid
            if state.after_rowid is None:
                scanned_rows = db.execute(
                    "SELECT count(*) FROM dedupe_keys WHERE rowid<=?", (last,),
                ).fetchone()[0]
            else:
                scanned_rows = db.execute(
                    "SELECT count(*) FROM dedupe_keys WHERE rowid>? AND rowid<=?",
                    (state.after_rowid, last),
                ).fetchone()[0]
            page = replace(
                page, sources=tuple(link.registration_source for link in links),
                next_rowid=last, has_more=True, scanned_rows=scanned_rows,
                source_bytes=sum(link.registration_source.source_bytes for link in links),
            )
        result = blocked if blocked is not None else advance_review_registration_membership(
            state, page, tuple(links), now, due_seconds,
        )
    _persist_state(db, repository, rowid, result, now)
    return result


def observe_matched_review_registration_membership(
    db: sqlite3.Connection, repository: str, *, now: datetime,
) -> MatchedReviewRegistrationMembership:
    """Compare two current complete global censuses; this is not import authority."""
    _request(db, repository, now)
    state = read_review_registration_membership_state(db, repository)
    lifecycle = read_review_lifecycle_archive_state(db, repository)
    versions = read_review_archive_source_versions(db, repository)
    if (not isinstance(state, CompletedReviewRegistrationMembership)
            or not isinstance(lifecycle, CompletedReviewLifecycleArchiveAudit)
            or state.round_rows_version != versions.round_rows
            or state.all_round_rows_version != versions.all_round_rows
            or state.round_receipts_version != versions.round_receipts
            or state.lifecycle_source_version != read_review_lifecycle_source_version(db)):
        raise WorkflowAuthorityError("review registration membership lacks current completed censuses")
    if now >= state.next_due_at or now >= lifecycle.next_due_at:
        raise WorkflowAuthorityError("review registration or lifecycle census is overdue")
    try:
        return MatchedReviewRegistrationMembership(state, lifecycle, now)
    except ValueError as exc:
        raise WorkflowAuthorityError("review registrations disagree with lifecycle commands") from exc
