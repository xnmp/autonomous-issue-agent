"""A retained review workflow census must not omit a PR or trust an alias."""

from dataclasses import replace
import sqlite3

from issue_agent.adapters import review_execution_workflow_census as census
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_workflow_census import (
    BlockedRetainedReviewWorkflows, CompleteRetainedReviewWorkflows,
    read_complete_retained_review_workflows,
)
from issue_agent.adapters.workflow_rows import encode_workflow_episodes, insert_workflow_component
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution import ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionBinding, ReviewExecutionState, ReviewExecutionCheckpoint, ActivatedReviewExecution
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey


def _store(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
            ("o/r", WorkflowFamily.REVIEW_REJECTIONS.value, 1, "a" * 64, "b" * 64, "2026-09-30T00:00:00+00:00"),
        )
    return store


def _add(db, number):
    key = WorkflowKey("o/r", PullRequestRef(number))
    snapshot = initial_review_workflow(key, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1))
    db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)", ("o/r", "pr", number, 0, 0,
                                                          encode_workflow_episodes(snapshot)))
    insert_workflow_component(db, key, snapshot.authority, snapshot.episode.episode_id)
    return snapshot


def test_census_returns_every_canonical_open_pr_and_is_read_only(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        first = _add(db, 7)
        second = _add(db, 8)
    with store.transaction() as db:
        before = db.total_changes
        result = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7, 8)))
        assert db.total_changes == before

    assert isinstance(result, CompleteRetainedReviewWorkflows)
    assert [item.snapshot for item in result.sources] == [first, second]
    assert len(result.source_digest) == 64


def test_missing_or_terminal_row_refuses_membership(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
    with store.transaction() as db:
        missing = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7, 8)))
        terminal = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed(()))

    assert isinstance(missing, BlockedRetainedReviewWorkflows)
    assert "missing=(8,)" in missing.reason
    assert isinstance(terminal, BlockedRetainedReviewWorkflows)
    assert "terminal_or_extra=(7,)" in terminal.reason


def test_blob_and_case_aliases_block_instead_of_being_skipped(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE workflows SET repository=CAST(repository AS BLOB) WHERE target_number=7")
    with store.transaction() as db:
        blob = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))
    assert isinstance(blob, BlockedRetainedReviewWorkflows)
    assert "alias" in blob.reason

    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE workflows SET repository='O/R' WHERE target_number=7")
    with store.transaction() as db:
        alias = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))
    assert isinstance(alias, BlockedRetainedReviewWorkflows)
    assert "alias" in alias.reason


def test_malformed_payload_and_bounds_refuse_before_snapshot_trust(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
    with store.transaction() as db:
        byte_bound = read_complete_retained_review_workflows(
            db, "o/r", OpenPullRequestsListed((7,)), max_bytes=1,
        )
        row_bound = read_complete_retained_review_workflows(
            db, "o/r", OpenPullRequestsListed((7,)), max_rows=1,
        )
    assert isinstance(byte_bound, BlockedRetainedReviewWorkflows)
    assert "byte bound" in byte_bound.reason
    assert isinstance(row_bound, CompleteRetainedReviewWorkflows)

    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}' WHERE target_number=7")
    with store.transaction() as db:
        malformed = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))
    assert isinstance(malformed, BlockedRetainedReviewWorkflows)
    assert "not canonical" in malformed.reason


def test_row_bound_and_workflow_version_change_are_observable(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
        _add(db, 8)
    with store.transaction() as db:
        bounded = read_complete_retained_review_workflows(
            db, "o/r", OpenPullRequestsListed((7, 8)), max_rows=1,
        )
        original = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7, 8)))
    assert isinstance(bounded, BlockedRetainedReviewWorkflows)
    assert "row count" in bounded.reason
    assert isinstance(original, CompleteRetainedReviewWorkflows)

    with store.transaction() as db:
        db.execute("UPDATE workflows SET version=version+1 WHERE target_number=7")
    with store.transaction() as db:
        changed = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7, 8)))
    assert isinstance(changed, CompleteRetainedReviewWorkflows)
    assert changed.source_digest != original.source_digest


def test_active_execution_row_refuses_preactivation_census(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        snapshot = _add(db, 7)
        binding = ReviewExecutionBinding(snapshot.binding, 1, "execution-episode")
        state = ReviewExecutionState(binding)
        execution = ActivatedReviewExecution(
            ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin-1"),
            ReviewExecutionCheckpoint(0, state), state, 0,
        )
        db.execute("UPDATE workflows SET episode_json=? WHERE target_number=7",
                   (encode_workflow_episodes(replace(snapshot, execution=execution)),))
    with store.transaction() as db:
        result = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))
    assert isinstance(result, BlockedRetainedReviewWorkflows)


def test_malformed_target_kind_cannot_disappear_from_pr_census(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE workflows SET target_kind='unknown' WHERE target_number=7")
    with store.transaction() as db:
        result = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))

    assert isinstance(result, BlockedRetainedReviewWorkflows)
    assert "malformed target kind" in result.reason


def test_unrelated_workflow_population_cannot_make_scope_scan_unbounded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    with store.transaction() as db:
        _add(db, 7)
        for number in (1, 2):
            db.execute(
                "INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                ("other/r", "issue", number, 0, 0, "[]"),
            )
    monkeypatch.setattr(census, "MAX_WORKFLOW_CENSUS_SCANNED_ROWS", 2)

    with store.transaction() as db:
        result = read_complete_retained_review_workflows(db, "o/r", OpenPullRequestsListed((7,)))

    assert isinstance(result, BlockedRetainedReviewWorkflows)
    assert "scope scan exceeds" in result.reason
