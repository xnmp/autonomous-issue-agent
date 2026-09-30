"""Pre-proposal maintenance compacts only source-proved finalized frontiers."""

from __future__ import annotations

from dataclasses import replace
import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_compaction_maintenance import (
    advance_review_execution_compaction_maintenance,
)
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowRevisionConflict, load_review_workflow_row
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.labels import Label
from issue_agent.domain.review_rearm import ReleaseStatus
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.workflow import WorkflowCommitted
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.review_execution_compaction_maintenance import (
    BlockedReviewExecutionMaintenance, PendingReviewExecutionMaintenance,
    ReadyReviewExecutionMaintenance, ReviewExecutionMaintenanceStage,
)
from tests.adapters.test_review_execution_archive_bootstrap import _abandoned_source, _copy_source
from tests.adapters.test_review_execution_archive_admission_audit import _next_selection
from tests.adapters.test_review_execution_checkpoint_frontiers import (
    _ceiling_checkpoint, activated_rig,
)
from tests.adapters.test_review_execution_checkpoint_commit import _paid_source
from tests.adapters.test_review_execution_control import OWNER, dump, rig
from tests.app.test_review_execution_activation import rig as activation_rig


def _advance(store, key, now, **options):
    with store.transaction() as db:
        return advance_review_execution_compaction_maintenance(db, key, now=now, **options)


def _ready(store, key, now, **options):
    pending = []
    for _ in range(256):
        result = _advance(store, key, now, **options)
        if isinstance(result, PendingReviewExecutionMaintenance):
            pending.append(result.stage)
            continue
        assert isinstance(result, ReadyReviewExecutionMaintenance), result
        return result, pending
    raise AssertionError("maintenance did not complete its bounded pages")


def test_below_threshold_is_inert(rig) -> None:
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    before = dump(store)
    result = _advance(store, key, store.now())
    assert isinstance(result, ReadyReviewExecutionMaintenance)
    assert result.revision == selection.expected
    assert dump(store) == before


@pytest.mark.parametrize("source", (_copy_source, _abandoned_source))
def test_first_checkpoint_through_public_control_preserves_state(rig, source) -> None:
    store, key, plan, now = source(rig)
    control = rig[4]
    first = control.maintain_archive(key, round_threshold=1)
    assert isinstance(first, PendingReviewExecutionMaintenance)
    assert first.stage is ReviewExecutionMaintenanceStage.INITIAL_ARCHIVE
    for _ in range(256):
        result = control.maintain_archive(key, round_threshold=1)
        if isinstance(result, PendingReviewExecutionMaintenance):
            continue
        assert isinstance(result, ReadyReviewExecutionMaintenance), result
        break
    else:
        raise AssertionError("first checkpoint maintenance did not finish")
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.revision == result.revision
        assert snapshot.execution.checkpoint.through_sequence == plan.round.sequence
        assert snapshot.execution.checkpoint_receipt is not None
    assert isinstance(_advance(store, key, now, round_threshold=1), ReadyReviewExecutionMaintenance)


def test_open_first_round_has_specific_capacity_refusal_without_receipt(rig) -> None:
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    key = selection.expected.fence.key
    for _ in range(256):
        result = _advance(store, key, store.now(), round_threshold=1)
        if isinstance(result, PendingReviewExecutionMaintenance):
            continue
        assert isinstance(result, BlockedReviewExecutionMaintenance), result
        assert "open prefix" in result.reason
        break
    else:
        raise AssertionError("open-prefix maintenance did not reach a decision")
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.execution.last_sequence == plan.round.sequence
        assert snapshot.execution.checkpoint_receipt is None


def test_tiny_shared_budget_preserves_every_source_and_cursor(rig) -> None:
    store, key, _, now = _copy_source(rig)
    before = dump(store)
    result = _advance(store, key, now, round_threshold=1, max_source_bytes=1)
    assert isinstance(result, BlockedReviewExecutionMaintenance)
    assert dump(store) == before


def test_compacted_overdue_certificate_waits_for_paged_renewal(rig) -> None:
    store, key, _, now = _copy_source(rig)
    _ready(store, key, now, round_threshold=1)
    with store.transaction() as db:
        from issue_agent.adapters.review_execution_archive_gate import read_review_execution_archive_gate
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
    at_due = gate.next_due_at
    first = _advance(store, key, at_due, round_threshold=1)
    assert isinstance(first, PendingReviewExecutionMaintenance)
    assert first.stage is ReviewExecutionMaintenanceStage.RETIRED_ARCHIVE
    reopened = SQLiteControlStore(store.path, now=store.now)
    result, _ = _ready(reopened, key, at_due, round_threshold=1)
    assert isinstance(result, ReadyReviewExecutionMaintenance)


def test_paid_settlement_rows_are_unchanged_by_first_checkpoint(rig) -> None:
    store, key, plan, now = _paid_source(rig)
    with store.transaction() as db:
        before = (
            tuple(tuple(row) for row in db.execute("SELECT * FROM charges ORDER BY rowid")),
            tuple(tuple(row) for row in db.execute("SELECT * FROM budget_reservations ORDER BY rowid")),
        )
    _ready(store, key, now, round_threshold=1)
    with store.transaction() as db:
        after = (
            tuple(tuple(row) for row in db.execute("SELECT * FROM charges ORDER BY rowid")),
            tuple(tuple(row) for row in db.execute("SELECT * FROM budget_reservations ORDER BY rowid")),
        )
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.execution.checkpoint.through_sequence == plan.round.sequence
    assert after == before


