"""Durable checkpoint-chain and retired-segment scan; no admission authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from ..domain.review_checkpoint_archive import (
    BlockedReviewCheckpointArchiveAudit, CompletedReviewCheckpointArchiveAudit,
    MAX_CHECKPOINT_ARCHIVE_DUE_SECONDS, ReviewCheckpointArchiveAwaiting,
    ReviewCheckpointArchiveBaseline, ReviewCheckpointArchiveSegment,
    ReviewCheckpointArchiveState, advance_review_checkpoint_archive_generation,
    advance_review_checkpoint_archive_round, begin_review_checkpoint_archive_scan,
    finish_review_checkpoint_archive_segment, start_review_checkpoint_archive_segment,
)
from ..domain.review_checkpoint_chain import ReviewCheckpointChainCursor
from ..domain.review_execution import (
    ReviewExecutionBoundaryCursor, ReviewGenerationArchiveFingerprint,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_checkpoint_chain import read_review_checkpoint_chain_page
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, ObservedReviewGenerationArchivePage,
    _read_recording_source, read_checkpoint_round_by_sequence,
    read_review_generation_archive_page,
)
from .review_round_archive_audit import _current_execution
from .review_round_receipt_sweep import read_classified_checkpoint_round_receipt_members
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError,
    _read_execution_origin, workflow_scope,
)


_AUDIT_NAMESPACE = "review-checkpoint-archive-scan-v2"


def _audit_key(key: WorkflowKey, epoch: int, episode: str) -> str:
    if type(epoch) is not int or epoch < 1:
        raise WorkflowAuthorityError("checkpoint audit has an invalid execution epoch")
    validate_workflow_identifier(episode, "checkpoint audit episode")
    identity = json.dumps(
        (key.repository, key.target.number, epoch, episode),
        ensure_ascii=True, separators=(",", ":"),
    )
    return "review-checkpoint-scan:" + hashlib.sha256(identity.encode()).hexdigest()


def _read_state(
    db: sqlite3.Connection, repository: str, audit_key: str,
) -> tuple[int | None, ReviewCheckpointArchiveState | None]:
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
        raise WorkflowAuthorityError("checkpoint audit cursor is aliased or unreadable")
    row = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? AND scope=? "
        "AND key=? AND length(CAST(payload_json AS BLOB))<=?",
        (rows[0]["rowid"], _AUDIT_NAMESPACE, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise WorkflowAuthorityError("checkpoint audit cursor changed during bounded read")
    raw = row["payload_json"]
    try:
        state = decode_workflow(raw, ReviewCheckpointArchiveState)
        if encode_workflow(state) != raw:
            raise ValueError("noncanonical cursor")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("checkpoint audit cursor is malformed") from exc
    return rows[0]["rowid"], state


def _persist_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: ReviewCheckpointArchiveState, now: datetime,
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
        raise WorkflowAuthorityError("checkpoint audit cursor changed during update")


def _cursor(state: ReviewCheckpointArchiveState) -> ReviewCheckpointChainCursor | None:
    if isinstance(state, ReviewCheckpointArchiveAwaiting):
        return state.cursor
    if isinstance(state, ReviewCheckpointArchiveSegment):
        return state.parent.cursor
    return None


def _origin_source_sha256(state: ReviewCheckpointArchiveState) -> str:
    if isinstance(state, ReviewCheckpointArchiveAwaiting):
        return state.origin_source_sha256
    if isinstance(state, ReviewCheckpointArchiveSegment):
        return state.parent.origin_source_sha256
    if isinstance(state, CompletedReviewCheckpointArchiveAudit):
        return state.origin_source_sha256
    return _origin_source_sha256(state.prior)


def advance_review_checkpoint_archive_audit(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
    due_seconds: int = 3600,
) -> ReviewCheckpointArchiveState:
    """Persist one checkpoint, round or generation observation per transaction."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_CHECKPOINT_ARCHIVE_DUE_SECONDS):
        raise ValueError("checkpoint archive audit requires a transaction and bounded inputs")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("checkpoint archive audit requires a PR workflow")
    execution = _current_execution(db, key)
    tip_id = execution.checkpoint_receipt
    if tip_id is None:
        raise WorkflowAuthorityError("checkpoint archive audit lacks a compacted tip")
    origin, _ = _read_execution_origin(db, scope, execution)
    _, origin_digest = _read_recording_source(
        db, REVIEW_EXECUTION_ORIGIN_NAMESPACE,
        key.repository, origin.origin.evidence_id,
    )
    audit_key = _audit_key(
        key, execution.binding.execution_epoch, execution.binding.execution_episode,
    )
    rowid, state = _read_state(db, key.repository, audit_key)
    if isinstance(state, BlockedReviewCheckpointArchiveAudit):
        return state
    if state is not None and _origin_source_sha256(state) != origin_digest:
        result: ReviewCheckpointArchiveState = BlockedReviewCheckpointArchiveAudit(
            state, "checkpoint origin source changed since audit start", now,
        )
        _persist_state(db, key.repository, audit_key, rowid, result, now)
        return result
    cursor = _cursor(state) if state is not None else None
    if cursor is not None and (
        cursor.tip_receipt_id != tip_id
        or cursor.tip_checkpoint != execution.checkpoint
        or cursor.origin != execution.origin
    ):
        result = BlockedReviewCheckpointArchiveAudit(
            state, "checkpoint tip changed during audit", now,
        )
        _persist_state(db, key.repository, audit_key, rowid, result, now)
        return result
    if isinstance(state, CompletedReviewCheckpointArchiveAudit):
        if (state.tip_receipt_id == tip_id and state.origin == execution.origin
                and now < state.next_due_at):
            return state
        baseline = ReviewCheckpointArchiveBaseline(
            state.tip_receipt_id, state.chain_sha256,
        )
        state = None
    else:
        baseline = None
    if state is None:
        initial = ReviewCheckpointChainCursor(
            tip_id, execution.checkpoint, execution.origin,
            tip_id, execution.checkpoint, None,
        )
        state = begin_review_checkpoint_archive_scan(
            initial, origin_digest, now, due_seconds, baseline,
        )
    try:
        if isinstance(state, ReviewCheckpointArchiveAwaiting):
            page = read_review_checkpoint_chain_page(
                db, key, after=state.cursor, limit=1,
            )
            result = start_review_checkpoint_archive_segment(state, page)
        elif isinstance(state, ReviewCheckpointArchiveSegment):
            page = read_review_checkpoint_chain_page(
                db, key, after=state.parent.cursor, limit=1,
            )
            if page.sources != (state.source,) or page.next_cursor != state.next_cursor:
                raise WorkflowAuthorityError("checkpoint segment source changed after its page")
            receipt = state.source.receipt
            if state.next_round_sequence <= receipt.checkpoint.through_sequence:
                observed = read_checkpoint_round_by_sequence(
                    db, origin.checkpoint.state.binding, state.next_round_sequence,
                )
                classified = read_classified_checkpoint_round_receipt_members(
                    db, observed.source.round,
                )
                if classified.source.source != observed.source:
                    raise WorkflowAuthorityError("checkpoint round changed during classification")
                result = advance_review_checkpoint_archive_round(
                    state, observed.source, observed.outcome,
                )
            elif (state.next_generation
                  <= receipt.checkpoint.state.binding.rejection.fence.generation):
                page = read_review_generation_archive_page(
                    db, origin.checkpoint.state.binding, state.next_generation,
                    receipt.checkpoint.state.binding.rejection.fence.generation,
                    preceding=state.preceding, limit=1,
                )
                if isinstance(page, BlockedReviewGenerationArchivePage):
                    raise WorkflowAuthorityError(page.reason)
                if (not isinstance(page, ObservedReviewGenerationArchivePage)
                        or len(page.sources) != 1):
                    raise WorkflowAuthorityError("checkpoint generation page omitted its source")
                source = page.sources[0]
                fingerprint = ReviewGenerationArchiveFingerprint(
                    ReviewExecutionBoundaryCursor(
                        source.boundary.receipt_id, source.boundary.to_revision,
                    ),
                    source.receipt_digest, source.accepted_namespace,
                    source.accepted_key, source.accepted_digest,
                )
                result = advance_review_checkpoint_archive_generation(state, fingerprint)
            else:
                result = finish_review_checkpoint_archive_segment(state, now, due_seconds)
        else:
            raise WorkflowAuthorityError("checkpoint audit has an invalid persisted phase")
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error, WorkflowAuthorityError) as exc:
        result = BlockedReviewCheckpointArchiveAudit(
            state, (str(exc) or "checkpoint archived source is unreadable")[:512], now,
        )
    _persist_state(db, key.repository, audit_key, rowid, result, now)
    return result
