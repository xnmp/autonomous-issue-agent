"""Durable, versioned lifecycle source census without admission authority."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..domain.review_lifecycle_archive import BlockedReviewLifecycleSourcePage
from ..domain.review_lifecycle_archive_audit import (
    MAX_LIFECYCLE_AUDIT_DUE_SECONDS, BlockedReviewLifecycleArchiveAudit,
    CompletedReviewLifecycleArchiveAudit, ReviewLifecycleAuditState,
    ScanningReviewLifecycleCommands, ScanningReviewLifecycleEffects,
    advance_review_lifecycle_command_scan, advance_review_lifecycle_effect_scan,
    begin_review_lifecycle_archive_audit,
)
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import read_review_lifecycle_source_version
from .review_lifecycle_archive import (
    _size_expression, read_review_lifecycle_effect_page,
    read_review_lifecycle_source_page,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError


_AUDIT_NAMESPACE = "review-lifecycle-archive-scan-v1"
_AUDIT_KEY = "review-submissions"


def _read_state(
    db: sqlite3.Connection, repository: str,
) -> tuple[int | None, ReviewLifecycleAuditState | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        f"{_size_expression('payload_json')} AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_AUDIT_NAMESPACE, _AUDIT_NAMESPACE, repository, repository, _AUDIT_KEY, _AUDIT_KEY),
    ).fetchall()
    if not rows:
        return None, None
    row = rows[0]
    if (len(rows) != 1 or any(row[f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(row["payload_bytes"]) is not int
            or row["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("lifecycle audit cursor is aliased or unreadable")
    retained = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? "
        "AND scope=? AND key=? "
        f"AND {_size_expression('payload_json')}<=?",
        (row["rowid"], _AUDIT_NAMESPACE, repository, _AUDIT_KEY, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if retained is None:
        raise WorkflowAuthorityError("lifecycle audit cursor changed during its bounded read")
    try:
        raw = retained["payload_json"]
        decoded = decode_workflow(raw, object)
        if (not isinstance(decoded, (
            ScanningReviewLifecycleCommands, ScanningReviewLifecycleEffects,
            CompletedReviewLifecycleArchiveAudit, BlockedReviewLifecycleArchiveAudit,
        )) or encode_workflow(decoded) != raw):
            raise ValueError("lifecycle audit cursor is not canonical")
        owner = decoded.prior.repository if isinstance(
            decoded, BlockedReviewLifecycleArchiveAudit,
        ) else decoded.repository
        if owner != repository:
            raise ValueError("lifecycle audit cursor belongs to another repository")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("lifecycle audit cursor is malformed") from exc
    return row["rowid"], decoded


def _persist_state(
    db: sqlite3.Connection, repository: str, rowid: int | None,
    state: ReviewLifecycleAuditState, now: datetime,
) -> None:
    encoded = encode_workflow(state)
    if rowid is None:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            (_AUDIT_NAMESPACE, repository, _AUDIT_KEY, encoded, now.isoformat()),
        )
    elif db.execute(
        "UPDATE level_state SET payload_json=?,updated_at=? WHERE rowid=? "
        "AND namespace=? AND scope=? AND key=?",
        (encoded, now.isoformat(), rowid, _AUDIT_NAMESPACE, repository, _AUDIT_KEY),
    ).rowcount != 1:
        raise WorkflowAuthorityError("lifecycle audit cursor changed during its update")


def read_review_lifecycle_archive_state(
    db: sqlite3.Connection, repository: str,
) -> ReviewLifecycleAuditState | None:
    """Read the persisted observation; callers must separately compare source revision."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository):
        raise ValueError("lifecycle audit read requires a transaction and repository")
    return _read_state(db, repository)[1]


def advance_review_lifecycle_archive_audit(
    db: sqlite3.Connection, repository: str, *, now: datetime,
    due_seconds: int = 3600,
) -> ReviewLifecycleAuditState:
    """Persist one bounded command or effect page under a schema source revision."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_LIFECYCLE_AUDIT_DUE_SECONDS):
        raise ValueError("lifecycle audit requires a transaction, repository and aware time")
    version = read_review_lifecycle_source_version(db)
    rowid, state = _read_state(db, repository)
    if isinstance(state, BlockedReviewLifecycleArchiveAudit):
        if now < state.detected_at:
            raise ValueError("lifecycle audit time moved backwards")
        if state.prior.source_version == version:
            return state
        state = state.prior
    if isinstance(state, (ScanningReviewLifecycleCommands, ScanningReviewLifecycleEffects)):
        if now < state.started_at:
            raise ValueError("lifecycle audit time moved backwards")
    elif isinstance(state, CompletedReviewLifecycleArchiveAudit) and now < state.completed_at:
        raise ValueError("lifecycle audit time moved backwards")
    if state is None or state.source_version != version or (
        isinstance(state, CompletedReviewLifecycleArchiveAudit) and now >= state.next_due_at
    ):
        deadline = (
            state.next_due_at if isinstance(state, CompletedReviewLifecycleArchiveAudit)
            else state.due_at if state is not None else None
        )
        state = begin_review_lifecycle_archive_audit(
            repository, version, now, due_seconds, deadline,
        )
    elif isinstance(state, CompletedReviewLifecycleArchiveAudit):
        return state
    if isinstance(state, ScanningReviewLifecycleCommands):
        page = read_review_lifecycle_source_page(
            db, repository, after_rowid=state.after_rowid,
            upper_rowid=state.upper_rowid,
        )
        if isinstance(page, BlockedReviewLifecycleSourcePage):
            result: ReviewLifecycleAuditState = BlockedReviewLifecycleArchiveAudit(
                state, page.reason, page.source_rowid, now,
            )
        else:
            result = advance_review_lifecycle_command_scan(state, page, now)
    elif isinstance(state, ScanningReviewLifecycleEffects):
        page = read_review_lifecycle_effect_page(
            db, repository, after_rowid=state.after_rowid,
            upper_rowid=state.upper_rowid,
        )
        if isinstance(page, BlockedReviewLifecycleSourcePage):
            result = BlockedReviewLifecycleArchiveAudit(
                state, page.reason, page.source_rowid, now,
            )
        else:
            result = advance_review_lifecycle_effect_scan(state, page, now, due_seconds)
    else:
        raise WorkflowAuthorityError("lifecycle audit has an invalid persisted phase")
    _persist_state(db, repository, rowid, result, now)
    return result
