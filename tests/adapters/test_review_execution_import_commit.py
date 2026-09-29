"""Atomic publication of a complete zero-history execution import."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import issue_agent.adapters.review_execution_import_commit as commit_module
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, insert_workflow_component,
    load_review_workflow_row,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution import ActivatedReviewExecution
from issue_agent.domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
from issue_agent.domain.review_execution_history_import import ReviewImportNoHistoryObserved
from issue_agent.domain.review_execution_import_plan import (
    EligibleZeroHistoryReviewImport, EmptyReviewExecutionImportPlan,
    screen_zero_history_review_import,
)
from issue_agent.domain.review_workflow import (
    CompleteReviews, ObservedPullRequest, PullRequestState, RecordedPullRequest,
)
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey,
)


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
REPOSITORY = "o/r"
HEAD = "a" * 40


def _rig(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    authority = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
    candidates = []
    with store.transaction() as db:
        db.execute(
            "INSERT INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)",
            (REPOSITORY, authority.family.value, authority.epoch, "review-source", "review-inventory", NOW.isoformat()),
        )
        for pr in (9, 10):
            key = WorkflowKey(REPOSITORY, PullRequestRef(pr))
            workflow = initial_review_workflow(key, authority)
            db.execute(
                "INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)",
                (REPOSITORY, pr, encode_workflow_episodes(workflow)),
            )
            insert_workflow_component(db, key, authority, workflow.episode.episode_id)
            recorded = RecordedPullRequest(ObservedPullRequest(
                key, HEAD, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
                CompleteReviews(()), NOW,
            ))
            candidate = screen_zero_history_review_import(
                ReviewImportNoHistoryObserved(pr, "b" * 64), workflow, recorded,
                LegacyOpenPRCounterLink(pr, HEAD, f"work/{pr}", None, None),
                "review-bot[bot]", now=NOW,
            )
            assert isinstance(candidate, EligibleZeroHistoryReviewImport)
            candidates.append(candidate)
    plan = EmptyReviewExecutionImportPlan(
        REPOSITORY, OpenPullRequestsListed((9, 10)), 2,
        "b" * 64, "c" * 64, "d" * 64, tuple(candidates), NOW,
    )
    return store, plan


def _dump(store):
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def test_two_pr_import_rolls_back_every_authority_row_after_partial_write(tmp_path, monkeypatch):
    store, plan = _rig(tmp_path)
    before = _dump(store)
    real_insert = commit_module.insert_imported_review_execution_origin
    calls = 0

    def fail_second_origin(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second PR origin failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", fail_second_origin)
    with pytest.raises(RuntimeError, match="injected second PR"):
        with store.transaction() as db:
            commit_module.commit_empty_review_execution_import(db, plan, now=NOW)
    assert calls == 2
    assert _dump(store) == before

    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", real_insert)
    with store.transaction() as db:
        committed = commit_module.commit_empty_review_execution_import(db, plan, now=NOW)
        assert tuple(load_review_workflow_row(db, item.workflow.revision.fence.key)
                     for item in plan.candidates) == committed
        assert all(snapshot.revision.version == 1 and isinstance(snapshot.execution, ActivatedReviewExecution)
                   for snapshot in committed)
        assert db.execute("SELECT COUNT(*) FROM workflow_activation_imports WHERE family='review_execution'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM workflow_components WHERE family='review_execution'").fetchone()[0] == 2
    assert _dump(store) != before


def test_import_requires_transaction_and_current_workflow(tmp_path):
    store, plan = _rig(tmp_path)
    with sqlite3.connect(store.path) as db, pytest.raises(ValueError, match="open SQL transaction"):
        commit_module.commit_empty_review_execution_import(db, plan, now=NOW)
    before = _dump(store)
    with pytest.raises(ValueError, match="fresh current evidence"):
        with store.transaction() as db:
            commit_module.commit_empty_review_execution_import(db, plan, now=NOW + timedelta(hours=1))
    assert _dump(store) == before
    with store.transaction() as db:
        db.execute("UPDATE workflows SET version=1 WHERE repository=? AND target_kind='pr' AND target_number=10",
                   (REPOSITORY,))
        with pytest.raises(WorkflowAuthorityError, match="workflow changed"):
            commit_module.commit_empty_review_execution_import(db, plan, now=NOW)
    assert _dump(store) != before
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_authority_epochs WHERE family='review_execution'").fetchone()[0] == 0
