"""Pure round-row census accounting across bounded durable pages."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.review_execution import ReviewExecutionOrigin, ReviewExecutionOriginKind
from issue_agent.domain.review_round_archive import (
    BlockedReviewRoundArchiveScan, CompletedReviewRoundArchiveScan,
    EMPTY_ROUND_ARCHIVE_SET, ReviewRoundArchiveBaseline,
    ReviewRoundArchiveFingerprint, ReviewRoundArchiveScan,
    advance_review_round_archive_scan, begin_review_round_archive_scan,
    review_round_archive_scan_overdue,
)
from tests.domain.test_review_execution import binding


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
ORIGIN = ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:round-audit")


def _begin(through, *, baseline=None):
    return begin_review_round_archive_scan(
        binding(), ORIGIN, "a" * 64, through, NOW, 3600, baseline,
    )


def _source(rowid, sequence, digest="b" * 64):
    return ReviewRoundArchiveFingerprint(rowid, sequence, f"round:{sequence}", digest)


def test_round_archive_scan_completes_contiguous_membership_across_pages():
    initial = _begin(2)
    first = advance_review_round_archive_scan(
        initial, (_source(-7, 1),), -7, 10, True, NOW, 3600,
    )
    assert isinstance(first, ReviewRoundArchiveScan)
    assert first.after_rowid == -7 and first.source_count == 1
    completed = advance_review_round_archive_scan(
        first, (_source(10, 2),), 10, 10, False, NOW, 3600,
    )
    assert isinstance(completed, CompletedReviewRoundArchiveScan)
    assert completed.through_sequence == 2
    assert completed.membership_xor != EMPTY_ROUND_ARCHIVE_SET
    assert not review_round_archive_scan_overdue(completed, NOW)
    assert review_round_archive_scan_overdue(completed, completed.next_due_at)


@pytest.mark.parametrize("members,through,reason", [
    ((_source(1, 1),), 2, "lacks allocated"),
    ((_source(1, 2),), 1, "unclaimed"),
    ((_source(1, 1), _source(2, 1)), 2, "unclaimed"),
])
def test_round_archive_scan_blocks_missing_extra_or_repeated_members(members, through, reason):
    result = advance_review_round_archive_scan(
        _begin(through), members, members[-1].source_rowid, members[-1].source_rowid,
        False, NOW, 3600,
    )
    assert isinstance(result, BlockedReviewRoundArchiveScan)
    assert reason in result.reason
    assert review_round_archive_scan_overdue(result, NOW)


def test_round_archive_next_pass_compares_prior_membership_during_growth():
    baseline = advance_review_round_archive_scan(
        _begin(1), (_source(1, 1),), 1, 1, False, NOW, 3600,
    )
    assert isinstance(baseline, CompletedReviewRoundArchiveScan)
    prior = ReviewRoundArchiveBaseline(1, baseline.membership_xor)
    scan = _begin(2, baseline=prior)
    unchanged = advance_review_round_archive_scan(
        scan, (_source(1, 1), _source(2, 2)), 2, 2, False, NOW, 3600,
    )
    assert isinstance(unchanged, CompletedReviewRoundArchiveScan)
    changed = advance_review_round_archive_scan(
        scan, (_source(1, 1, "c" * 64), _source(2, 2)),
        2, 2, False, NOW, 3600,
    )
    assert isinstance(changed, BlockedReviewRoundArchiveScan)
    assert "changed since its prior pass" in changed.reason


def test_round_archive_empty_pass_and_deadline_are_explicit():
    complete = advance_review_round_archive_scan(
        _begin(0), (), None, None, False, NOW, 3600,
    )
    assert isinstance(complete, CompletedReviewRoundArchiveScan)
    assert complete.membership_xor == EMPTY_ROUND_ARCHIVE_SET
    overdue = begin_review_round_archive_scan(
        binding(), ORIGIN, "a" * 64, 0, NOW + timedelta(hours=2),
        3600, ReviewRoundArchiveBaseline(0, EMPTY_ROUND_ARCHIVE_SET),
        complete.next_due_at,
    )
    assert review_round_archive_scan_overdue(overdue, NOW + timedelta(hours=2))
    with pytest.raises(ValueError, match="fixed upper"):
        replace(_begin(1), after_rowid=2, upper_rowid=1)
    with pytest.raises(ValueError, match="SHA-256"):
        _source(1, 1, "not-a-digest")


def test_round_archive_missing_tail_uses_an_empty_final_page():
    first = advance_review_round_archive_scan(
        _begin(2), (_source(1, 1),), 1, 2, True, NOW, 3600,
    )
    assert isinstance(first, ReviewRoundArchiveScan)
    result = advance_review_round_archive_scan(
        first, (), 1, 2, False, NOW, 3600,
    )
    assert isinstance(result, BlockedReviewRoundArchiveScan)
    assert "lacks allocated" in result.reason
    with pytest.raises(ValueError, match="did not advance"):
        advance_review_round_archive_scan(
            first, (), 1, 2, True, NOW, 3600,
        )
