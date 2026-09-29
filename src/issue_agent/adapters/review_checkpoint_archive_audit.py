"""Durable checkpoint-chain and retired-segment scan; no admission authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

from ..domain.review_checkpoint_archive import (
    BlockedReviewCheckpointArchiveAudit, CompletedReviewCheckpointArchiveAudit,
    MAX_CHECKPOINT_ARCHIVE_DUE_SECONDS, ReviewCheckpointArchiveAwaiting,
    ReviewCheckpointArchiveBaseline, ReviewCheckpointArchiveSegment,
    ReviewCheckpointArchivePendingBoundary, ReviewCheckpointArchiveReplaySegment,
    ReviewCheckpointArchiveState, ReviewCheckpointArchiveStorageAnchor,
    advance_review_checkpoint_archive_round, begin_review_checkpoint_archive_scan,
    finish_review_checkpoint_archive_replay_segment,
    fold_checkpoint_generation_stream, start_review_checkpoint_archive_segment,
)
from ..domain.review_checkpoint_stream_replay import (
    _CheckpointReplayBetweenBoundaries, _CheckpointReplayWithinBoundary,
    _CheckpointReplayTail, advance_checkpoint_replay_boundary_round,
    advance_checkpoint_replay_tail_round, begin_checkpoint_stream_replay,
    finish_checkpoint_replay_boundary, start_checkpoint_replay_boundary,
    start_checkpoint_replay_tail,
)
from ..domain.review_checkpoint_chain import (
    ObservedReviewCheckpointChainSource, ReviewCheckpointChainCursor,
)
from ..domain.review_checkpoint_application import (
    application_proposal_from_receipt,
)
from ..domain.review_execution import (
    ReviewExecutionBoundaryCursor,
)
from ..domain.workflow_identity import WorkflowKey, validate_workflow_identifier
from .review_checkpoint_chain import read_review_checkpoint_chain_page
from .review_checkpoint_application_source import read_review_checkpoint_application_source
from .review_checkpoint_following_source import (
    checkpoint_following_fingerprint, read_checkpoint_following_application_source,
)
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, ObservedReviewGenerationArchivePage,
    _read_recording_source, read_checkpoint_round_by_sequence,
    read_review_generation_archive_page,
)
from .review_round_archive_audit import _current_execution
from .review_round_receipt_sweep import read_classified_checkpoint_round_receipt_members
from .workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, _decode, _encode, _unique_object,
    WorkflowEncodingError, decode_workflow, encode_workflow,
)
from .workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError,
    _read_execution_origin, workflow_scope,
)


_AUDIT_NAMESPACE = "review-checkpoint-archive-scan-v3"
_V2_AUDIT_NAMESPACE = "review-checkpoint-archive-scan-v2"
_LEGACY_AUDIT_NAMESPACE = "review-checkpoint-archive-scan"
_LEGACY_SEGMENT_FIELDS = frozenset((
    "parent", "source", "next_cursor", "next_round_sequence", "next_generation",
    "preceding", "round_stream_sha256", "generation_stream_sha256",
))


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
    namespace: str = _AUDIT_NAMESPACE,
) -> tuple[int | None, ReviewCheckpointArchiveState | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (namespace, namespace, repository, repository, audit_key, audit_key),
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
        (rows[0]["rowid"], namespace, repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
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


def _v2_discrepancy(db: sqlite3.Connection, repository: str, audit_key: str) -> str | None:
    try:
        _, state = _read_state(db, repository, audit_key, _V2_AUDIT_NAMESPACE)
    except WorkflowAuthorityError:
        return "prior checkpoint audit cursor is malformed or unreadable"
    if isinstance(state, BlockedReviewCheckpointArchiveAudit):
        return "prior checkpoint audit recorded an unresolved discrepancy"
    if isinstance(state, ReviewCheckpointArchiveReplaySegment):
        return "prior checkpoint audit cursor has an invalid version"
    return None


def _legacy_discrepancy(
    db: sqlite3.Connection, repository: str, audit_key: str,
) -> str | None:
    """Retain v1 blockers while restarting valid observational cursors at origin."""
    rows = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type,typeof(payload_json) AS payload_type,"
        "length(CAST(payload_json AS BLOB)) AS payload_bytes "
        "FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_LEGACY_AUDIT_NAMESPACE, _LEGACY_AUDIT_NAMESPACE,
         repository, repository, audit_key, audit_key),
    ).fetchall()
    if not rows:
        return None
    if (len(rows) != 1 or any(rows[0][f"{name}_type"] != "text" for name in (
        "namespace", "scope", "key", "payload",
    )) or type(rows[0]["payload_bytes"]) is not int
            or rows[0]["payload_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        return "prior checkpoint audit cursor is aliased or unreadable"
    row = db.execute(
        "SELECT payload_json FROM level_state WHERE rowid=? AND namespace=? "
        "AND scope=? AND key=? AND length(CAST(payload_json AS BLOB))<=?",
        (rows[0]["rowid"], _LEGACY_AUDIT_NAMESPACE,
         repository, audit_key, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        return "prior checkpoint audit cursor changed during read"
    raw = row["payload_json"]
    try:
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(envelope, dict) or set(envelope) != {"format", "value"}
                or type(envelope["format"]) is not int or envelope["format"] != 1
                or not isinstance(envelope["value"], dict)
                or set(envelope["value"]) != {"type", "fields"}
                or not isinstance(envelope["value"]["fields"], dict)
                or json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False) != raw):
            raise ValueError("noncanonical prior cursor")
        tag = envelope["value"]["type"]
        if tag == "BlockedReviewCheckpointArchiveAudit":
            return "prior checkpoint audit recorded an unresolved discrepancy"
        if tag == "ReviewCheckpointArchiveSegment":
            fields = envelope["value"]["fields"]
            if set(fields) != _LEGACY_SEGMENT_FIELDS:
                raise ValueError("malformed prior segment")
            decoded = {name: _decode(value) for name, value in fields.items()}
            if (not isinstance(decoded["parent"], ReviewCheckpointArchiveAwaiting)
                    or not isinstance(decoded["source"], ObservedReviewCheckpointChainSource)
                    or decoded["next_cursor"] is not None
                    and not isinstance(decoded["next_cursor"], ReviewCheckpointChainCursor)
                    or type(decoded["next_round_sequence"]) is not int
                    or type(decoded["next_generation"]) is not int
                    or decoded["preceding"] is not None
                    and not isinstance(decoded["preceding"], ReviewExecutionBoundaryCursor)
                    or any(not isinstance(decoded[name], str)
                           or len(decoded[name]) != 64
                           or any(char not in "0123456789abcdef" for char in decoded[name])
                           for name in ("round_stream_sha256", "generation_stream_sha256"))):
                raise ValueError("malformed prior segment")
            parent = decoded["parent"]
            source = decoded["source"]
            next_cursor = decoded["next_cursor"]
            previous = source.receipt.previous_checkpoint
            current = source.receipt.checkpoint
            if (source.receipt_id != parent.cursor.next_receipt_id
                    or current != parent.cursor.expected_checkpoint
                    or (next_cursor is None)
                    != (source.receipt.previous_receipt_id is None)
                    or next_cursor is not None
                    and next_cursor.next_receipt_id
                    != source.receipt.previous_receipt_id
                    or not previous.through_sequence + 1
                    <= decoded["next_round_sequence"] <= current.through_sequence + 1
                    or not previous.state.binding.rejection.fence.generation + 1
                    <= decoded["next_generation"]
                    <= current.state.binding.rejection.fence.generation + 1):
                raise ValueError("malformed prior segment")
            canonical = {"format": 1, "value": {
                "type": tag,
                "fields": {name: _encode(value) for name, value in decoded.items()},
            }}
            if json.dumps(
                canonical, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False,
            ) != raw:
                raise ValueError("noncanonical prior segment")
        elif tag in ("ReviewCheckpointArchiveAwaiting",
                     "CompletedReviewCheckpointArchiveAudit"):
            state = decode_workflow(
                raw, ReviewCheckpointArchiveAwaiting | CompletedReviewCheckpointArchiveAudit,
            )
            if encode_workflow(state) != raw:
                raise ValueError("noncanonical prior cursor")
        else:
            raise ValueError("unknown prior cursor")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return "prior checkpoint audit cursor is malformed"
    return None


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


def _persist_bounded_state(
    db: sqlite3.Connection, repository: str, audit_key: str, rowid: int | None,
    state: ReviewCheckpointArchiveState, tip_id: str, origin_digest: str,
    now: datetime,
) -> ReviewCheckpointArchiveState:
    try:
        _persist_state(db, repository, audit_key, rowid, state, now)
    except WorkflowEncodingError:
        # Source and cursor limits differ; the fixed-size anchor also covers
        # blockers constructed from an already large prior cursor.
        state = BlockedReviewCheckpointArchiveAudit(
            ReviewCheckpointArchiveStorageAnchor(tip_id, origin_digest),
            "checkpoint archive audit cursor exceeds its storage bound", now,
        )
        _persist_state(db, repository, audit_key, rowid, state, now)
    return state


def _cursor(state: ReviewCheckpointArchiveState) -> ReviewCheckpointChainCursor | None:
    if isinstance(state, ReviewCheckpointArchiveAwaiting):
        return state.cursor
    if isinstance(state, ReviewCheckpointArchiveSegment):
        return state.parent.cursor
    if isinstance(state, ReviewCheckpointArchiveReplaySegment):
        return state.segment.parent.cursor
    return None


def _origin_source_sha256(state: ReviewCheckpointArchiveState) -> str:
    if isinstance(state, ReviewCheckpointArchiveAwaiting):
        return state.origin_source_sha256
    if isinstance(state, ReviewCheckpointArchiveSegment):
        return state.parent.origin_source_sha256
    if isinstance(state, ReviewCheckpointArchiveReplaySegment):
        return state.segment.parent.origin_source_sha256
    if isinstance(state, CompletedReviewCheckpointArchiveAudit):
        return state.origin_source_sha256
    if isinstance(state.prior, ReviewCheckpointArchiveStorageAnchor):
        return state.prior.origin_source_sha256
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
    if isinstance(state, ReviewCheckpointArchiveSegment):
        raise WorkflowAuthorityError("checkpoint audit cursor has an invalid version")
    legacy_reason = (_legacy_discrepancy(db, key.repository, audit_key)
                     or _v2_discrepancy(db, key.repository, audit_key))
    if legacy_reason is not None:
        if state is None:
            initial = ReviewCheckpointChainCursor(
                tip_id, execution.checkpoint, execution.origin,
                tip_id, execution.checkpoint, None,
            )
            state = begin_review_checkpoint_archive_scan(
                initial, origin_digest, now, due_seconds,
            )
        blocked = BlockedReviewCheckpointArchiveAudit(state, legacy_reason, now)
        return _persist_bounded_state(
            db, key.repository, audit_key, rowid, blocked, tip_id, origin_digest, now,
        )
    if state is not None and _origin_source_sha256(state) != origin_digest:
        result: ReviewCheckpointArchiveState = BlockedReviewCheckpointArchiveAudit(
            state, "checkpoint origin source changed since audit start", now,
        )
        return _persist_bounded_state(
            db, key.repository, audit_key, rowid, result, tip_id, origin_digest, now,
        )
    cursor = _cursor(state) if state is not None else None
    if cursor is not None and (
        cursor.tip_receipt_id != tip_id
        or cursor.tip_checkpoint != execution.checkpoint
        or cursor.origin != execution.origin
    ):
        result = BlockedReviewCheckpointArchiveAudit(
            state, "checkpoint tip changed during audit", now,
        )
        return _persist_bounded_state(
            db, key.repository, audit_key, rowid, result, tip_id, origin_digest, now,
        )
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
            application = read_review_checkpoint_application_source(
                db, key.repository, page.sources[0].receipt_id,
            )
            if application.checkpoint != page.sources[0].receipt:
                raise WorkflowAuthorityError("checkpoint application changed its checkpoint source")
            read_checkpoint_following_application_source(
                db, origin.checkpoint.state.binding,
                execution.binding.rejection.fence.generation, application.application,
            )
            segment = start_review_checkpoint_archive_segment(
                state, page, application.recorded, application.source_sha256,
            )
            result = ReviewCheckpointArchiveReplaySegment(
                segment, begin_checkpoint_stream_replay(application.application),
            )
        elif isinstance(state, ReviewCheckpointArchiveReplaySegment):
            segment = state.segment
            page = read_review_checkpoint_chain_page(
                db, key, after=segment.parent.cursor, limit=1,
            )
            if page.sources != (segment.source,) or page.next_cursor != segment.next_cursor:
                raise WorkflowAuthorityError("checkpoint segment source changed after its page")
            application = read_review_checkpoint_application_source(
                db, key.repository, segment.source.receipt_id,
            )
            if (application.checkpoint != segment.source.receipt
                    or application.recorded != segment.application
                    or application.application != application_proposal_from_receipt(
                        segment.source.receipt, segment.application,
                    ) or application.source_sha256 != segment.application_source_sha256):
                raise WorkflowAuthorityError("checkpoint application changed after its page")
            following = read_checkpoint_following_application_source(
                db, origin.checkpoint.state.binding,
                execution.binding.rejection.fence.generation, application.application,
            )
            receipt = segment.source.receipt
            replay = state.replay
            if isinstance(replay, _CheckpointReplayBetweenBoundaries) and (
                replay.next_generation
                <= receipt.checkpoint.state.binding.rejection.fence.generation
            ):
                generation = replay.next_generation
                generation_page = read_review_generation_archive_page(
                    db, origin.checkpoint.state.binding, generation, generation,
                    preceding=replay.preceding, limit=1,
                )
                if isinstance(generation_page, BlockedReviewGenerationArchivePage):
                    raise WorkflowAuthorityError(generation_page.reason)
                if (not isinstance(generation_page, ObservedReviewGenerationArchivePage)
                        or len(generation_page.sources) != 1):
                    raise WorkflowAuthorityError("checkpoint generation page omitted its source")
                source = generation_page.sources[0]
                fingerprint = checkpoint_following_fingerprint(source)
                pending = start_checkpoint_replay_boundary(replay, source.boundary)
                result = ReviewCheckpointArchiveReplaySegment(
                    replace(segment, generation_stream_sha256=fold_checkpoint_generation_stream(
                        segment.generation_stream_sha256, generation, fingerprint,
                    )), ReviewCheckpointArchivePendingBoundary(
                        pending.progress, fingerprint, source.boundary.through_sequence,
                    ),
                )
            elif isinstance(replay, ReviewCheckpointArchivePendingBoundary):
                generation_page = read_review_generation_archive_page(
                    db, origin.checkpoint.state.binding, replay.progress.next_generation,
                    replay.progress.next_generation, preceding=replay.progress.preceding,
                    limit=1,
                )
                if isinstance(generation_page, BlockedReviewGenerationArchivePage):
                    raise WorkflowAuthorityError(generation_page.reason)
                if (not isinstance(generation_page, ObservedReviewGenerationArchivePage)
                        or len(generation_page.sources) != 1):
                    raise WorkflowAuthorityError("checkpoint pending generation source is missing")
                source = generation_page.sources[0]
                if (checkpoint_following_fingerprint(source) != replay.source
                        or source.boundary.through_sequence != replay.through_sequence):
                    raise WorkflowAuthorityError("checkpoint pending generation source changed")
                pending = _CheckpointReplayWithinBoundary(replay.progress, source.boundary)
                if pending.progress.next_round_sequence > pending.boundary.through_sequence:
                    progressed = finish_checkpoint_replay_boundary(pending)
                    result = ReviewCheckpointArchiveReplaySegment(
                        replace(segment, next_generation=progressed.next_generation,
                                preceding=progressed.preceding), progressed,
                    )
                else:
                    observed = read_checkpoint_round_by_sequence(
                        db, origin.checkpoint.state.binding, segment.next_round_sequence,
                    )
                    classified = read_classified_checkpoint_round_receipt_members(
                        db, observed.source.round,
                    )
                    if classified.source.source != observed.source:
                        raise WorkflowAuthorityError(
                            "checkpoint round changed during classification"
                        )
                    advanced = advance_checkpoint_replay_boundary_round(
                        pending, observed.outcome,
                    )
                    result = ReviewCheckpointArchiveReplaySegment(
                        advance_review_checkpoint_archive_round(segment, observed.source),
                        ReviewCheckpointArchivePendingBoundary(
                            advanced.progress, replay.source, replay.through_sequence,
                        ),
                    )
            elif isinstance(replay, _CheckpointReplayBetweenBoundaries):
                result = ReviewCheckpointArchiveReplaySegment(
                    segment, start_checkpoint_replay_tail(
                        replay, following.boundary if following is not None else None,
                        checkpoint_following_fingerprint(following)
                        if following is not None else None,
                    ),
                )
            elif (isinstance(replay, _CheckpointReplayTail)
                  and replay.progress.next_round_sequence <= receipt.checkpoint.through_sequence):
                observed = read_checkpoint_round_by_sequence(
                    db, origin.checkpoint.state.binding, segment.next_round_sequence,
                )
                classified = read_classified_checkpoint_round_receipt_members(
                    db, observed.source.round,
                )
                if classified.source.source != observed.source:
                    raise WorkflowAuthorityError("checkpoint round changed during classification")
                result = ReviewCheckpointArchiveReplaySegment(
                    advance_review_checkpoint_archive_round(segment, observed.source),
                    advance_checkpoint_replay_tail_round(replay, observed.outcome),
                )
            else:
                result = finish_review_checkpoint_archive_replay_segment(state, now, due_seconds)
        else:
            raise WorkflowAuthorityError("checkpoint audit has an invalid persisted phase")
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error, WorkflowAuthorityError) as exc:
        result = BlockedReviewCheckpointArchiveAudit(
            state, (str(exc) or "checkpoint archived source is unreadable")[:512], now,
        )
    return _persist_bounded_state(
        db, key.repository, audit_key, rowid, result, tip_id, origin_digest, now,
    )
