"""The first checkpoint gets source proof through bounded durable pages."""

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

import pytest

import issue_agent.adapters.review_execution_archive_bootstrap as bootstrap_module
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_archive_identity import advance_review_archive_identity_audit
from issue_agent.adapters.review_execution_archive_bootstrap import advance_review_execution_archive_bootstrap
from issue_agent.adapters.review_execution_checkpoint_commit import commit_review_execution_checkpoint
from issue_agent.adapters.review_round_receipt_archive_audit import advance_review_round_receipt_archive_audit
from issue_agent.adapters.sql_read_budget import MAX_SQL_READ_BYTES
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.review_archive_identity import CompletedReviewArchiveIdentities
from issue_agent.domain.review_execution_archive_bootstrap import (
    BlockedReviewExecutionArchiveBootstrap, PendingReviewExecutionArchiveBootstrap,
    ReviewExecutionArchiveBootstrapStage,
)
from issue_agent.domain.review_execution_archive_evidence import ObservedInitialReviewExecutionArchiveEvidence
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
from tests.adapters.test_review_execution_control import OWNER, dump, rig
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


def _copy_source(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    return store, selection.expected.fence.key, plan, store.now()


def _abandoned_source(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    control.finalize_abandoned_round(plan.round.round_id)
    return store, selection.expected.fence.key, plan, store.now()


def _advance(store, key, now, *, max_source_bytes=None):
    options = {} if max_source_bytes is None else {"max_source_bytes": max_source_bytes}
    with store.transaction() as db:
        return advance_review_execution_archive_bootstrap(db, key, now=now, **options)


def _complete(store, key, now):
    seen_pending = set()
    for _ in range(128):
        result = _advance(store, key, now)
        if isinstance(result, PendingReviewExecutionArchiveBootstrap):
            seen_pending.add(result.stage)
            continue
        assert isinstance(result, ObservedInitialReviewExecutionArchiveEvidence), result
        return result, seen_pending
    raise AssertionError("archive bootstrap did not finish")


@pytest.mark.parametrize("source", (_copy_source, _abandoned_source))
def test_bootstrap_from_first_page_enables_real_checkpoint(rig, source) -> None:
    store, key, plan, now = source(rig)
    first = _advance(store, key, now)
    assert isinstance(first, PendingReviewExecutionArchiveBootstrap)
    assert first.stage is ReviewExecutionArchiveBootstrapStage.IDENTITIES
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None and snapshot.execution.checkpoint_receipt is None
        assert db.execute(
            "SELECT COUNT(*) FROM level_state WHERE namespace='review-execution-archive-gate-v1'"
        ).fetchone()[0] == 0
    completed, stages = _complete(store, key, now)
    assert ReviewExecutionArchiveBootstrapStage.EPISODE_SOURCES in stages
    assert completed.execution.last_sequence == plan.round.sequence
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        committed = commit_review_execution_checkpoint(
            db, key, before.revision, plan.round.sequence, now=now,
        )
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint_receipt == committed.receipt_id
    assert isinstance(_advance(store, key, now), BlockedReviewExecutionArchiveBootstrap)


def test_pending_bootstrap_resumes_after_control_store_reopen(rig) -> None:
    store, key, plan, now = _copy_source(rig)
    assert isinstance(_advance(store, key, now), PendingReviewExecutionArchiveBootstrap)
    reopened = SQLiteControlStore(store.path, now=store.now)
    completed, _ = _complete(reopened, key, now)
    assert completed.execution.last_sequence == plan.round.sequence


def test_changed_round_source_refuses_without_checkpoint_or_certificate(rig) -> None:
    store, key, _, now = _copy_source(rig)
    _complete(store, key, now)
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET outcome_json='{}'")
    before = dump(store)
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewExecutionArchiveBootstrap)
    with store.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-checkpoint'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM level_state WHERE namespace='review-execution-archive-gate-v1'"
        ).fetchone()[0] == 0
    assert dump(store) == before


def test_expired_source_pass_cannot_complete_bootstrap(rig) -> None:
    store, key, _, now = _copy_source(rig)
    _complete(store, key, now)
    result = _advance(store, key, now + timedelta(days=1))
    assert not isinstance(result, ObservedInitialReviewExecutionArchiveEvidence)


def test_small_shared_budget_refuses_without_losing_round_or_authority(rig) -> None:
    store, key, _, now = _copy_source(rig)
    before = dump(store)
    result = _advance(store, key, now, max_source_bytes=1)
    assert isinstance(result, BlockedReviewExecutionArchiveBootstrap)
    assert dump(store) == before
    _complete(store, key, now)


def test_backwards_receipt_time_rolls_back_earlier_round_progress(rig) -> None:
    store, key, _, now = _copy_source(rig)
    for _ in range(64):
        with store.transaction() as db:
            identity = advance_review_archive_identity_audit(db, now=now)
        if isinstance(identity, CompletedReviewArchiveIdentities):
            break
    else:
        raise AssertionError("identity bootstrap did not complete")
    with store.transaction() as db:
        advance_review_round_receipt_archive_audit(db, key, now=now + timedelta(minutes=10))
    before = dump(store)
    with store.transaction() as db:
        result = advance_review_execution_archive_bootstrap(
            db, key, now=now + timedelta(minutes=5),
        )
        assert isinstance(result, BlockedReviewExecutionArchiveBootstrap)
        assert "backwards" in result.reason
    assert dump(store) == before


def test_unexpected_later_step_failure_rethrows_after_cursor_rollback(rig, monkeypatch) -> None:
    store, key, _, now = _copy_source(rig)
    for _ in range(64):
        with store.transaction() as db:
            identity = advance_review_archive_identity_audit(db, now=now)
        if isinstance(identity, CompletedReviewArchiveIdentities):
            break
    else:
        raise AssertionError("identity bootstrap did not complete")
    before = dump(store)

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("injected later step failure")

    monkeypatch.setattr(bootstrap_module, "advance_review_round_receipt_archive_audit", fail_receipt)
    with store.transaction() as db:
        with pytest.raises(RuntimeError, match="injected later step failure"):
            advance_review_execution_archive_bootstrap(db, key, now=now)
    assert dump(store) == before


@pytest.mark.parametrize("change", (
    "issue_key", "naive_time", "bool_due", "zero_due", "huge_due",
    "bool_budget", "zero_budget", "huge_budget",
))
def test_invalid_public_request_fails_before_any_cursor_write(rig, change: str) -> None:
    store, key, _, now = _copy_source(rig)
    options = {}
    if change == "issue_key":
        key = WorkflowKey(key.repository, IssueRef(9))
    elif change == "naive_time":
        now = datetime.now()
    elif change == "bool_due":
        options["due_seconds"] = True
    elif change == "zero_due":
        options["due_seconds"] = 0
    elif change == "huge_due":
        options["due_seconds"] = 86401
    elif change == "bool_budget":
        options["max_source_bytes"] = True
    elif change == "zero_budget":
        options["max_source_bytes"] = 0
    else:
        options["max_source_bytes"] = MAX_SQL_READ_BYTES + 1
    before = dump(store)
    with store.transaction() as db, pytest.raises(ValueError):
        advance_review_execution_archive_bootstrap(db, key, now=now, **options)
    assert dump(store) == before


def test_public_request_requires_an_open_transaction(rig) -> None:
    store, key, _, now = _copy_source(rig)
    before = dump(store)
    with sqlite3.connect(store.path) as db, pytest.raises(ValueError, match="transaction"):
        advance_review_execution_archive_bootstrap(db, key, now=now)
    assert dump(store) == before
