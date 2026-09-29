"""Behavioral coverage for bounded global lifecycle source pages."""

from contextlib import closing

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_lifecycle_archive import read_review_lifecycle_source_page
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_lifecycle_archive import (
    BlockedReviewLifecycleSourcePage, ObservedReviewLifecycleSourcePage,
)
from tests.adapters.test_review_archive_source_versions import (
    _insert_lifecycle_command, _insert_lifecycle_effect,
)


@pytest.fixture
def store(tmp_path):
    result = SQLiteControlStore(tmp_path / "control.db")
    result.initialize()
    return result


def _review_command(db, command_id):
    _insert_lifecycle_command(db, command_id)
    db.execute(
        "UPDATE lifecycle_commands SET repository='o/r' WHERE command_id=?",
        (command_id,),
    )


def test_global_lifecycle_page_advances_over_unrelated_rows_and_freezes_upper_edge(store):
    with store.transaction() as db:
        _insert_lifecycle_command(db, "unrelated:first", kind="other")
        _review_command(db, "review:one")
        _insert_lifecycle_command(db, "unrelated:second", kind="other")
        _review_command(db, "review:two")
        first = read_review_lifecycle_source_page(db, "o/r", limit=2)
        assert isinstance(first, ObservedReviewLifecycleSourcePage)
        assert first.scanned_rows == 2
        assert tuple(dict(source.command_fields)["command_id"] for source in first.sources) == (
            "review:one",
        )
        assert first.has_more
        _review_command(db, "review:newer")
        second = read_review_lifecycle_source_page(
            db, "o/r", after_rowid=first.next_rowid, upper_rowid=first.upper_rowid,
            limit=2,
        )
        assert isinstance(second, ObservedReviewLifecycleSourcePage)
        assert second.scanned_rows == 2
        assert tuple(dict(source.command_fields)["command_id"] for source in second.sources) == (
            "review:two",
        )
        assert not second.has_more
        assert second.lifecycle_source_version > first.lifecycle_source_version
        fresh = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(fresh, ObservedReviewLifecycleSourcePage)
        assert len(fresh.sources) == 3


def test_global_lifecycle_page_can_advance_without_a_matching_command(store):
    with store.transaction() as db:
        _insert_lifecycle_command(db, "unrelated:first", kind="other")
        _insert_lifecycle_command(db, "unrelated:second", kind="other")
        _review_command(db, "review:third")
        first = read_review_lifecycle_source_page(db, "o/r", limit=2)
        assert isinstance(first, ObservedReviewLifecycleSourcePage)
        assert first.sources == ()
        assert first.scanned_rows == 2
        assert first.has_more
        second = read_review_lifecycle_source_page(
            db, "o/r", after_rowid=first.next_rowid,
            upper_rowid=first.upper_rowid, limit=2,
        )
        assert isinstance(second, ObservedReviewLifecycleSourcePage)
        assert tuple(dict(source.command_fields)["command_id"] for source in second.sources) == (
            "review:third",
        )
        assert not second.has_more


def test_global_lifecycle_page_skips_large_unrelated_repository(store):
    with store.transaction() as db:
        _insert_lifecycle_command(db, "unrelated:large", kind="other")
        db.execute(
            "UPDATE lifecycle_commands SET repository=? "
            "WHERE command_id='unrelated:large'",
            ("z" * 100_000,),
        )
        _review_command(db, "review:next")
        page = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(page, ObservedReviewLifecycleSourcePage)
        assert page.scanned_rows == 2
        assert tuple(dict(source.command_fields)["command_id"] for source in page.sources) == (
            "review:next",
        )


