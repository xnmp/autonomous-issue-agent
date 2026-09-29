"""Durable finalized-prefix and complete receipt-namespace census."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from ..domain.review_execution import ActivatedReviewExecution, same_execution_episode
from ..domain.review_execution_activation import MAX_LEGACY_ROUND_PAGE_BYTES
from ..domain.review_round_archive import MAX_ROUND_ARCHIVE_DUE_SECONDS
from ..domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit, CompletedReviewRoundReceiptArchiveScan,
    ReviewRoundReceiptArchiveBaseline, ReviewRoundReceiptArchiveMember,
    ReviewRoundReceiptArchiveScan, ReviewRoundReceiptArchiveSource,
    ReviewRoundReceiptArchiveSweep, RoundReceiptArchiveState,
    advance_review_round_receipt_archive_scan,
    advance_review_round_receipt_archive_sweep,
    begin_review_round_receipt_archive_scan,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_execution_activation import (
    MAX_CHECKPOINT_ROUND_RECEIPT_BYTES, MAX_LEGACY_RECORDING_BYTES,
    _read_recording_source, read_checkpoint_round_by_sequence,
)
from .review_round_archive_audit import _current_execution
from .review_round_receipt_owner import (
    BlockedReviewRoundReceiptOwner, MatchedReviewRoundReceiptOwner,
    PendingOpenReviewRoundReceiptOwner, reconcile_review_round_receipt_owner,
)
from .review_round_receipt_sweep import (
    BlockedReviewRoundReceiptSweepPage, ClassifiedReviewRoundReceiptSweepPage,
    MAX_ROUND_RECEIPT_SWEEP_BYTES,
    read_classified_checkpoint_round_receipt_members,
    read_classified_review_round_receipt_sweep_page,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError, _read_execution_origin,
    workflow_scope,
)


_AUDIT_NAMESPACE = "review-round-receipt-archive-scan-v2"
MAX_ROUND_RECEIPT_AUDIT_STEP_SOURCE_BYTES = (
    2 * MAX_LEGACY_ROUND_PAGE_BYTES
    + 3 * MAX_CHECKPOINT_ROUND_RECEIPT_BYTES
    + MAX_ROUND_RECEIPT_SWEEP_BYTES
    + 6 * MAX_LEGACY_RECORDING_BYTES
    + 4 * MAX_WORKFLOW_PAYLOAD_BYTES
)


def _audit_key(key: WorkflowKey, epoch: int, episode: str) -> str:
    if type(epoch) is not int or epoch < 1:
        raise WorkflowAuthorityError("round receipt audit has an invalid execution epoch")
    validate_workflow_identifier(episode, "round receipt audit episode")
    identity = json.dumps(
        (key.repository, key.target.number, epoch, episode),
        ensure_ascii=True, separators=(",", ":"),
    )
    return "review-round-receipt-scan:" + hashlib.sha256(identity.encode()).hexdigest()


def _expected(state: RoundReceiptArchiveState) -> (
    ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan
):
    if isinstance(state, (ReviewRoundReceiptArchiveScan, CompletedReviewRoundReceiptArchiveScan)):
        return state
    if isinstance(state, BlockedReviewRoundReceiptArchiveScan):
        return state.prior
    if isinstance(state, (ReviewRoundReceiptArchiveSweep,
                          CompletedReviewRoundReceiptArchiveAudit)):
        return state.expected
    return state.prior.expected


def _read_state(
    db: sqlite3.Connection, repository: str, audit_key: str,
) -> tuple[int | None, RoundReceiptArchiveState | None]:
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
        raise WorkflowAuthorityError("round receipt audit cursor is aliased or unreadable")
    row = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? AND scope=? "
        "AND key=? AND length(CAST(payload_json AS BLOB))<=?",
        (rows[0]["rowid"], _AUDIT_NAMESPACE, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise WorkflowAuthorityError("round receipt audit cursor changed during bounded read")
    raw = row["payload_json"]
    try:
        state = decode_workflow(raw, RoundReceiptArchiveState)
        if encode_workflow(state) != raw:
            raise ValueError("noncanonical cursor")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("round receipt audit cursor is malformed") from exc
    return rows[0]["rowid"], state


def _persist_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: RoundReceiptArchiveState, now: datetime,
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
        raise WorkflowAuthorityError("round receipt audit cursor changed during update")


def _finalized_frontier(
    db: sqlite3.Connection, execution: ActivatedReviewExecution,
) -> int:
    binding = execution.binding
    key = binding.rejection.fence.key
    row = db.execute(
        "SELECT sequence,typeof(sequence) AS sequence_type "
        "FROM review_execution_rounds INDEXED BY review_execution_rounds_open "
        "WHERE repository=? AND target_number=? AND execution_epoch=? "
        "AND execution_episode=? AND outcome_json IS NULL "
        "ORDER BY sequence LIMIT 1",
        (key.repository, key.target.number, binding.execution_epoch,
         binding.execution_episode),
    ).fetchone()
    if row is None:
        return execution.last_sequence
    sequence = row["sequence"]
    if (row["sequence_type"] != "integer" or type(sequence) is not int
            or not 1 <= sequence <= execution.last_sequence):
        raise WorkflowAuthorityError("round receipt audit found an invalid open frontier")
    return sequence - 1


def _read_expected_source(
    db: sqlite3.Connection, scan: ReviewRoundReceiptArchiveScan,
) -> ReviewRoundReceiptArchiveSource:
    observed = read_checkpoint_round_by_sequence(db, scan.origin, scan.next_sequence)
    classified = read_classified_checkpoint_round_receipt_members(
        db, observed.source.round,
    )
    if classified.source.source != observed.source:
        raise WorkflowAuthorityError("round receipt expected source changed during read")
    round_id = observed.source.round
    return ReviewRoundReceiptArchiveSource(
        round_id, observed.source.row_sha256,
        classified.source.source.receipt_set_sha256,
        tuple(ReviewRoundReceiptArchiveMember(
            round_id, observed.source.row_sha256,
            receipt.member.namespace, receipt.member.key,
            receipt.member.source_sha256,
        ) for receipt in classified.receipts),
    )


def advance_review_round_receipt_archive_audit(
    db: sqlite3.Connection, key: WorkflowKey, *, now: datetime,
    due_seconds: int = 3600,
) -> RoundReceiptArchiveState:
    """Persist one bounded source page, without admission authority.

    One expected round rereads its 32 MiB receipt set to check each payload.
    A namespace page reads its source and one owner's full receipt set. The
    cumulative source-read ceiling derives from all these bounded readers;
    the namespace page's 16 MiB limit alone is not the audit-step bound.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None
            or type(due_seconds) is not int
            or not 1 <= due_seconds <= MAX_ROUND_ARCHIVE_DUE_SECONDS):
        raise ValueError("round receipt archive audit requires transaction, key and aware time")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("round receipt archive audit requires a PR workflow")
    execution = _current_execution(db, key)
    audit_key = _audit_key(
        key, execution.binding.execution_epoch, execution.binding.execution_episode,
    )
    rowid, state = _read_state(db, key.repository, audit_key)
    if isinstance(state, (BlockedReviewRoundReceiptArchiveScan,
                          BlockedReviewRoundReceiptArchiveSweep)):
        return state
    try:
        frontier = _finalized_frontier(db, execution)
    except (WorkflowAuthorityError, sqlite3.Error) as exc:
        if state is None:
            origin_receipt, _ = _read_execution_origin(db, scope, execution)
            _, origin_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
                origin_receipt.origin.evidence_id,
            )
            state = begin_review_round_receipt_archive_scan(
                origin_receipt.checkpoint.state.binding, origin_receipt.origin,
                origin_digest, 0, now, due_seconds,
            )
        reason = (str(exc) or "open round frontier is unreadable")[:512]
        result: RoundReceiptArchiveState = (
            BlockedReviewRoundReceiptArchiveScan(state, reason, None, now)
            if isinstance(state, (ReviewRoundReceiptArchiveScan,
                                  CompletedReviewRoundReceiptArchiveScan))
            else BlockedReviewRoundReceiptArchiveSweep(state, reason, None, now)
        )
        _persist_state(db, key.repository, audit_key, rowid, result, now)
        return result
    if state is None:
        origin_receipt, _ = _read_execution_origin(db, scope, execution)
        _, origin_digest = _read_recording_source(
            db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
            origin_receipt.origin.evidence_id,
        )
        state = begin_review_round_receipt_archive_scan(
            origin_receipt.checkpoint.state.binding, origin_receipt.origin,
            origin_digest, frontier, now, due_seconds,
        )
    else:
        prior = _expected(state)
        if (not same_execution_episode(prior.origin, execution.binding)
                or prior.origin_receipt != execution.origin):
            raise WorkflowAuthorityError("round receipt audit cursor changed its episode")
        if frontier < prior.through_sequence:
            result: RoundReceiptArchiveState = (
                BlockedReviewRoundReceiptArchiveScan(
                    state, "finalized round frontier moved backwards", None, now,
                ) if isinstance(state, (ReviewRoundReceiptArchiveScan,
                                        CompletedReviewRoundReceiptArchiveScan))
                else BlockedReviewRoundReceiptArchiveSweep(
                    state, "finalized round frontier moved backwards", None, now,
                )
            )
            _persist_state(db, key.repository, audit_key, rowid, result, now)
            return result
        try:
            _, current_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
                prior.origin_receipt.evidence_id,
            )
        except (TypeError, ValueError, UnicodeError, sqlite3.Error):
            current_digest = None
        if current_digest != prior.origin_source_sha256:
            result = (
                BlockedReviewRoundReceiptArchiveScan(
                    state, "execution origin source changed or is unreadable", None, now,
                ) if isinstance(state, (ReviewRoundReceiptArchiveScan,
                                        CompletedReviewRoundReceiptArchiveScan))
                else BlockedReviewRoundReceiptArchiveSweep(
                    state, "execution origin source changed or is unreadable", None, now,
                )
            )
            _persist_state(db, key.repository, audit_key, rowid, result, now)
            return result
        if isinstance(state, CompletedReviewRoundReceiptArchiveAudit):
            if frontier == state.expected.through_sequence and now < state.next_due_at:
                return state
            state = begin_review_round_receipt_archive_scan(
                prior.origin, prior.origin_receipt, prior.origin_source_sha256,
                frontier, now, due_seconds,
                ReviewRoundReceiptArchiveBaseline(
                    prior.through_sequence, prior.rolling_sha256,
                ), state.next_due_at,
            )
    if isinstance(state, ReviewRoundReceiptArchiveScan):
        try:
            source = _read_expected_source(db, state)
            result = advance_review_round_receipt_archive_scan(state, source, now)
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
                RecursionError, sqlite3.Error, WorkflowAuthorityError) as exc:
            result = BlockedReviewRoundReceiptArchiveScan(
                state, (str(exc) or "finalized round source is unreadable")[:512],
                state.next_sequence, now,
            )
    else:
        sweep = (ReviewRoundReceiptArchiveSweep(state)
                 if isinstance(state, CompletedReviewRoundReceiptArchiveScan) else state)
        if not isinstance(sweep, ReviewRoundReceiptArchiveSweep):
            raise WorkflowAuthorityError("round receipt audit has an invalid persisted phase")
        page = read_classified_review_round_receipt_sweep_page(
            db, after=sweep.after, upper=sweep.upper, limit=1,
        )
        if isinstance(page, BlockedReviewRoundReceiptSweepPage):
            result = BlockedReviewRoundReceiptArchiveSweep(
                sweep, page.reason[:512], page.source_rowid, now,
            )
        elif isinstance(page, ClassifiedReviewRoundReceiptSweepPage):
            members: list[ReviewRoundReceiptArchiveMember] = []
            pending = 0
            result = None
            for source in page.sources:
                owner = reconcile_review_round_receipt_owner(db, source)
                if isinstance(owner, BlockedReviewRoundReceiptOwner):
                    result = BlockedReviewRoundReceiptArchiveSweep(
                        sweep, owner.reason, owner.source_rowid, now,
                    )
                    break
                if isinstance(owner, PendingOpenReviewRoundReceiptOwner):
                    if (same_execution_episode(owner.round.binding, sweep.expected.origin)
                            and owner.round.sequence <= sweep.expected.through_sequence):
                        result = BlockedReviewRoundReceiptArchiveSweep(
                            sweep, "captured finalized prefix contains an open receipt",
                            source.source.source_rowid, now,
                        )
                        break
                    pending += 1
                elif isinstance(owner, MatchedReviewRoundReceiptOwner):
                    if (same_execution_episode(owner.round.binding, sweep.expected.origin)
                            and owner.round.sequence <= sweep.expected.through_sequence):
                        members.append(ReviewRoundReceiptArchiveMember(
                            owner.round, owner.round_source.row_sha256,
                            source.source.cursor.namespace, source.source.cursor.key,
                            source.source.source_sha256,
                        ))
                else:
                    raise WorkflowAuthorityError("round receipt owner returned an untyped result")
            if result is None:
                result = advance_review_round_receipt_archive_sweep(
                    sweep, tuple(members), page.next_cursor, page.upper_cursor,
                    page.has_more, pending, now, due_seconds,
                )
        else:
            raise WorkflowAuthorityError("round receipt namespace sweep returned an untyped page")
    _persist_state(db, key.repository, audit_key, rowid, result, now)
    return result
