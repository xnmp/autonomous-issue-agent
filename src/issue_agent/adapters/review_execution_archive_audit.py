"""Durable, bounded generation-source scan; no compaction or admission authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

from ..domain.review_execution import (
    ActivatedReviewExecution, BlockedReviewGenerationArchiveScan,
    CompletedReviewGenerationArchiveScan, ReviewExecutionBoundaryCursor,
    ReviewGenerationArchiveFingerprint, ReviewGenerationArchiveScan,
    MAX_ARCHIVE_AUDIT_DUE_SECONDS,
    advance_review_generation_archive_scan, begin_review_generation_archive_scan,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, ObservedReviewGenerationArchivePage,
    _read_recording_source, read_review_generation_archive_page,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError, _read_execution_origin,
    load_review_workflow_row, workflow_scope,
)


_AUDIT_NAMESPACE = "review-generation-archive-scan"
ARCHIVE_AUDIT_PAGE_SIZE = 16
_AuditState = (
    ReviewGenerationArchiveScan
    | CompletedReviewGenerationArchiveScan
    | BlockedReviewGenerationArchiveScan
)


def _audit_key(key: WorkflowKey, epoch: int, episode: str) -> str:
    if type(epoch) is not int or epoch < 1:
        raise WorkflowAuthorityError("generation archive scan has an invalid execution epoch")
    validate_workflow_identifier(episode, "generation archive scan episode")
    identity = json.dumps(
        (key.repository, key.target.number, epoch, episode),
        ensure_ascii=True, separators=(",", ":"),
    )
    return "review-generation-scan:" + hashlib.sha256(identity.encode()).hexdigest()


def _current_frontier(db: sqlite3.Connection, key: WorkflowKey) -> tuple[int, int, str]:
    scope = workflow_scope(key)
    row = db.execute(
        "SELECT w.generation,c.epoch,c.episode_id,typeof(w.generation) AS generation_type,"
        "typeof(c.epoch) AS epoch_type,typeof(c.episode_id) AS episode_type "
        "FROM workflows AS w JOIN workflow_components AS c "
        "ON c.repository=w.repository AND c.target_kind=w.target_kind "
        "AND c.target_number=w.target_number "
        "WHERE w.repository=? AND w.target_kind=? AND w.target_number=? "
        "AND c.family='review_execution'",
        scope,
    ).fetchone()
    if (row is None or row["generation_type"] != "integer" or row["epoch_type"] != "integer"
            or row["episode_type"] != "text" or type(row["generation"]) is not int
            or row["generation"] < 0 or type(row["epoch"]) is not int or row["epoch"] < 1):
        raise WorkflowAuthorityError("generation archive scan lacks an indexed active execution")
    validate_workflow_identifier(row["episode_id"], "generation archive scan episode")
    return row["generation"], row["epoch"], row["episode_id"]


def _read_state(
    db: sqlite3.Connection, repository: str, audit_key: str,
) -> tuple[int | None, _AuditState | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_AUDIT_NAMESPACE, _AUDIT_NAMESPACE, repository, repository, audit_key, audit_key),
    ).fetchall()
    if not rows:
        return None, None
    if (len(rows) != 1 or any(rows[0][f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(rows[0]["payload_bytes"]) is not int
            or rows[0]["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("generation archive scan cursor is aliased or unreadable")
    row = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? AND scope=? "
        "AND key=? AND length(CAST(payload_json AS BLOB))<=?",
        (rows[0]["rowid"], _AUDIT_NAMESPACE, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise WorkflowAuthorityError("generation archive scan cursor changed during its bounded read")
    raw = row["payload_json"]
    try:
        state = decode_workflow(raw, _AuditState)
        if encode_workflow(state) != raw:
            raise ValueError("noncanonical cursor")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("generation archive scan cursor is malformed") from exc
    return rows[0]["rowid"], state


def _persist_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: _AuditState, now: datetime,
) -> None:
    encoded = encode_workflow(state)
    if rowid is None:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
            (_AUDIT_NAMESPACE, repository, audit_key, encoded, now.isoformat()),
        )
    elif db.execute(
        "UPDATE level_state SET payload_json=?,updated_at=? WHERE rowid=? "
        "AND namespace=? AND scope=? AND key=?",
        (encoded, now.isoformat(), rowid, _AUDIT_NAMESPACE, repository, audit_key),
    ).rowcount != 1:
        raise WorkflowAuthorityError("generation archive scan cursor changed during its update")


def advance_review_generation_archive_audit(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime, due_seconds: int = 3600,
) -> _AuditState:
    """Check and persist one expected generation page in the caller's write transaction.

    This cursor covers generation source pairs only. Checkpoint ancestry,
    receipt-namespace sweep, retired rounds and the admission gate remain
    separate obligations before compaction can use its result.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey) or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int or not 1 <= due_seconds <= MAX_ARCHIVE_AUDIT_DUE_SECONDS):
        raise ValueError("generation archive audit requires a transaction, key and aware time")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("generation archive audit requires a PR workflow")
    current_generation, epoch, episode = _current_frontier(db, key)
    audit_key = _audit_key(key, epoch, episode)
    rowid, state = _read_state(db, key.repository, audit_key)
    if state is None:
        snapshot = load_review_workflow_row(db, key)
        if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
            raise WorkflowAuthorityError("generation archive audit requires active execution authority")
        origin_receipt, _ = _read_execution_origin(db, scope, snapshot.execution)
        _, origin_source_sha256 = _read_recording_source(
            db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
            origin_receipt.origin.evidence_id,
        )
        origin = origin_receipt.checkpoint.state.binding
        if (snapshot.execution.origin != origin_receipt.origin
                or snapshot.execution.binding.execution_epoch != epoch
                or snapshot.execution.binding.execution_episode != episode
                or snapshot.revision.fence.generation != current_generation):
            raise WorkflowAuthorityError("generation archive audit origin disagrees with active execution")
        state = begin_review_generation_archive_scan(
            origin, origin_receipt.origin, origin_source_sha256,
            current_generation, now, due_seconds,
        )
    else:
        origin = state.scan.origin if isinstance(state, BlockedReviewGenerationArchiveScan) else state.origin
        cause = state.scan.origin_receipt if isinstance(state, BlockedReviewGenerationArchiveScan) else state.origin_receipt
        if (origin.rejection.fence.key != key
                or origin.execution_epoch != epoch or origin.execution_episode != episode):
            raise WorkflowAuthorityError("generation archive audit cursor changed its episode")
        if isinstance(state, BlockedReviewGenerationArchiveScan):
            return state
        if current_generation < state.through_generation:
            raise WorkflowAuthorityError("generation archive audit frontier moved backwards")
        try:
            _, source_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository, cause.evidence_id,
            )
            origin_discrepancy = source_digest != state.origin_source_sha256
        except (ValueError, TypeError, UnicodeError, sqlite3.Error):
            origin_discrepancy = True
        if origin_discrepancy:
            generation = (state.next_generation if isinstance(state, ReviewGenerationArchiveScan)
                          else state.through_generation)
            result = BlockedReviewGenerationArchiveScan(
                state, generation, "execution origin source changed or is unreadable", now,
            )
            _persist_state(db, key.repository, audit_key, rowid, result, now)
            return result
        if isinstance(state, CompletedReviewGenerationArchiveScan) and (
            current_generation > state.through_generation or now >= state.next_due_at
        ):
            prior_due = state.next_due_at
            state = begin_review_generation_archive_scan(
                origin, cause, state.origin_source_sha256, current_generation, now, due_seconds,
            )
            if isinstance(state, ReviewGenerationArchiveScan):
                state = replace(state, due_at=min(state.due_at, prior_due))
        elif isinstance(state, CompletedReviewGenerationArchiveScan):
            return state
    if isinstance(state, ReviewGenerationArchiveScan):
        page = read_review_generation_archive_page(
            db, state.origin, state.next_generation, state.through_generation,
            preceding=state.preceding, limit=ARCHIVE_AUDIT_PAGE_SIZE,
        )
        if isinstance(page, BlockedReviewGenerationArchivePage):
            result: _AuditState = BlockedReviewGenerationArchiveScan(
                state, page.generation, (page.reason or "archived source is unreadable")[:512], now,
            )
        elif isinstance(page, ObservedReviewGenerationArchivePage) and page.sources:
            fingerprints = tuple(ReviewGenerationArchiveFingerprint(
                ReviewExecutionBoundaryCursor(source.boundary.receipt_id, source.boundary.to_revision),
                source.receipt_digest, source.accepted_namespace, source.accepted_key,
                source.accepted_digest,
            ) for source in page.sources)
            result = advance_review_generation_archive_scan(state, fingerprints, now, due_seconds)
            expected_next = (result.next_generation if isinstance(result, ReviewGenerationArchiveScan)
                             else result.through_generation + 1)
            if page.next_generation != expected_next:
                raise WorkflowAuthorityError("generation archive page cursor changed during audit")
        else:
            result = BlockedReviewGenerationArchiveScan(
                state, state.next_generation, "generation archive page returned no expected sources", now,
            )
    else:
        result = state
    _persist_state(db, key.repository, audit_key, rowid, result, now)
    return result