def test_lifecycle_page_includes_negative_and_zero_rowids_and_effect_fields(store):
    with store.transaction() as db:
        _review_command(db, "review:negative")
        _review_command(db, "review:zero")
        db.execute("UPDATE lifecycle_commands SET rowid=-1 WHERE command_id='review:negative'")
        db.execute("UPDATE lifecycle_commands SET rowid=0 WHERE command_id='review:zero'")
        _insert_lifecycle_effect(db, "review:zero")
        page = read_review_lifecycle_source_page(db, "o/r", limit=1)
        assert isinstance(page, ObservedReviewLifecycleSourcePage)
        assert tuple(source.source_rowid for source in page.sources) == (-1,)
        assert page.sources[0].receipt_fields is None
        continuation = read_review_lifecycle_source_page(
            db, "o/r", after_rowid=page.next_rowid, upper_rowid=page.upper_rowid,
            limit=1,
        )
        assert isinstance(continuation, ObservedReviewLifecycleSourcePage)
        assert tuple(source.source_rowid for source in continuation.sources) == (0,)
        assert dict(continuation.sources[0].receipt_fields or ())["command_id"] == "review:zero"
        assert not continuation.has_more


def test_lifecycle_page_splits_before_exceeding_source_byte_budget(store):
    with store.transaction() as db:
        _review_command(db, "review:first")
        _review_command(db, "review:second")
        full = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(full, ObservedReviewLifecycleSourcePage)
        first_size = read_review_lifecycle_source_page(db, "o/r", limit=1).source_bytes
        exact = read_review_lifecycle_source_page(db, "o/r", max_source_bytes=first_size)
        assert isinstance(exact, ObservedReviewLifecycleSourcePage)
        assert len(exact.sources) == 1
        assert exact.source_bytes == first_size
        assert exact.has_more
        assert isinstance(
            read_review_lifecycle_source_page(db, "o/r", max_source_bytes=first_size - 1),
            BlockedReviewLifecycleSourcePage,
        )
        tail = read_review_lifecycle_source_page(
            db, "o/r", after_rowid=exact.next_rowid, upper_rowid=exact.upper_rowid,
        )
        assert isinstance(tail, ObservedReviewLifecycleSourcePage)
        assert len(tail.sources) == 1
        assert tail.source_bytes + exact.source_bytes == full.source_bytes


@pytest.mark.parametrize("column", ("repository", "kind"))
def test_lifecycle_page_blocks_selected_blob_aliases(store, column):
    with store.transaction() as db:
        _review_command(db, "review:alias")
        db.execute(
            f"UPDATE lifecycle_commands SET {column}=CAST({column} AS BLOB) "
            "WHERE command_id='review:alias'"
        )
        result = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "BLOB alias" in result.reason


def test_lifecycle_page_blocks_malformed_selected_source(store):
    with store.transaction() as db:
        _review_command(db, "review:malformed")
        db.execute(
            "UPDATE lifecycle_commands SET attempts='invalid' "
            "WHERE command_id='review:malformed'"
        )
        result = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "unreadable" in result.reason


def test_lifecycle_page_blocks_oversized_selected_field(store):
    with store.transaction() as db:
        _review_command(db, "review:oversized")
        db.execute(
            "UPDATE lifecycle_commands SET payload_json=? "
            "WHERE command_id='review:oversized'",
            ("x" * 65_537,),
        )
        result = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "field bound" in result.reason


def test_lifecycle_page_requires_transaction_and_complete_witness(store):
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_review_lifecycle_source_page(db, "o/r")
    with store.transaction() as db:
        db.execute("DELETE FROM review_archive_source_versions WHERE source='lifecycle_rows'")
        with pytest.raises(WorkflowAuthorityError, match="lifecycle row mutation witness"):
            read_review_lifecycle_source_page(db, "o/r")


@pytest.mark.parametrize("after,upper", ((1, None), (2, 1), (True, 1)))
def test_lifecycle_page_rejects_invalid_cursor(store, after, upper):
    with store.transaction() as db:
        with pytest.raises(ValueError, match="bounded cursor"):
            read_review_lifecycle_source_page(db, "o/r", after_rowid=after, upper_rowid=upper)