def test_payload_capacity_trigger_without_new_frontier_refuses_cleanly(rig) -> None:
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    for _ in range(256):
        result = _advance(store, key, store.now(), payload_threshold=1)
        if isinstance(result, PendingReviewExecutionMaintenance):
            continue
        assert isinstance(result, BlockedReviewExecutionMaintenance)
        assert "compactable capacity" in result.reason
        break
    else:
        raise AssertionError("capacity refusal did not finish source passes")
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None and snapshot.execution.checkpoint_receipt is None


def test_second_checkpoint_and_stale_captured_preflight(rig) -> None:
    store, key, first_round, now = _abandoned_source(rig)
    _ready(store, key, now, round_threshold=1)
    control = rig[4]
    selection = _next_selection(rig, store, observed_at=store.now())
    second = control.preflight(selection)
    assert second.round.sequence == first_round.round.sequence + 1
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(second.round.round_id)
    control.finalize_abandoned_round(second.round.round_id)
    captured = replace(
        _next_selection(rig, store, observed_at=store.now()),
        request_id="preflight:stale-after-maintenance",
    )
    result, stages = _ready(store, key, store.now(), round_threshold=1)
    assert ReviewExecutionMaintenanceStage.RETIRED_ARCHIVE in stages
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.revision == result.revision
        assert snapshot.execution.checkpoint.through_sequence == second.round.sequence
    with pytest.raises(WorkflowRevisionConflict):
        control.preflight(captured)


def test_real_zero_new_round_generation_checkpoint(activated_rig) -> None:
    store, gateway, key, round_plan, first = _ceiling_checkpoint(activated_rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
    assert before is not None
    release_control = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=0,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    released = release_control.reconcile_ceiling(key)
    assert isinstance(released, WorkflowCommitted)
    assert release_control.release_projection(key).status is ReleaseStatus.PENDING
    result, stages = _ready(store, key, store.now(), generation_threshold=1)
    assert ReviewExecutionMaintenanceStage.RETIRED_ARCHIVE in stages
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.revision == result.revision
        assert after.execution.checkpoint.through_sequence == round_plan.round.sequence
        assert after.execution.checkpoint.boundary_cursor is not None
        assert after.execution.checkpoint_receipt != first.receipt_id
        assert after.execution.last_sequence == before.execution.last_sequence
        assert after.gates == ()


def test_default_round_threshold_preflight_progress_retry_and_fresh_proposal(activated_rig) -> None:
    store, _, gateway, selection, control = activated_rig
    key = selection.expected.fence.key
    original_selection = selection
    original_plan = None
    for index in range(128):
        if index:
            assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
            occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
            with store.transaction() as db:
                snapshot = load_review_workflow_row(db, key)
                assert snapshot is not None
            selection = replace(
                selection, request_id=f"default-threshold:{index}",
                expected=snapshot.revision, binding=snapshot.execution.binding,
                claim_occurrence_id=occurrence,
                observation=replace(selection.observation, observed_at=store.now()),
            )
        plan = control.preflight(selection)
        if index == 0:
            original_plan = plan
        assert plan.round.sequence == index + 1
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        control.seal_abandoned_planning(plan.round.round_id)
        control.finalize_abandoned_round(plan.round.round_id)
    assert original_plan is not None

    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        audit_before = db.execute(
            "SELECT namespace,scope,key,payload_json,updated_at FROM level_state ORDER BY namespace,scope,key"
        ).fetchall()
    captured = replace(
        selection, request_id="default-threshold:captured", expected=before.revision,
        binding=before.execution.binding, claim_occurrence_id=occurrence,
        observation=replace(selection.observation, observed_at=store.now()),
    )
    gateway.unavailable = True
    with pytest.raises(WorkflowAuthorityError, match="awaits archive maintenance"):
        control.preflight(captured)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds").fetchone()[0] == 128
        assert db.execute(
            "SELECT namespace,scope,key,payload_json,updated_at FROM level_state ORDER BY namespace,scope,key"
        ).fetchall() != audit_before
    inert = dump(store)
    assert control.preflight(original_selection) == original_plan
    assert dump(store) == inert
    gateway.unavailable = False

    pending = 1
    for _ in range(1024):
        try:
            control.preflight(captured)
        except WorkflowRevisionConflict:
            break
        except WorkflowAuthorityError as exc:
            assert "awaits archive maintenance" in str(exc), str(exc)
            pending += 1
    else:
        raise AssertionError("default maintenance did not publish a checkpoint")
    assert pending > 1
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint.through_sequence == 128
        assert after.revision != captured.expected
    fresh = replace(
        captured, request_id="default-threshold:fresh", expected=after.revision,
        binding=after.execution.binding,
        observation=replace(captured.observation, observed_at=store.now()),
    )
    assert control.preflight(fresh).round.sequence == 129
