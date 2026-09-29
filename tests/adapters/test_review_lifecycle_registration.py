"""Bounded registration sources for an expected lifecycle membership audit."""

from contextlib import closing
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.review_lifecycle_registration import (
    _round_source_sql_bytes, read_review_lifecycle_registration_link,
    read_review_lifecycle_registration_page,
)
from issue_agent.adapters.workflow_rows import (
    REVIEW_SUBMISSION_NAMESPACE, REVIEW_SUBMISSION_INDEX_NAMESPACE,
    WorkflowAuthorityError, review_submission_receipt_key,
)
from issue_agent.domain.review_lifecycle_registration import (
    BlockedReviewLifecycleRegistrationPage, ObservedReviewLifecycleRegistrationPage,
)
from issue_agent.domain.review_rounds import ReviewSubmissionKind
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from tests.adapters.test_review_execution_control import OWNER, current, rig
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_productive_settlement import confirmed_copy


NOW = datetime(2026, 9, 29, 0, tzinfo=timezone.utc)


def _pages(db, repository):
    after = upper = None
    for _ in range(64):
        page = read_review_lifecycle_registration_page(
            db, repository, after_rowid=after, upper_rowid=upper,
        )
        yield page
        if isinstance(page, BlockedReviewLifecycleRegistrationPage) or not page.has_more:
            return
        after, upper = page.next_rowid, page.upper_rowid
    raise AssertionError("registration scan did not complete")


def test_registration_page_advances_past_unrelated_global_rows(rig):
    store = rig[0]
    with store.transaction() as db:
        for index in range(65):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('unrelated','o/r',?,?, '{}')",
                (f"unrelated:{index}", NOW.isoformat()),
            )
    _, _, selection, _, plan, command = confirmed_copy(rig)
    with store.transaction() as db:
        before = db.total_changes
        pages = list(_pages(db, selection.expected.fence.key.repository))
        assert db.total_changes == before
    assert len(pages) >= 2
    assert all(isinstance(page, ObservedReviewLifecycleRegistrationPage) for page in pages)
    assert all(page.scanned_rows <= 64 for page in pages)
    sources = tuple(source for page in pages for source in page.sources)
    assert len(sources) == 1
    assert sources[0].registration.submission.command_id == command.command_id
    assert sources[0].registration.round == plan.round
    assert sources[0].source_bytes > 0


def test_registration_page_blocks_malformed_payload(rig):
    store, _, selection, _, _, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUBMISSION_NAMESPACE, repository, command.command_id),
        )
        blocked = next(page for page in _pages(db, repository)
                       if isinstance(page, BlockedReviewLifecycleRegistrationPage))
    assert "unreadable" in blocked.reason


def test_registration_page_blocks_selected_blob_aliases(rig):
    store = rig[0]
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (CAST(? AS BLOB),?,?,?, '{}')",
            (REVIEW_SUBMISSION_NAMESPACE, "o/r", "alias:namespace", NOW.isoformat()),
        )
        blocked = next(page for page in _pages(db, "o/r")
                       if isinstance(page, BlockedReviewLifecycleRegistrationPage))
    assert "namespace has a BLOB alias" in blocked.reason

    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE key='alias:namespace'"
        )
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (?, CAST(? AS BLOB),?,?, '{}')",
            (REVIEW_SUBMISSION_NAMESPACE, "o/r", "alias:scope", NOW.isoformat()),
        )
        blocked = next(page for page in _pages(db, "o/r")
                       if isinstance(page, BlockedReviewLifecycleRegistrationPage))
    assert "repository has a BLOB alias" in blocked.reason


def test_registration_page_ignores_foreign_repository_rows(rig):
    store = rig[0]
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (?, 'foreign/repo', 'foreign:registration', ?, '{}')",
            (REVIEW_SUBMISSION_NAMESPACE, NOW.isoformat()),
        )
        pages = list(_pages(db, "o/r"))
    assert all(isinstance(page, ObservedReviewLifecycleRegistrationPage) for page in pages)
    assert not any(page.sources for page in pages)


