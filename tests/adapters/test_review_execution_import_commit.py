"""Atomic publication of a complete zero-history execution import."""

from dataclasses import replace
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
from issue_agent.domain.review_execution import CurrentReviewExecution
from issue_agent.domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
from issue_agent.domain.review_execution_history_import import ReviewImportNoHistoryObserved
from issue_agent.domain.review_execution_import_plan import (
    EligibleZeroHistoryReviewImport, ReviewExecutionImportPlan,
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
    plan = ReviewExecutionImportPlan(
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
            commit_module.commit_review_execution_import(db, plan, now=NOW)
    assert calls == 2
    assert _dump(store) == before

    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", real_insert)
    with store.transaction() as db:
        committed = commit_module.commit_review_execution_import(db, plan, now=NOW)
        assert tuple(load_review_workflow_row(db, item.workflow.revision.fence.key)
                     for item in plan.candidates) == committed
        assert all(snapshot.revision.version == 1 and isinstance(snapshot.execution, CurrentReviewExecution)
                   for snapshot in committed)
        assert db.execute("SELECT COUNT(*) FROM workflow_activation_imports WHERE family='review_execution'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM workflow_components WHERE family='review_execution'").fetchone()[0] == 2
    assert _dump(store) != before


def test_import_requires_transaction_and_current_workflow(tmp_path):
    store, plan = _rig(tmp_path)
    with sqlite3.connect(store.path) as db, pytest.raises(ValueError, match="open SQL transaction"):
        commit_module.commit_review_execution_import(db, plan, now=NOW)
    before = _dump(store)
    with pytest.raises(ValueError, match="fresh current evidence"):
        with store.transaction() as db:
            commit_module.commit_review_execution_import(db, plan, now=NOW + timedelta(hours=1))
    assert _dump(store) == before
    with store.transaction() as db:
        db.execute("UPDATE workflows SET version=1 WHERE repository=? AND target_kind='pr' AND target_number=10",
                   (REPOSITORY,))
        with pytest.raises(WorkflowAuthorityError, match="workflow changed"):
            commit_module.commit_review_execution_import(db, plan, now=NOW)
    assert _dump(store) != before
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_authority_epochs WHERE family='review_execution'").fetchone()[0] == 0


def _mixed(plan):
    from issue_agent.domain.review_execution_history_import import (
        ReviewImportHistoryBlocked, ReviewImportBlocker, ReviewImportBlockerKind, ReviewImportSource,
    )
    history = ReviewImportHistoryBlocked(9, (ReviewImportBlocker(
        ReviewImportBlockerKind.ORIGIN, (ReviewImportSource("session", "review:pr:9", "f" * 64),),
    ),))
    return replace(plan, candidates=(history, plan.candidates[1]))


def test_mixed_import_retains_blocked_authority_and_only_eligible_origin(tmp_path):
    from issue_agent.domain.review_execution import BlockedReviewExecutionMigration
    store, original = _rig(tmp_path)
    plan = _mixed(original)
    with store.transaction() as db:
        committed = commit_module.commit_review_execution_import(db, plan, now=NOW)
        blocked, eligible = committed
        assert isinstance(blocked.execution, BlockedReviewExecutionMigration)
        assert isinstance(eligible.execution, CurrentReviewExecution)
        assert blocked.execution.history == plan.candidates[0]
        assert tuple(load_review_workflow_row(db, WorkflowKey(REPOSITORY, PullRequestRef(pr)))
                     for pr in (9, 10)) == committed
        assert db.execute("SELECT target_number FROM workflow_components WHERE family='review_execution'").fetchall()[0][0] == 10
        assert db.execute("SELECT COUNT(*) FROM workflow_activation_imports WHERE family='review_execution'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0


def test_caught_failure_of_mixed_import_cannot_commit_partial_epoch(tmp_path, monkeypatch):
    store, original = _rig(tmp_path)
    plan = _mixed(original)
    before = _dump(store)
    real_insert = commit_module.insert_imported_review_execution_origin
    def failed_origin(*args, **kwargs):
        raise RuntimeError("failure after the blocked target write")
    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", failed_origin)
    with store.transaction() as db:
        with pytest.raises(RuntimeError, match="after the blocked"):
            commit_module.commit_review_execution_import(db, plan, now=NOW)
    assert _dump(store) == before
    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", real_insert)
    with store.transaction() as db:
        assert len(commit_module.commit_review_execution_import(db, plan, now=NOW)) == 2


def test_blocked_target_cannot_reserve_generic_paid_review(tmp_path):
    import json
    from tests.adapters.test_control_store import _request
    store, original = _rig(tmp_path)
    with store.transaction() as db:
        commit_module.commit_review_execution_import(db, _mixed(original), now=NOW)
        blocked = load_review_workflow_row(db, WorkflowKey(REPOSITORY, PullRequestRef(9)))
    request = replace(_request(1), kind="REVIEW", owner_id="o/r:review", workflow=blocked.binding,
                      dispatch_id=json.dumps({"repository": REPOSITORY, "number": 9, "kind": "REVIEW", "role": "review"}))
    result = store.reserve(request)
    assert not result.admitted and "migration is blocked" in result.detail
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


def test_blocked_target_refuses_fresh_submission_but_keeps_terminal_exact_retry(tmp_path):
    from issue_agent.adapters.control_store import ControlStoreError
    from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
    from issue_agent.domain.contracts import LifecycleCommand, CommandKind, CommandId, EffectReceipt, EffectStatus
    store, original = _rig(tmp_path)
    with store.transaction() as db:
        prior = load_review_workflow_row(db, WorkflowKey(REPOSITORY, PullRequestRef(9)))
    op = BoundReviewSubmissionOp(9, "APPROVE", "review body", HEAD, "review-bot[bot]", "d" * 64, None)
    command = LifecycleCommand(CommandId("prior-verdict"), "prior-verdict", CommandKind.SUBMIT_REVIEW,
                               "pr:9", HEAD, op.payload(), "config-v1", REPOSITORY, "o/r:review", prior.binding)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, command.executor_identity, NOW, NOW,
                                             EffectStatus.SUCCEEDED, remote_object_id="old-verdict"))
    with store.transaction() as db:
        commit_module.commit_review_execution_import(db, _mixed(original), now=NOW)
    before = _dump(store)
    assert store.enqueue_command(command).state == "succeeded"
    assert _dump(store) == before
    with pytest.raises(ControlStoreError, match="migration is blocked"):
        store.enqueue_command(replace(command, command_id=CommandId("new-verdict"), idempotency_key="new-verdict"))
    with pytest.raises(ControlStoreError, match="migration is blocked"):
        store.enqueue_command_occurrence(command)
    assert _dump(store) == before


@pytest.mark.parametrize("api", ("process", "lease"))
def test_blocked_target_refuses_fresh_generic_launch_without_changing_existing_run(tmp_path, api):
    import os
    from issue_agent.adapters.control_store import ControlStoreError, workflow_episode_key
    from issue_agent.adapters.process_supervisor import ProcessSupervisor
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.contracts import RunId
    store, original = _rig(tmp_path)
    with store.transaction() as db:
        commit_module.commit_review_execution_import(db, _mixed(original), now=NOW)
        blocked = load_review_workflow_row(db, WorkflowKey(REPOSITORY, PullRequestRef(9)))
    run = RunId("retained-unlaunched")
    store.start_run(run, "prior-dispatch", "REVIEW", "o/r:review", "cfg", state="reserved")
    with store.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=?,workflow_episode_key=? WHERE run_id=?",
                   (encode_workflow(blocked.binding), workflow_episode_key(blocked.binding), str(run)))
    identity = ProcessSupervisor.process_identity(os.getpid())
    assert identity is not None
    before = _dump(store)
    with pytest.raises(ControlStoreError, match="migration is blocked"):
        if api == "process":
            store.register_process_lease(run, expected_owner_id="o/r:review", pid=os.getpid(), pgid=os.getpgrp(),
                                         process_start_ticks=identity.start_ticks)
        else:
            from issue_agent.adapters.control_store import Lease
            store.register_lease(Lease(run, "prior-dispatch", "o/r:review", os.getpid(), os.getpgrp(), NOW,
                                      identity.start_ticks))
    assert _dump(store) == before
