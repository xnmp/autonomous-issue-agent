"""Pure finalized-prefix receipt accounting and namespace reconciliation."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.review_execution import (
    ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewRoundIdentity,
)
from issue_agent.domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit, CompletedReviewRoundReceiptArchiveScan,
    EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST, EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST,
    ReviewRoundReceiptArchiveBaseline,
    ReviewRoundReceiptArchiveMember, ReviewRoundReceiptArchiveScan,
    ReviewRoundReceiptArchiveSource, ReviewRoundReceiptArchiveSweep,
    ReviewRoundReceiptSweepCursor, advance_review_round_receipt_archive_scan,
    advance_review_round_receipt_archive_sweep,
    begin_review_round_receipt_archive_scan, review_round_receipt_archive_overdue,
)
from tests.domain.test_review_execution import binding


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
ORIGIN = ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:receipt-audit")


def _begin(through, baseline=None):
    return begin_review_round_receipt_archive_scan(
        binding(), ORIGIN, "a" * 64, through, NOW, 3600, baseline,
    )


def _source(sequence, *, digest="b" * 64):
    round_id = ReviewRoundIdentity(binding(), f"round:{sequence}", sequence)
    members = tuple(ReviewRoundReceiptArchiveMember(
        round_id, "c" * 64, namespace, round_id.round_id, digest,
    ) for namespace in ("review-round-outcome", "review-round-preflight"))
    receipt_set = hashlib.sha256(json.dumps(
        [(item.namespace, item.key, item.source_sha256) for item in members],
        separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    return ReviewRoundReceiptArchiveSource(round_id, "c" * 64, receipt_set, members)


def test_expected_prefix_and_namespace_members_complete_exactly():
    first = advance_review_round_receipt_archive_scan(_begin(2), _source(1), NOW)
    assert isinstance(first, ReviewRoundReceiptArchiveScan)
    expected = advance_review_round_receipt_archive_scan(first, _source(2), NOW)
    assert isinstance(expected, CompletedReviewRoundReceiptArchiveScan)
    assert expected.receipt_count == 4
    assert expected.membership_xor != EMPTY_ROUND_RECEIPT_ARCHIVE_DIGEST
    stream = EMPTY_ROUND_RECEIPT_ARCHIVE_STREAM_DIGEST
    for sequence in (1, 2):
        source = _source(sequence)
        encoded = "\0".join((
            str(sequence), source.round.round_id,
            str(source.round.binding.rejection.fence.generation),
            source.round_source_sha256, source.receipt_set_sha256,
        )).encode()
        stream = hashlib.sha256(bytes.fromhex(stream) + encoded).hexdigest()
    assert expected.rolling_sha256 == stream
    ordered = sorted(
        (*_source(1).members, *_source(2).members),
        key=lambda member: (member.namespace, member.key),
    )
    upper = ReviewRoundReceiptSweepCursor(
        ordered[-1].namespace, "o/r", ordered[-1].key,
    )
    state = ReviewRoundReceiptArchiveSweep(expected)
    for index, member in enumerate(ordered):
        cursor = ReviewRoundReceiptSweepCursor(member.namespace, "o/r", member.key)
        state = advance_review_round_receipt_archive_sweep(
            state, (member,), cursor, upper, index < len(ordered) - 1,
            0, NOW, 3600,
        )
    completed = state
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    assert not review_round_receipt_archive_overdue(completed, NOW)
    assert review_round_receipt_archive_overdue(completed, completed.next_due_at)


def test_prior_finalized_prefix_change_blocks_even_when_frontier_grows():
    prior = advance_review_round_receipt_archive_scan(_begin(1), _source(1), NOW)
    assert isinstance(prior, CompletedReviewRoundReceiptArchiveScan)
    baseline = ReviewRoundReceiptArchiveBaseline(1, prior.rolling_sha256)
    changed = advance_review_round_receipt_archive_scan(
        _begin(2, baseline), _source(1, digest="d" * 64), NOW,
    )
    assert isinstance(changed, BlockedReviewRoundReceiptArchiveScan)
    assert "changed since its prior pass" in changed.reason
    assert review_round_receipt_archive_overdue(changed, NOW)


def test_namespace_sweep_blocks_missing_or_changed_members():
    expected = advance_review_round_receipt_archive_scan(_begin(1), _source(1), NOW)
    assert isinstance(expected, CompletedReviewRoundReceiptArchiveScan)
    first_cursor = ReviewRoundReceiptSweepCursor("review-round-outcome", "o/r", "round:1")
    cursor = ReviewRoundReceiptSweepCursor("review-round-preflight", "o/r", "round:1")
    missing = advance_review_round_receipt_archive_sweep(
        ReviewRoundReceiptArchiveSweep(expected), (_source(1).members[0],),
        cursor, cursor, False, 0, NOW, 3600,
    )
    assert isinstance(missing, BlockedReviewRoundReceiptArchiveSweep)
    assert "disagrees" in missing.reason
    changed = _source(1, digest="d" * 64).members
    state = advance_review_round_receipt_archive_sweep(
        ReviewRoundReceiptArchiveSweep(expected), changed[:1],
        first_cursor, cursor, True, 0, NOW, 3600,
    )
    assert isinstance(state, ReviewRoundReceiptArchiveSweep)
    mismatch = advance_review_round_receipt_archive_sweep(
        state, changed[1:], cursor, cursor, False, 0, NOW, 3600,
    )
    assert isinstance(mismatch, BlockedReviewRoundReceiptArchiveSweep)
    assert review_round_receipt_archive_overdue(mismatch, NOW)


def test_empty_finalized_prefix_accepts_only_pending_sources():
    empty = _begin(0)
    assert isinstance(empty, CompletedReviewRoundReceiptArchiveScan)
    cursor = ReviewRoundReceiptSweepCursor("review-round-preflight", "o/r", "round:1")
    completed = advance_review_round_receipt_archive_sweep(
        ReviewRoundReceiptArchiveSweep(empty), (), cursor, cursor,
        False, 1, NOW, 3600,
    )
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    assert completed.pending_count == 1 and completed.receipt_count == 0
    with pytest.raises(ValueError, match="outside its prefix"):
        advance_review_round_receipt_archive_sweep(
            ReviewRoundReceiptArchiveSweep(empty), (_source(1).members[0],),
            cursor, cursor, False, 0, NOW, 3600,
        )


def test_invalid_cursor_and_due_window_are_rejected():
    expected = advance_review_round_receipt_archive_scan(_begin(1), _source(1), NOW)
    assert isinstance(expected, CompletedReviewRoundReceiptArchiveScan)
    with pytest.raises(ValueError, match="ordered cursors"):
        ReviewRoundReceiptArchiveSweep(
            expected, ReviewRoundReceiptSweepCursor(
                "review-round-preflight", "o/r", "round:2",
            ), ReviewRoundReceiptSweepCursor(
                "review-round-preflight", "o/r", "round:1",
            ),
        )
    with pytest.raises(ValueError, match="bounded due window"):
        begin_review_round_receipt_archive_scan(
            binding(), ORIGIN, "a" * 64, 1, NOW, 0,
        )
    with pytest.raises(ValueError, match="unrepresentable"):
        begin_review_round_receipt_archive_scan(
            binding(), ORIGIN, "a" * 64, 1,
            datetime.max.replace(tzinfo=timezone.utc), 3600,
        )
    with pytest.raises(ValueError, match="selected namespace"):
        replace(_source(1).members[0], namespace="foreign")
    with pytest.raises(ValueError, match="disagrees with its receipt set"):
        replace(_source(1), receipt_set_sha256="d" * 64)
    due = expected.due_at
    assert review_round_receipt_archive_overdue(expected, due)
    assert not review_round_receipt_archive_overdue(expected, due - timedelta(microseconds=1))