def test_registration_page_enforces_selected_source_byte_budget(rig):
    store, _, selection, _, _, _ = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        sources = tuple(source for page in _pages(db, repository)
                        if isinstance(page, ObservedReviewLifecycleRegistrationPage)
                        for source in page.sources)
        assert len(sources) == 1
        blocked = next(page for page in _pages_with_budget(db, repository, sources[0].source_bytes - 1)
                       if isinstance(page, BlockedReviewLifecycleRegistrationPage))
        accepted = tuple(source for page in _pages_with_budget(db, repository, sources[0].source_bytes)
                         if isinstance(page, ObservedReviewLifecycleRegistrationPage)
                         for source in page.sources)
    assert "byte bound" in blocked.reason
    assert accepted == sources


def _pages_with_budget(db, repository, budget):
    after = upper = None
    for _ in range(64):
        page = read_review_lifecycle_registration_page(
            db, repository, after_rowid=after, upper_rowid=upper,
            max_source_bytes=budget,
        )
        yield page
        if isinstance(page, BlockedReviewLifecycleRegistrationPage) or not page.has_more:
            return
        after, upper = page.next_rowid, page.upper_rowid
    raise AssertionError("registration budget scan did not complete")


def _registration_source(db, repository):
    sources = tuple(source for page in _pages(db, repository)
                    if isinstance(page, ObservedReviewLifecycleRegistrationPage)
                    for source in page.sources)
    assert len(sources) == 1
    return sources[0]


def test_registration_link_rechecks_real_command_round_and_recorded_sources(rig):
    store, _, selection, _, plan, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
        before = db.total_changes
        linked = read_review_lifecycle_registration_link(db, source)
        assert db.total_changes == before
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(
                db, source, max_source_bytes=linked.source_bytes - 1,
            )
    assert linked.command_source.source_rowid > 0
    assert dict(linked.command_source.command_fields)["command_id"] == command.command_id
    assert dict(linked.round_source.fields)["round_id"] == plan.round.round_id


def test_registration_link_refuses_missing_command(rig):
    store, _, selection, _, plan, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        db.execute("DELETE FROM lifecycle_commands WHERE command_id=?", (command.command_id,))
        db.commit()
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_refuses_changed_round_index(rig):
    store, _, selection, _, plan, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUBMISSION_INDEX_NAMESPACE, repository,
             review_submission_receipt_key(plan.round.round_id, command.command_id)),
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_refuses_changed_registration(rig):
    store, _, selection, _, _, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-29T01:00:00+00:00' "
            "WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUBMISSION_NAMESPACE, repository, command.command_id),
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_refuses_changed_command(rig):
    store, _, selection, _, _, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
        db.execute(
            "UPDATE lifecycle_commands SET expected_revision='wrong-head' WHERE command_id=?",
            (command.command_id,),
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_refuses_replaced_registration_with_identical_content(rig):
    store, _, selection, _, _, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        source = _registration_source(db, repository)
        row = db.execute(
            "SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
            "FROM dedupe_keys WHERE rowid=?", (source.source_rowid,),
        ).fetchone()
        db.execute("DELETE FROM dedupe_keys WHERE rowid=?", (source.source_rowid,))
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
            "VALUES (?,?,?,?,?,?)", tuple(row),
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_requires_paid_stage_receipts(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    with store.transaction() as db:
        source = _registration_source(db, selection.expected.fence.key.repository)
        linked = read_review_lifecycle_registration_link(db, source)
        assert linked.stage_source_sha256
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='review-round-stage' AND scope='o/r'",
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_registration_link_refuses_forged_copy_refusal_effect(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.PERMANENT_FAILURE, error="head changed"))
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    with store.transaction() as db:
        source = _registration_source(db, selection.expected.fence.key.repository)
        assert read_review_lifecycle_registration_link(db, source).stage_source_sha256
        db.execute(
            "UPDATE effect_receipts SET receipt_digest=? WHERE command_id=?",
            ("0" * 64, command.command_id),
        )
        with pytest.raises(WorkflowAuthorityError, match="cannot be linked"):
            read_review_lifecycle_registration_link(db, source)


def test_round_source_byte_charge_matches_sqlite_large_integer_and_unicode(rig):
    store = rig[0]
    fields = (("sequence", 9223372036854775807), ("label", "é"), ("absent", None))
    with store.transaction() as db:
        expected = db.execute(
            "SELECT ifnull(length(CAST(? AS BLOB)),0) + "
            "ifnull(length(CAST(? AS BLOB)),0) + "
            "ifnull(length(CAST(? AS BLOB)),0)",
            tuple(value for _, value in fields),
        ).fetchone()[0]
    assert _round_source_sql_bytes(fields) == expected
