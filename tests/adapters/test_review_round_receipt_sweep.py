"""Indexed, bounded observations of retained review round receipt rows."""

import pytest

import issue_agent.adapters.review_round_receipt_sweep as sweep
from issue_agent.adapters.review_round_receipt_sweep import (
    BlockedReviewRoundReceiptSweepPage, ObservedReviewRoundReceiptSweepPage,
    ReviewRoundReceiptSweepCursor,
    read_review_round_receipt_sweep_page,
)
from tests.adapters.test_review_execution_persistence import active


def _insert(db, namespace, scope, key, *, payload="{}"):
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?,?,?,'2026-09-29T00:00:00+00:00',?)",
        (namespace, scope, key, payload),
    )


def test_round_receipt_sweep_resumes_fixed_upper_and_revisits_later_insert(active, monkeypatch):
    store, _ = active
    monkeypatch.setattr(sweep, "MAX_ROUND_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        _insert(db, "review-round-stage", "other/repo", "stage:2")
        first = read_review_round_receipt_sweep_page(db)
    assert isinstance(first, ObservedReviewRoundReceiptSweepPage)
    assert first.has_more and len(first.sources) == 1
    assert first.sources[0].cursor.key == "round:1"
    with store.transaction() as db:
        _insert(db, "review-round-stage", "other/repo", "stage:3")
        second = read_review_round_receipt_sweep_page(
            db, after=first.next_cursor, upper=first.upper_cursor,
        )
    assert isinstance(second, ObservedReviewRoundReceiptSweepPage)
    assert not second.has_more
    assert tuple(source.cursor.key for source in second.sources) == ("stage:2",)
    with store.transaction() as db:
        next_pass = read_review_round_receipt_sweep_page(db)
    assert isinstance(next_pass, ObservedReviewRoundReceiptSweepPage)
    assert next_pass.has_more
    with store.transaction() as db:
        later = read_review_round_receipt_sweep_page(
            db, after=next_pass.next_cursor, upper=next_pass.upper_cursor,
        )
    assert isinstance(later, ObservedReviewRoundReceiptSweepPage)
    assert later.sources[0].cursor.key == "stage:2" and later.has_more
    with store.transaction() as db:
        final = read_review_round_receipt_sweep_page(
            db, after=later.next_cursor, upper=later.upper_cursor,
        )
    assert isinstance(final, ObservedReviewRoundReceiptSweepPage)
    assert tuple(source.cursor.key for source in final.sources) == ("stage:3",)
    assert not final.has_more


def test_round_receipt_sweep_observes_payload_digest_change(active):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        before = read_review_round_receipt_sweep_page(db)
    assert isinstance(before, ObservedReviewRoundReceiptSweepPage)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{\"changed\":true}' "
            "WHERE namespace='review-round-outcome' AND key='round:1'",
        )
        after = read_review_round_receipt_sweep_page(db)
    assert isinstance(after, ObservedReviewRoundReceiptSweepPage)
    assert after.sources[0].source_sha256 != before.sources[0].source_sha256


@pytest.mark.parametrize("column", ["namespace", "scope", "key"])
def test_round_receipt_sweep_blocks_blob_identity_aliases(active, column):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        db.execute(
            f"UPDATE dedupe_keys SET {column}=CAST({column} AS BLOB) "
            "WHERE namespace='review-round-outcome' AND key='round:1'",
        )
        result = read_review_round_receipt_sweep_page(db)
    assert isinstance(result, BlockedReviewRoundReceiptSweepPage)
    assert result.source_rowid is not None


def test_round_receipt_sweep_blocks_oversized_source_without_loading_it(active):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1", payload="x" * (2**21 + 1))
        result = read_review_round_receipt_sweep_page(db)
    assert isinstance(result, BlockedReviewRoundReceiptSweepPage)
    assert "byte bound" in result.reason or "oversized" in result.reason


def test_round_receipt_sweep_requires_explicit_transaction(active):
    store, _ = active
    db = store._connect()
    try:
        with pytest.raises(ValueError, match="transaction"):
            read_review_round_receipt_sweep_page(db)
    finally:
        db.close()


def test_round_receipt_sweep_seeks_to_a_late_cursor(active):
    store, _ = active
    with store.transaction() as db:
        db.execute(
            "WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value<5000) "
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "SELECT 'review-round-stage','o/r',printf('stage:%06d',value),"
            "'2026-09-29T00:00:00+00:00','{}' FROM n",
        )
        calls = 0

        def count_steps():
            nonlocal calls
            calls += 1
            return 0

        db.set_progress_handler(count_steps, 100)
        try:
            page = read_review_round_receipt_sweep_page(
                db,
                after=ReviewRoundReceiptSweepCursor(
                    "review-round-stage", "o/r", "stage:004900",
                ),
                upper=ReviewRoundReceiptSweepCursor(
                    "review-round-stage", "o/r", "stage:005000",
                ),
            )
        finally:
            db.set_progress_handler(None, 0)
    assert isinstance(page, ObservedReviewRoundReceiptSweepPage)
    assert page.sources[0].cursor.key == "stage:004901" and page.has_more
    assert calls < 200
