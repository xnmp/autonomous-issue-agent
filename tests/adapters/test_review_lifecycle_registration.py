"""Bounded registration sources for an expected lifecycle membership audit."""

from datetime import datetime, timezone

from issue_agent.adapters.review_lifecycle_registration import read_review_lifecycle_registration_page
from issue_agent.adapters.workflow_rows import REVIEW_SUBMISSION_NAMESPACE
from issue_agent.domain.review_lifecycle_registration import (
    BlockedReviewLifecycleRegistrationPage, ObservedReviewLifecycleRegistrationPage,
)
from tests.adapters.test_review_execution_control import rig
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
