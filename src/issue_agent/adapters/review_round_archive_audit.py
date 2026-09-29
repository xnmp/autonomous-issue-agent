"""Durable round-row census; no checkpoint or admission authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from ..domain.review_execution import ActivatedReviewExecution, same_execution_episode
from ..domain.review_round_archive import (
    MAX_ROUND_ARCHIVE_DUE_SECONDS, BlockedReviewRoundArchiveScan,
    CompletedReviewRoundArchiveScan, ReviewRoundArchiveBaseline,
    ReviewRoundArchiveCursor, ReviewRoundArchiveFingerprint,
    ReviewRoundArchiveScan, RoundArchiveState,
    advance_review_round_archive_scan, begin_review_round_archive_scan,
)
from ..domain.review_workflow import ReviewFamilyEpisodes
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_execution_activation import (
    BlockedReviewCheckpointRoundSweepPage, ObservedReviewCheckpointRoundSweepPage,
    _read_recording_source, read_checkpoint_round_sweep_page,
)
from .review_archive_source_versions import read_review_archive_source_versions
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError, _read_execution_origin,
    workflow_scope,
)


_AUDIT_NAMESPACE = "review-round-archive-scan"


def _audit_key(key: WorkflowKey, epoch: int, episode: str) -> str:
    if type(epoch) is not int or epoch < 1:
        raise WorkflowAuthorityError("round archive scan has an invalid execution epoch")
    validate_workflow_identifier(episode, "round archive scan episode")
    identity = json.dumps(
        (key.repository, key.target.number, epoch, episode),
        ensure_ascii=True, separators=(",", ":"),
    )
    return "review-round-scan:" + hashlib.sha256(identity.encode()).hexdigest()


def _current_execution(db: sqlite3.Connection, key: WorkflowKey) -> ActivatedReviewExecution:
    scope = workflow_scope(key)
    metadata = db.execute(
        "SELECT w.rowid AS source_rowid,typeof(w.episode_json) AS episode_type,"
        "length(CAST(w.episode_json AS BLOB)) AS episode_bytes,"
        "typeof(w.generation) AS generation_type,w.generation,"
        "typeof(c.epoch) AS epoch_type,c.epoch,"
        "typeof(c.episode_id) AS episode_id_type,c.episode_id "
        "FROM workflows AS w JOIN workflow_components AS c "
        "ON c.repository=w.repository AND c.target_kind=w.target_kind "
        "AND c.target_number=w.target_number "
        "WHERE w.repository=? AND w.target_kind=? AND w.target_number=? "
        "AND c.family='review_execution'",
        scope,
    ).fetchone()
    if (metadata is None or metadata["episode_type"] != "text"
            or type(metadata["episode_bytes"]) is not int
            or metadata["episode_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES
            or metadata["generation_type"] != "integer"
            or metadata["epoch_type"] != "integer"
            or metadata["episode_id_type"] != "text"):
        raise WorkflowAuthorityError("round archive scan lacks bounded indexed execution")
    row = db.execute(
        "SELECT episode_json FROM workflows WHERE rowid=? AND repository=? "
        "AND target_kind=? AND target_number=? AND length(CAST(episode_json AS BLOB))<=?",
        (metadata["source_rowid"], *scope, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise WorkflowAuthorityError("round archive execution changed during its bounded read")
    try:
        family = decode_workflow(row["episode_json"], ReviewFamilyEpisodes)
        execution = family.execution
        if (encode_workflow(family) != row["episode_json"]
                or not isinstance(execution, ActivatedReviewExecution)
                or execution.binding.rejection.fence.key != key
                or execution.binding.execution_epoch != metadata["epoch"]
                or execution.binding.execution_episode != metadata["episode_id"]
                or execution.state.binding.rejection.fence.generation != metadata["generation"]):
            raise ValueError("indexed execution disagrees with its component")
        return execution
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("round archive scan has an unreadable execution") from exc


def _read_state(
    db: sqlite3.Connection, repository: str, audit_key: str,
) -> tuple[int | None, RoundArchiveState | None, int | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_AUDIT_NAMESPACE, _AUDIT_NAMESPACE, repository, repository, audit_key, audit_key),
    ).fetchall()
    if not rows:
        return None, None, None
    if (len(rows) != 1 or any(rows[0][f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(rows[0]["payload_bytes"]) is not int
            or rows[0]["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("round archive scan cursor is aliased or unreadable")
    row = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? AND scope=? "
        "AND key=? AND length(CAST(payload_json AS BLOB))<=?",
        (rows[0]["rowid"], _AUDIT_NAMESPACE, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise WorkflowAuthorityError("round archive scan cursor changed during its bounded read")
    raw = row["payload_json"]
    try:
        decoded = decode_workflow(raw, object)
        if encode_workflow(decoded) != raw:
            raise ValueError("noncanonical cursor")
        if isinstance(decoded, ReviewRoundArchiveCursor):
            state, version = decoded.state, decoded.round_rows_version
        elif isinstance(decoded, (
            ReviewRoundArchiveScan, CompletedReviewRoundArchiveScan,
            BlockedReviewRoundArchiveScan,
        )):
            state, version = decoded, None
        else:
            raise ValueError("wrong round archive cursor variant")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("round archive scan cursor is malformed") from exc
    return rows[0]["rowid"], state, version


def _persist_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: RoundArchiveState, round_rows_version: int, now: datetime,
) -> None:
    encoded = encode_workflow(ReviewRoundArchiveCursor(state, round_rows_version))
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
        raise WorkflowAuthorityError("round archive scan cursor changed during its update")


def advance_review_round_archive_audit(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime, due_seconds: int = 3600,
) -> RoundArchiveState:
    """Persist one bounded repository page and account for this episode's rows."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_ROUND_ARCHIVE_DUE_SECONDS):
        raise ValueError("round archive audit requires a transaction, key and aware time")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("round archive audit requires a PR workflow")
    execution = _current_execution(db, key)
    audit_key = _audit_key(key, execution.binding.execution_epoch, execution.binding.execution_episode)
    round_rows_version = read_review_archive_source_versions(db, key.repository).round_rows
    rowid, state, stored_version = _read_state(db, key.repository, audit_key)
    if state is not None:
        prior = state.prior if isinstance(state, BlockedReviewRoundArchiveScan) else state
        if (not same_execution_episode(prior.origin, execution.binding)
                or prior.origin_receipt != execution.origin):
            raise WorkflowAuthorityError("round archive cursor changed its execution episode")
        if isinstance(state, BlockedReviewRoundArchiveScan):
            return state
        if now < (state.started_at if isinstance(state, ReviewRoundArchiveScan) else state.completed_at):
            raise ValueError("round archive audit time moved backwards")
        try:
            _, origin_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
                state.origin_receipt.evidence_id,
            )
        except (TypeError, ValueError, UnicodeError, sqlite3.Error):
            origin_digest = None
        if origin_digest != state.origin_source_sha256:
            result: RoundArchiveState = BlockedReviewRoundArchiveScan(
                state, "execution origin source changed or is unreadable", None, now,
            )
            _persist_state(db, key.repository, audit_key, rowid, result, round_rows_version, now)
            return result
        if execution.last_sequence < state.through_sequence:
            result = BlockedReviewRoundArchiveScan(
                state, "indexed round frontier moved backwards", None, now,
            )
            _persist_state(db, key.repository, audit_key, rowid, result, round_rows_version, now)
            return result
    if state is None or stored_version != round_rows_version:
        baseline = None
        deadline = None
        if state is not None:
            if isinstance(state, CompletedReviewRoundArchiveScan):
                baseline = ReviewRoundArchiveBaseline(
                    state.through_sequence, state.membership_xor,
                )
            else:
                baseline, deadline = state.baseline, state.due_at
        origin_receipt, _ = _read_execution_origin(db, scope, execution)
        _, origin_digest = _read_recording_source(
            db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
            origin_receipt.origin.evidence_id,
        )
        state = begin_review_round_archive_scan(
            origin_receipt.checkpoint.state.binding, origin_receipt.origin,
            origin_digest, execution.last_sequence, now, due_seconds,
            baseline, deadline,
        )
    else:
        if isinstance(state, CompletedReviewRoundArchiveScan):
            if execution.last_sequence == state.through_sequence and now < state.next_due_at:
                return state
            state = begin_review_round_archive_scan(
                state.origin, state.origin_receipt, state.origin_source_sha256,
                execution.last_sequence, now, due_seconds,
                ReviewRoundArchiveBaseline(state.through_sequence, state.membership_xor),
                state.next_due_at,
            )
    if not isinstance(state, ReviewRoundArchiveScan):
        raise WorkflowAuthorityError("round archive audit has an invalid persisted phase")
    page = read_checkpoint_round_sweep_page(
        db, state.origin, after_rowid=state.after_rowid, upper_rowid=state.upper_rowid,
    )
    if isinstance(page, BlockedReviewCheckpointRoundSweepPage):
        result = BlockedReviewRoundArchiveScan(
            state, page.reason[:512], page.source_rowid, now,
        )
    elif isinstance(page, ObservedReviewCheckpointRoundSweepPage):
        fingerprints = tuple(ReviewRoundArchiveFingerprint(
            source.source_rowid, dict(source.fields)["sequence"],
            dict(source.fields)["round_id"], source.source_digest,
        ) for source in page.sources)
        result = advance_review_round_archive_scan(
            state, fingerprints, page.next_rowid, page.upper_rowid,
            page.has_more, now, due_seconds,
        )
    else:
        raise WorkflowAuthorityError("round archive sweep returned an untyped page")
    _persist_state(db, key.repository, audit_key, rowid, result, round_rows_version, now)
    return result
