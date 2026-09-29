"""Bounded global pages of recorded review submission registrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..domain.review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from ..domain.review_lifecycle_archive import (
    MAX_REVIEW_LIFECYCLE_PAGE_BYTES, MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
)
from ..domain.review_lifecycle_registration import (
    BlockedReviewLifecycleRegistrationPage, ObservedReviewLifecycleRegistrationPage,
    ReviewLifecycleRegistrationSource,
)
from ..domain.review_rounds import ReviewSubmissionRegistration
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import read_review_archive_source_versions
from .review_execution_activation import _DEDUPE_SOURCE_COLUMNS, _read_recording_source
from .review_lifecycle_archive import _size_expression
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import REVIEW_SUBMISSION_NAMESPACE, WorkflowAuthorityError


def read_review_lifecycle_registration_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
    upper_rowid: int | None = None,
    limit: int = MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
    max_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> ObservedReviewLifecycleRegistrationPage | BlockedReviewLifecycleRegistrationPage:
    """Read one fixed global dedupe page; a later cursor must reconcile its sources."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or after_rowid is not None and (
                type(after_rowid) is not int
                or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID
            ) or upper_rowid is not None and (
                type(upper_rowid) is not int
                or not MIN_SQL_ROWID <= upper_rowid <= MAX_SQL_ROWID
            ) or after_rowid is not None and (
                upper_rowid is None or after_rowid > upper_rowid
            ) or type(limit) is not int or not 1 <= limit <= MAX_REVIEW_LIFECYCLE_SOURCE_PAGE
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
        raise ValueError("registration page requires a transaction and bounded cursor")
    version = read_review_archive_source_versions(db, repository).round_receipts
    if upper_rowid is None:
        edge = db.execute("SELECT rowid FROM dedupe_keys ORDER BY rowid DESC LIMIT 1").fetchone()
        if edge is None:
            return ObservedReviewLifecycleRegistrationPage(
                repository, (), None, None, False, 0, 0, version,
            )
        upper_rowid = edge["rowid"]
    position = "" if after_rowid is None else "AND rowid>? "
    parameters: tuple[object, ...] = (
        REVIEW_SUBMISSION_NAMESPACE, len(repository.encode()), repository, upper_rowid,
    )
    if after_rowid is not None:
        parameters = (*parameters, after_rowid)
    rows = db.execute(
        "SELECT rowid AS source_rowid,typeof(namespace) AS namespace_type,"
        "CAST(namespace AS BLOB)=CAST(? AS BLOB) AS selected,"
        "typeof(scope) AS scope_type,"
        f"CASE WHEN {_size_expression('scope')}=? "
        "THEN CAST(scope AS BLOB)=CAST(? AS BLOB) ELSE 0 END AS scoped "
        "FROM dedupe_keys WHERE rowid<=? "
        f"{position}ORDER BY rowid LIMIT ?",
        (*parameters, limit + 1),
    ).fetchall()
    sources: list[ReviewLifecycleRegistrationSource] = []
    source_bytes = 0
    scanned_rows = 0
    next_rowid = after_rowid
    sizes = tuple(_size_expression(column) for column in _DEDUPE_SOURCE_COLUMNS)
    for row in rows[:limit]:
        rowid = row["source_rowid"]
        if row["selected"]:
            if row["namespace_type"] != "text":
                return BlockedReviewLifecycleRegistrationPage(
                    "review registration namespace has a BLOB alias", rowid,
                )
            if row["scoped"]:
                if row["scope_type"] != "text":
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration repository has a BLOB alias", rowid,
                    )
                metadata = db.execute(
                    "SELECT typeof(key) AS key_type,typeof(claimed_at) AS claimed_type,"
                    "typeof(reusable_after) AS reusable_type,typeof(payload_json) AS payload_type,"
                    f"max({','.join(sizes)}) AS largest_value,"
                    f"{'+'.join(sizes)} AS source_bytes "
                    "FROM dedupe_keys WHERE rowid=?",
                    (rowid,),
                ).fetchone()
                if (metadata is None or metadata["key_type"] != "text"
                        or metadata["claimed_type"] != "text"
                        or metadata["reusable_type"] != "null"
                        or metadata["payload_type"] != "text"
                        or type(metadata["largest_value"]) is not int
                        or type(metadata["source_bytes"]) is not int):
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration has unreadable fields", rowid,
                    )
                size = metadata["source_bytes"]
                if size > max_source_bytes - source_bytes:
                    if scanned_rows:
                        break
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration exceeds the page byte bound", rowid,
                    )
                try:
                    identity = db.execute(
                        "SELECT key FROM dedupe_keys WHERE rowid=? AND namespace=? AND scope=?",
                        (rowid, REVIEW_SUBMISSION_NAMESPACE, repository),
                    ).fetchone()
                    if identity is None:
                        raise ValueError("registration identity changed during read")
                    fields, digest = _read_recording_source(
                        db, REVIEW_SUBMISSION_NAMESPACE, repository, identity["key"],
                    )
                    recorded = dict(fields)
                    registration = decode_workflow(
                        recorded["payload_json"], ReviewSubmissionRegistration,
                    )
                    claimed = datetime.fromisoformat(recorded["claimed_at"])
                    if (encode_workflow(registration) != recorded["payload_json"]
                            or registration.submission.command_id != identity["key"]
                            or registration.round.binding.rejection.fence.key.repository != repository
                            or claimed.tzinfo is None or claimed.utcoffset() is None):
                        raise ValueError("review registration disagrees with its recorded source")
                    sources.append(ReviewLifecycleRegistrationSource(
                        rowid, registration, digest, size,
                    ))
                    source_bytes += size
                except (TypeError, ValueError, UnicodeError, RecursionError,
                        sqlite3.Error, WorkflowAuthorityError) as exc:
                    return BlockedReviewLifecycleRegistrationPage(
                        f"review registration is unreadable: {str(exc)[:465]}", rowid,
                    )
        scanned_rows += 1
        next_rowid = rowid
    return ObservedReviewLifecycleRegistrationPage(
        repository, tuple(sources), next_rowid, upper_rowid,
        len(rows) > scanned_rows, scanned_rows, source_bytes, version,
    )
