"""Behavioral coverage for bounded global lifecycle source pages."""

from contextlib import closing
from dataclasses import replace

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_lifecycle_archive import (
    read_review_lifecycle_effect_page, read_review_lifecycle_source_page,
)
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_lifecycle_archive import (
    BlockedReviewLifecycleSourcePage, ObservedReviewLifecycleEffectPage,
    ObservedReviewLifecycleSourcePage,
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


def test_effect_page_censuses_selected_receipts_across_unrelated_rows(store):
    with store.transaction() as db:
        _insert_lifecycle_command(db, "unrelated:effect", kind="other")
        _insert_lifecycle_effect(db, "unrelated:effect")
        _review_command(db, "review:first")
        _insert_lifecycle_effect(db, "review:first")
        _review_command(db, "review:second")
        _insert_lifecycle_effect(db, "review:second")
        first = read_review_lifecycle_effect_page(db, "o/r", limit=2)
        assert isinstance(first, ObservedReviewLifecycleEffectPage)
        assert first.scanned_rows == 2
        assert tuple(source.command_id for source in first.sources) == ("review:first",)
        assert first.has_more
        command = read_review_lifecycle_source_page(db, "o/r")
        assert isinstance(command, ObservedReviewLifecycleSourcePage)
        assert first.sources[0].receipt_fields == command.sources[0].receipt_fields
        assert first.sources[0].command_rowid == command.sources[0].source_rowid
        _review_command(db, "review:newer")
        _insert_lifecycle_effect(db, "review:newer")
        second = read_review_lifecycle_effect_page(
            db, "o/r", after_rowid=first.next_rowid,
            upper_rowid=first.upper_rowid, limit=2,
        )
        assert isinstance(second, ObservedReviewLifecycleEffectPage)
        assert tuple(source.command_id for source in second.sources) == ("review:second",)
        assert not second.has_more
        assert second.lifecycle_source_version > first.lifecycle_source_version


def test_effect_page_variant_rejects_foreign_source_attribution(store):
    with store.transaction() as db:
        _review_command(db, "review:source")
        _insert_lifecycle_effect(db, "review:source")
        page = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(page, ObservedReviewLifecycleEffectPage)
        foreign = replace(page.sources[0], repository="other/repo")
        with pytest.raises(ValueError, match="typed sources"):
            replace(page, sources=(foreign,))


def test_effect_page_advances_with_no_selected_rows_and_includes_signed_rowids(store):
    with store.transaction() as db:
        _insert_lifecycle_command(db, "unrelated:first", kind="other")
        _insert_lifecycle_effect(db, "unrelated:first")
        _insert_lifecycle_command(db, "unrelated:second", kind="other")
        _insert_lifecycle_effect(db, "unrelated:second")
        _review_command(db, "review:negative")
        _insert_lifecycle_effect(db, "review:negative")
        _review_command(db, "review:zero")
        _insert_lifecycle_effect(db, "review:zero")
        db.execute("UPDATE effect_receipts SET rowid=-1 WHERE command_id='review:negative'")
        db.execute("UPDATE effect_receipts SET rowid=0 WHERE command_id='review:zero'")
        first = read_review_lifecycle_effect_page(db, "o/r", limit=2)
        assert isinstance(first, ObservedReviewLifecycleEffectPage)
        assert tuple(source.source_rowid for source in first.sources) == (-1, 0)
        second = read_review_lifecycle_effect_page(
            db, "o/r", after_rowid=first.next_rowid,
            upper_rowid=first.upper_rowid, limit=2,
        )
        assert isinstance(second, ObservedReviewLifecycleEffectPage)
        assert second.sources == ()
        assert second.scanned_rows == 2
        assert not second.has_more


def test_effect_page_blocks_orphaned_receipt_even_when_unrelated(store):
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        _insert_lifecycle_effect(db, "orphan:effect")
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "no matching command" in result.reason
        db.rollback()


def test_effect_page_blocks_oversized_identity_before_parent_lookup(store):
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        _insert_lifecycle_effect(db, "z" * 100_000)
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "oversized command identity" in result.reason
        db.rollback()


def test_effect_page_blocks_invalid_utf8_text_identity(store):
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        db.execute(
            "INSERT INTO effect_receipts(command_id,executor_identity,started_at,"
            "completed_at,status,receipt_digest) VALUES (CAST(X'FF' AS TEXT),'fixture',"
            "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00',"
            "'succeeded',?)",
            ("b" * 64,),
        )
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "identity is unreadable" in result.reason
        db.rollback()


def test_effect_page_blocks_blob_command_identity(store):
    with store.transaction() as db:
        _review_command(db, "review:alias")
        _insert_lifecycle_effect(db, "review:alias")
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        db.execute(
            "UPDATE effect_receipts SET command_id=CAST(command_id AS BLOB) "
            "WHERE command_id='review:alias'"
        )
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "non-text command identity" in result.reason
        db.rollback()


@pytest.mark.parametrize("column", ("repository", "kind"))
def test_effect_page_blocks_selected_parent_blob_alias(store, column):
    with store.transaction() as db:
        _review_command(db, "review:alias")
        _insert_lifecycle_effect(db, "review:alias")
        db.execute(
            f"UPDATE lifecycle_commands SET {column}=CAST({column} AS BLOB) "
            "WHERE command_id='review:alias'"
        )
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert "BLOB alias" in result.reason


@pytest.mark.parametrize("field,value,reason", (
    ("remote_revision", b"bad", "unreadable"),
    ("error", "x" * 65_537, "field bound"),
))
def test_effect_page_blocks_malformed_or_oversized_selected_source(store, field, value, reason):
    with store.transaction() as db:
        _review_command(db, "review:invalid")
        _insert_lifecycle_effect(db, "review:invalid")
        db.execute(
            f"UPDATE effect_receipts SET {field}=? WHERE command_id='review:invalid'",
            (value,),
        )
        result = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(result, BlockedReviewLifecycleSourcePage)
        assert reason in result.reason


def test_effect_page_splits_at_exact_source_byte_budget(store):
    with store.transaction() as db:
        _review_command(db, "review:first")
        _insert_lifecycle_effect(db, "review:first")
        _review_command(db, "review:second")
        _insert_lifecycle_effect(db, "review:second")
        full = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(full, ObservedReviewLifecycleEffectPage)
        first_size = read_review_lifecycle_effect_page(db, "o/r", limit=1).source_bytes
        exact = read_review_lifecycle_effect_page(db, "o/r", max_source_bytes=first_size)
        assert isinstance(exact, ObservedReviewLifecycleEffectPage)
        assert len(exact.sources) == 1
        assert exact.source_bytes == first_size
        assert exact.has_more
        assert isinstance(
            read_review_lifecycle_effect_page(db, "o/r", max_source_bytes=first_size - 1),
            BlockedReviewLifecycleSourcePage,
        )
        tail = read_review_lifecycle_effect_page(
            db, "o/r", after_rowid=exact.next_rowid, upper_rowid=exact.upper_rowid,
        )
        assert isinstance(tail, ObservedReviewLifecycleEffectPage)
        assert tail.source_bytes + exact.source_bytes == full.source_bytes


def test_effect_page_requires_transaction_and_complete_witness(store):
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_review_lifecycle_effect_page(db, "o/r")
    with store.transaction() as db:
        db.execute("DELETE FROM review_archive_source_versions WHERE source='lifecycle_rows'")
        with pytest.raises(WorkflowAuthorityError, match="lifecycle row mutation witness"):
            read_review_lifecycle_effect_page(db, "o/r")


def test_effect_page_reports_empty_source_table(store):
    with store.transaction() as db:
        page = read_review_lifecycle_effect_page(db, "o/r")
        assert isinstance(page, ObservedReviewLifecycleEffectPage)
        assert page.sources == ()
        assert page.upper_rowid is None
        assert page.next_rowid is None
        assert not page.has_more


@pytest.mark.parametrize("after,upper", ((1, None), (2, 1), (True, 1)))
def test_effect_page_rejects_invalid_cursor(store, after, upper):
    with store.transaction() as db:
        with pytest.raises(ValueError, match="bounded cursor"):
            read_review_lifecycle_effect_page(db, "o/r", after_rowid=after, upper_rowid=upper)
