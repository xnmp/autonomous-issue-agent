"""Durable global checkpoint-receipt absence scan for an initial execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from ..domain.review_execution import ReviewExecutionBinding
from ..domain.review_execution_activation import MAX_LEGACY_SQL_TEXT_BYTES
from ..domain.review_initial_checkpoint_absence import (
    MAX_INITIAL_CHECKPOINT_DUE_SECONDS, MAX_INITIAL_CHECKPOINT_PAGE,
    BlockedInitialCheckpointAbsence, BlockedInitialCheckpointReceiptPage,
    CompletedInitialCheckpointAbsence, InitialCheckpointAbsenceState,
    ObservedInitialCheckpointReceiptPage, ScanningInitialCheckpointAbsence,
    advance_initial_checkpoint_absence, begin_initial_checkpoint_absence,
)
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import read_review_archive_source_versions
from .review_checkpoint_application_source import (
    REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, read_review_checkpoint_application_source,
)
from .review_lifecycle_archive import _size_expression
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, WorkflowAuthorityError,
    _read_checkpoint_receipt_row,
)


_AUDIT_NAMESPACE = "review-initial-checkpoint-absence-v1"


def _audit_key(binding: ReviewExecutionBinding) -> str:
    key = binding.rejection.fence.key
    encoded = json.dumps(
        (key.repository, key.target.number, binding.execution_epoch,
         binding.execution_episode),
        ensure_ascii=True, separators=(",", ":"),
    )
    return "initial-checkpoint-absence:" + hashlib.sha256(encoded.encode()).hexdigest()


def _read_state(
    db: sqlite3.Connection, binding: ReviewExecutionBinding,
) -> tuple[int | None, InitialCheckpointAbsenceState | None]:
    repository = binding.rejection.fence.key.repository
    audit_key = _audit_key(binding)
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        f"{_size_expression('payload_json')} AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_AUDIT_NAMESPACE, _AUDIT_NAMESPACE, repository, repository, audit_key, audit_key),
    ).fetchall()
    if not rows:
        return None, None
    row = rows[0]
    if (len(rows) != 1 or any(row[f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(row["payload_bytes"]) is not int
            or row["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("initial checkpoint cursor is aliased or unreadable")
    retained = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? "
        "AND scope=? AND key=? "
        f"AND {_size_expression('payload_json')}<=?",
        (row["rowid"], _AUDIT_NAMESPACE, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if retained is None:
        raise WorkflowAuthorityError("initial checkpoint cursor changed during bounded read")
    try:
        raw = retained["payload_json"]
        state = decode_workflow(raw, object)
        if (not isinstance(state, (
            ScanningInitialCheckpointAbsence, CompletedInitialCheckpointAbsence,
            BlockedInitialCheckpointAbsence,
        )) or encode_workflow(state) != raw):
            raise ValueError("initial checkpoint cursor is not canonical")
        owner = state.prior.binding if isinstance(state, BlockedInitialCheckpointAbsence) else state.binding
        if owner != binding:
            raise ValueError("initial checkpoint cursor belongs to another execution")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("initial checkpoint cursor is malformed") from exc
    return row["rowid"], state


def _persist_state(
    db: sqlite3.Connection, binding: ReviewExecutionBinding,
    rowid: int | None, state: InitialCheckpointAbsenceState, now: datetime,
) -> None:
    repository = binding.rejection.fence.key.repository
    audit_key = _audit_key(binding)
    payload = encode_workflow(state)
    if rowid is None:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            (_AUDIT_NAMESPACE, repository, audit_key, payload, now.isoformat()),
        )
    elif db.execute(
        "UPDATE level_state SET payload_json=?,updated_at=? WHERE rowid=? "
        "AND namespace=? AND scope=? AND key=?",
        (payload, now.isoformat(), rowid, _AUDIT_NAMESPACE, repository, audit_key),
    ).rowcount != 1:
        raise WorkflowAuthorityError("initial checkpoint cursor changed during its update")


def _read_page(
    db: sqlite3.Connection, after_rowid: int | None,
    upper_rowid: int | None, source_version: int,
) -> ObservedInitialCheckpointReceiptPage | BlockedInitialCheckpointReceiptPage:
    if upper_rowid is None:
        upper = db.execute("SELECT max(rowid) AS upper_rowid FROM dedupe_keys").fetchone()
        upper_rowid = upper["upper_rowid"]
    if upper_rowid is None:
        return ObservedInitialCheckpointReceiptPage((), None, None, False, 0, source_version)
    query = (
        "SELECT rowid,typeof(namespace) AS namespace_type,"
        "CAST(namespace AS BLOB) IN (CAST(? AS BLOB),CAST(? AS BLOB)) AS selected "
        "FROM dedupe_keys WHERE rowid<=? "
        + ("AND rowid>? " if after_rowid is not None else "")
        + "ORDER BY rowid LIMIT ?"
    )
    parameters: tuple[object, ...] = (
        (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_CHECKPOINT_APPLICATION_NAMESPACE,
         upper_rowid, after_rowid,
         MAX_INITIAL_CHECKPOINT_PAGE + 1)
        if after_rowid is not None else
        (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_CHECKPOINT_APPLICATION_NAMESPACE,
         upper_rowid, MAX_INITIAL_CHECKPOINT_PAGE + 1)
    )
    rows = db.execute(query, parameters).fetchall()
    retained = rows[:MAX_INITIAL_CHECKPOINT_PAGE]
    receipts = []
    for row in retained:
        if not row["selected"]:
            continue
        source_rowid = row["rowid"]
        if row["namespace_type"] != "text":
            return BlockedInitialCheckpointReceiptPage(
                "checkpoint source namespace has a BLOB alias", source_rowid,
            )
        metadata = db.execute(
            "SELECT typeof(scope) AS scope_type,typeof(key) AS key_type,"
            "typeof(claimed_at) AS claimed_type,typeof(reusable_after) AS reusable_type,"
            "typeof(payload_json) AS payload_type,"
            f"{_size_expression('scope')} AS scope_bytes,"
            f"{_size_expression('key')} AS key_bytes,"
            f"{_size_expression('claimed_at')} AS claimed_bytes,"
            f"{_size_expression('payload_json')} AS payload_bytes "
            "FROM dedupe_keys WHERE rowid=? AND namespace IN (?,?)",
            (source_rowid, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE,
             REVIEW_CHECKPOINT_APPLICATION_NAMESPACE),
        ).fetchone()
        if (metadata is None or any(metadata[f"{name}_type"] != "text" for name in (
            "scope", "key", "claimed", "payload",
        )) or metadata["reusable_type"] != "null"
                or any(type(metadata[f"{name}_bytes"]) is not int
                       or metadata[f"{name}_bytes"] > MAX_LEGACY_SQL_TEXT_BYTES
                       for name in ("scope", "key", "claimed"))
                or type(metadata["payload_bytes"]) is not int
                or metadata["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
            return BlockedInitialCheckpointReceiptPage(
                "checkpoint source has unreadable or oversized fields", source_rowid,
            )
        try:
            identity = db.execute(
                "SELECT namespace,scope,key FROM dedupe_keys WHERE rowid=? "
                "AND namespace IN (?,?)",
                (source_rowid, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE,
                 REVIEW_CHECKPOINT_APPLICATION_NAMESPACE),
            ).fetchone()
            if identity is None:
                raise ValueError("checkpoint receipt changed during its identity read")
            namespace, scope, receipt_id = (
                identity["namespace"], identity["scope"], identity["key"],
            )
            if canonical_workflow_repository(scope) != scope:
                raise ValueError("checkpoint source has a noncanonical repository")
            if namespace == REVIEW_EXECUTION_CHECKPOINT_NAMESPACE:
                receipt, _ = _read_checkpoint_receipt_row(db, scope, receipt_id)
            elif namespace == REVIEW_CHECKPOINT_APPLICATION_NAMESPACE:
                receipt = read_review_checkpoint_application_source(
                    db, scope, receipt_id,
                ).checkpoint
            else:
                raise ValueError("checkpoint source namespace changed during its read")
            if receipt.previous_checkpoint.state.binding.rejection.fence.key.repository != scope:
                raise ValueError("checkpoint source is stored under a foreign repository")
        except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
            return BlockedInitialCheckpointReceiptPage(
                f"checkpoint source is unreadable: {str(exc)[:450]}", source_rowid,
            )
        receipts.append((source_rowid, receipt))
    next_rowid = retained[-1]["rowid"] if retained else after_rowid
    return ObservedInitialCheckpointReceiptPage(
        tuple(receipts), next_rowid, upper_rowid,
        len(rows) > MAX_INITIAL_CHECKPOINT_PAGE, len(retained), source_version,
    )


def read_initial_checkpoint_absence_state(
    db: sqlite3.Connection, binding: ReviewExecutionBinding,
) -> InitialCheckpointAbsenceState | None:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(binding, ReviewExecutionBinding)):
        raise ValueError("initial checkpoint absence read requires a transaction and binding")
    return _read_state(db, binding)[1]


def advance_initial_checkpoint_absence_audit(
    db: sqlite3.Connection, binding: ReviewExecutionBinding, *, now: datetime,
    due_seconds: int = 3600,
) -> InitialCheckpointAbsenceState:
    """Persist one bounded global dedupe row page under checkpoint-source revision."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(binding, ReviewExecutionBinding)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_INITIAL_CHECKPOINT_DUE_SECONDS):
        raise ValueError("initial checkpoint absence requires transaction, binding and time")
    repository = binding.rejection.fence.key.repository
    source_version = read_review_archive_source_versions(db, repository).checkpoint_sources
    rowid, state = _read_state(db, binding)
    if isinstance(state, BlockedInitialCheckpointAbsence):
        if now < state.detected_at:
            raise ValueError("initial checkpoint audit time moved backwards")
        if state.prior.source_version == source_version:
            return state
        state = state.prior
    if isinstance(state, ScanningInitialCheckpointAbsence) and now < state.started_at:
        raise ValueError("initial checkpoint audit time moved backwards")
    if isinstance(state, CompletedInitialCheckpointAbsence) and now < state.completed_at:
        raise ValueError("initial checkpoint audit time moved backwards")
    if (state is None or state.source_version != source_version
            or isinstance(state, CompletedInitialCheckpointAbsence)
            and now >= state.next_due_at):
        state = begin_initial_checkpoint_absence(binding, source_version, now, due_seconds)
    elif isinstance(state, CompletedInitialCheckpointAbsence):
        return state
    if not isinstance(state, ScanningInitialCheckpointAbsence):
        raise WorkflowAuthorityError("initial checkpoint audit has an invalid persisted phase")
    try:
        page = _read_page(db, state.after_rowid, state.upper_rowid, source_version)
    except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        result: InitialCheckpointAbsenceState = BlockedInitialCheckpointAbsence(
            state, f"checkpoint source page is unreadable: {str(exc)[:450]}", None, now,
        )
    else:
        if isinstance(page, BlockedInitialCheckpointReceiptPage):
            result = BlockedInitialCheckpointAbsence(state, page.reason, page.source_rowid, now)
        else:
            result = advance_initial_checkpoint_absence(state, page, now, due_seconds)
    _persist_state(db, binding, rowid, result, now)
    return result
