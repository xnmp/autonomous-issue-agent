"""Durable, bounded generation-source scan; no compaction or admission authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

from ..domain.review_execution import (
    BlockedReviewGenerationArchiveScan,
    BlockedReviewGenerationArchiveSweep, CompletedReviewGenerationArchiveAudit,
    CompletedReviewGenerationArchiveScan, ReviewExecutionBoundaryCursor,
    ReviewGenerationArchiveBaseline, ReviewGenerationArchiveFingerprint,
    ReviewGenerationArchiveCursor,
    ReviewGenerationArchiveReceiptMember,
    ReviewGenerationArchiveScan, ReviewGenerationArchiveSweep,
    MAX_ARCHIVE_AUDIT_DUE_SECONDS,
    advance_review_generation_archive_scan, advance_review_generation_archive_sweep,
    begin_review_generation_archive_scan,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, BlockedReviewGenerationReceiptSweepPage,
    ObservedReviewGenerationArchivePage, ObservedReviewGenerationReceiptSweepPage,
    _read_recording_source, read_reconciled_review_generation_receipt_sweep_page,
    read_review_generation_archive_page,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .review_round_archive_audit import _current_execution
from .review_archive_source_versions import read_review_archive_source_versions
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError, _read_execution_origin,
    workflow_scope,
)


_AUDIT_NAMESPACE = "review-generation-archive-scan"
ARCHIVE_AUDIT_PAGE_SIZE = 16
_AuditState = (
    ReviewGenerationArchiveScan
    | CompletedReviewGenerationArchiveScan
    | ReviewGenerationArchiveSweep
    | CompletedReviewGenerationArchiveAudit
    | BlockedReviewGenerationArchiveScan
    | BlockedReviewGenerationArchiveSweep
)


def _generation_scan(state: _AuditState) -> ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan:
    if isinstance(state, (ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan)):
        return state
    if isinstance(state, BlockedReviewGenerationArchiveScan):
        return state.scan
    if isinstance(state, ReviewGenerationArchiveSweep):
        return state.generation_scan
    if isinstance(state, CompletedReviewGenerationArchiveAudit):
        return state.generation_scan
    return state.prior.generation_scan


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
) -> tuple[int | None, _AuditState | None, int | None, ReviewGenerationArchiveBaseline | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_AUDIT_NAMESPACE, _AUDIT_NAMESPACE, repository, repository, audit_key, audit_key),
    ).fetchall()
    if not rows:
        return None, None, None, None
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
        decoded = decode_workflow(raw, object)
        if encode_workflow(decoded) != raw:
            raise ValueError("noncanonical cursor")
        if isinstance(decoded, ReviewGenerationArchiveCursor):
            state, version = decoded.state, decoded.generation_sources_version
            prior_baseline = decoded.prior_baseline
        elif isinstance(decoded, (
            ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan,
            ReviewGenerationArchiveSweep, CompletedReviewGenerationArchiveAudit,
            BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep,
        )):
            state, version, prior_baseline = decoded, None, None
        else:
            raise ValueError("wrong generation archive cursor variant")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("generation archive scan cursor is malformed") from exc
    return rows[0]["rowid"], state, version, prior_baseline


def _persist_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: _AuditState, source_version: int,
    prior_baseline: ReviewGenerationArchiveBaseline | None, now: datetime,
) -> None:
    encoded = encode_workflow(ReviewGenerationArchiveCursor(state, source_version, prior_baseline))
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
    """Check and persist one generation or namespace page in one write transaction.

    The complete pass reconciles retained generation receipts with the
    namespace census. Checkpoint ancestry, retired rounds and the admission
    gate remain separate obligations before compaction can use its result.
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
    source_version = read_review_archive_source_versions(db, key.repository).generation_sources
    rowid, state, stored_version, prior_baseline = _read_state(db, key.repository, audit_key)
    if state is None:
        execution = _current_execution(db, key)
        origin_receipt, _ = _read_execution_origin(db, scope, execution)
        _, origin_source_sha256 = _read_recording_source(
            db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
            origin_receipt.origin.evidence_id,
        )
        origin = origin_receipt.checkpoint.state.binding
        if (execution.origin != origin_receipt.origin
                or execution.binding.execution_epoch != epoch
                or execution.binding.execution_episode != episode
                or execution.binding.rejection.fence.generation != current_generation):
            raise WorkflowAuthorityError("generation archive audit origin disagrees with active execution")
        state = begin_review_generation_archive_scan(
            origin, origin_receipt.origin, origin_source_sha256,
            current_generation, now, due_seconds,
        )
    else:
        generation_scan = _generation_scan(state)
        origin = generation_scan.origin
        cause = generation_scan.origin_receipt
        if (origin.rejection.fence.key != key
                or origin.execution_epoch != epoch or origin.execution_episode != episode):
            raise WorkflowAuthorityError("generation archive audit cursor changed its episode")
        if isinstance(state, (BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep)):
            if now < state.detected_at:
                raise ValueError("generation archive audit time moved backwards")
            if stored_version is None or stored_version == source_version:
                return state
            state = (state.scan if isinstance(state, BlockedReviewGenerationArchiveScan)
                     else state.prior)
        if current_generation < generation_scan.through_generation:
            if isinstance(state, (ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan)):
                generation = (state.next_generation if isinstance(state, ReviewGenerationArchiveScan)
                              else state.through_generation)
                result: _AuditState = BlockedReviewGenerationArchiveScan(
                    state, generation, "indexed generation frontier moved backwards", now,
                )
            else:
                result = BlockedReviewGenerationArchiveSweep(
                    state, "indexed generation frontier moved backwards", None, now,
                )
            _persist_state(db, key.repository, audit_key, rowid, result, source_version,
                           prior_baseline, now)
            return result
        try:
            _, source_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository, cause.evidence_id,
            )
            origin_discrepancy = source_digest != generation_scan.origin_source_sha256
        except (ValueError, TypeError, UnicodeError, sqlite3.Error):
            origin_discrepancy = True
        if origin_discrepancy:
            if isinstance(state, (ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan)):
                generation = (state.next_generation if isinstance(state, ReviewGenerationArchiveScan)
                              else state.through_generation)
                result: _AuditState = BlockedReviewGenerationArchiveScan(
                    state, generation, "execution origin source changed or is unreadable", now,
                )
            else:
                result = BlockedReviewGenerationArchiveSweep(
                    state, "execution origin source changed or is unreadable", None, now,
                )
            _persist_state(db, key.repository, audit_key, rowid, result, source_version,
                           prior_baseline, now)
            return result
        if stored_version != source_version:
            baseline = (
                (prior_baseline or generation_scan.baseline)
                if isinstance(generation_scan, ReviewGenerationArchiveScan)
                else ReviewGenerationArchiveBaseline(
                    generation_scan.through_generation, generation_scan.rolling_sha256,
                )
            )
            prior_baseline = baseline
            prior_due = (state.next_due_at if isinstance(state, CompletedReviewGenerationArchiveAudit)
                         else generation_scan.due_at)
            state = begin_review_generation_archive_scan(
                origin, cause, generation_scan.origin_source_sha256,
                current_generation, now, due_seconds, baseline,
            )
            state = replace(state, due_at=min(state.due_at, prior_due))
        elif isinstance(state, CompletedReviewGenerationArchiveAudit) and (
            current_generation > state.generation_scan.through_generation or now >= state.next_due_at
        ):
            prior_due = state.next_due_at
            prior_baseline = ReviewGenerationArchiveBaseline(
                generation_scan.through_generation, generation_scan.rolling_sha256,
            )
            state = begin_review_generation_archive_scan(
                origin, cause, generation_scan.origin_source_sha256,
                current_generation, now, due_seconds,
                prior_baseline,
            )
            if isinstance(state, (ReviewGenerationArchiveScan, CompletedReviewGenerationArchiveScan)):
                state = replace(state, due_at=min(state.due_at, prior_due))
        elif isinstance(state, CompletedReviewGenerationArchiveAudit):
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
            result = advance_review_generation_archive_scan(state, fingerprints, now)
            if not isinstance(result, BlockedReviewGenerationArchiveScan):
                expected_next = (result.next_generation if isinstance(result, ReviewGenerationArchiveScan)
                                 else result.through_generation + 1)
                if page.next_generation != expected_next:
                    raise WorkflowAuthorityError("generation archive page cursor changed during audit")
        else:
            result = BlockedReviewGenerationArchiveScan(
                state, state.next_generation, "generation archive page returned no expected sources", now,
            )
    else:
        sweep = (ReviewGenerationArchiveSweep(state)
                 if isinstance(state, CompletedReviewGenerationArchiveScan) else state)
        if not isinstance(sweep, ReviewGenerationArchiveSweep):
            raise WorkflowAuthorityError("generation archive audit has an invalid persisted phase")
        page = read_reconciled_review_generation_receipt_sweep_page(
            db, sweep.generation_scan.origin, current_generation,
            after=sweep.after, upper=sweep.upper,
        )
        if isinstance(page, BlockedReviewGenerationReceiptSweepPage):
            result = BlockedReviewGenerationArchiveSweep(
                sweep, (page.reason or "generation receipt namespace is unreadable")[:512],
                page.source_rowid, now,
            )
        elif isinstance(page, ObservedReviewGenerationReceiptSweepPage):
            members = tuple(ReviewGenerationArchiveReceiptMember(
                source.boundary.to_revision.fence.generation,
                source.boundary.receipt_id, source.receipt_digest,
                source.accepted_namespace, source.accepted_key,
            ) for source in page.sources
                if source.boundary.to_revision.fence.generation <= sweep.generation_scan.through_generation)
            result = advance_review_generation_archive_sweep(
                sweep, members, page.next_cursor, page.upper_cursor,
                page.has_more, now, due_seconds,
            )
        else:
            raise WorkflowAuthorityError("generation receipt sweep returned an untyped page")
    _persist_state(db, key.repository, audit_key, rowid, result, source_version,
                   prior_baseline, now)
    return result
