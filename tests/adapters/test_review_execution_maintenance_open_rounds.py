"""Maintenance preserves allocation identity while open round sources settle."""

import pytest

from issue_agent.adapters.workflow_rows import load_review_workflow_row
from issue_agent.domain.review_execution_compaction_maintenance import (
    BlockedReviewExecutionMaintenance, PendingReviewExecutionMaintenance,
    ReadyReviewExecutionMaintenance,
)
from tests.adapters.test_review_execution_archive_admission_audit import _next_selection
from tests.adapters.test_review_execution_archive_bootstrap import (
    _abandoned_source, _complete, _copy_source,
)
from tests.adapters.test_review_execution_compaction_maintenance import _ready
from tests.adapters.test_review_execution_control import OWNER, rig


@pytest.mark.parametrize("already_compacted", (False, True))
def test_open_round_finalization_after_maintenance_still_publishes_checkpoint(rig, already_compacted):
    store, _, _, selection, control = rig
    key = selection.expected.fence.key
    if already_compacted:
        _copy_source(rig)
        _ready(store, key, store.now(), round_threshold=1)
        selection = _next_selection(rig, store, observed_at=store.now())
    plan = control.preflight(selection)
    for _ in range(128):
        result = control.maintain_archive(key, round_threshold=1)
        if isinstance(result, PendingReviewExecutionMaintenance):
            continue
        assert isinstance(result, BlockedReviewExecutionMaintenance), result
        assert "open prefix" in result.reason
        break
    else:
        raise AssertionError("open round maintenance did not reach its capacity refusal")
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    control.finalize_abandoned_round(plan.round.round_id)
    ready, _ = _ready(store, key, store.now(), round_threshold=1)
    assert isinstance(ready, ReadyReviewExecutionMaintenance)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint.through_sequence == plan.round.sequence
        assert after.execution.checkpoint_receipt is not None


@pytest.mark.parametrize("column", ("outcome_json", "planning_json", "closure_json"))
def test_prior_finalized_round_payload_change_still_blocks_initial_publication(rig, column):
    store, key, plan, now = _abandoned_source(rig)
    _complete(store, key, now)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        db.execute(
            f"UPDATE review_execution_rounds SET {column}={column} || ' ' WHERE round_id=?",
            (plan.round.round_id,),
        )
    for _ in range(128):
        result = rig[4].maintain_archive(key, round_threshold=1)
        if isinstance(result, PendingReviewExecutionMaintenance):
            continue
        assert isinstance(result, BlockedReviewExecutionMaintenance), result
        break
    else:
        raise AssertionError("changed finalized payload was not refused")
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after == before
        assert after.execution.checkpoint_receipt is None
