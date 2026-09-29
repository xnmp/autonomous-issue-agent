"""Bounded, read-only checkpoint predecessor observations; no audit authority."""

from __future__ import annotations

import sqlite3

from ..domain.review_checkpoint_chain import (
    MAX_REVIEW_CHECKPOINT_CHAIN_PAGE, ObservedReviewCheckpointChainPage,
    ObservedReviewCheckpointChainSource, ReviewCheckpointChainCursor,
)
from ..domain.review_execution import ActivatedReviewExecution
from ..domain.workflow_identity import WorkflowKey
from .review_execution_activation import _read_recording_source
from .review_round_archive_audit import _current_execution
from .workflow_rows import (
    REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, WorkflowAuthorityError,
    _read_checkpoint_receipt_row, _read_execution_origin,
    workflow_scope,
)


def read_review_checkpoint_chain_page(
    db: sqlite3.Connection, key: WorkflowKey, *,
    after: ReviewCheckpointChainCursor | None = None,
    limit: int = 1,
) -> ObservedReviewCheckpointChainPage:
    """Walk at most `limit` checkpoint receipts toward the execution origin.

    A cursor is caller-held observation. Its path from the current tip is not
    proven by this page; a persisted audit must own and revalidate that path.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or after is not None and not isinstance(after, ReviewCheckpointChainCursor)
            or type(limit) is not int
            or not 1 <= limit <= MAX_REVIEW_CHECKPOINT_CHAIN_PAGE):
        raise ValueError("checkpoint chain page requires a transaction and bounded inputs")
    scope = workflow_scope(key)
    if scope[1] != "pr":
        raise ValueError("checkpoint chain page requires a PR workflow")
    execution = _current_execution(db, key)
    if not isinstance(execution, ActivatedReviewExecution):
        raise WorkflowAuthorityError("checkpoint chain lacks active execution")
    tip_id = execution.checkpoint_receipt
    if tip_id is None:
        raise WorkflowAuthorityError("checkpoint chain lacks a compacted tip")
    origin, origin_claimed = _read_execution_origin(db, scope, execution)
    if after is None:
        cursor = ReviewCheckpointChainCursor(
            tip_id, execution.checkpoint, execution.origin, tip_id,
            execution.checkpoint, None,
        )
    else:
        if (after.tip_receipt_id != tip_id
                or after.tip_checkpoint != execution.checkpoint
                or after.origin != execution.origin):
            raise WorkflowAuthorityError("checkpoint chain cursor changed its current tip")
        cursor = after
    sources: list[ObservedReviewCheckpointChainSource] = []
    for _ in range(limit):
        receipt, claimed = _read_checkpoint_receipt_row(
            db, key.repository, cursor.next_receipt_id,
        )
        if (receipt.checkpoint != cursor.expected_checkpoint
                or receipt.origin != origin.origin
                or claimed < origin_claimed
                or cursor.newer_claimed_at is not None
                and claimed > cursor.newer_claimed_at):
            raise WorkflowAuthorityError("checkpoint chain has a changed predecessor")
        try:
            _, source_digest = _read_recording_source(
                db, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE,
                key.repository, cursor.next_receipt_id,
            )
        except (TypeError, ValueError, UnicodeError, sqlite3.Error) as exc:
            raise WorkflowAuthorityError("checkpoint chain source is unreadable") from exc
        sources.append(ObservedReviewCheckpointChainSource(
            cursor.next_receipt_id, receipt, claimed, source_digest,
        ))
        predecessor = receipt.previous_receipt_id
        if predecessor is None:
            if receipt.previous_checkpoint != origin.checkpoint:
                raise WorkflowAuthorityError("checkpoint chain lost its origin frontier")
            return ObservedReviewCheckpointChainPage(tuple(sources), None)
        cursor = ReviewCheckpointChainCursor(
            tip_id, execution.checkpoint, execution.origin, predecessor,
            receipt.previous_checkpoint, claimed,
        )
    return ObservedReviewCheckpointChainPage(tuple(sources), cursor)
