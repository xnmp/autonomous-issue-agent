"""Checkpoint frontiers follow real activated review rounds and generation advances."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_archive_identity import advance_review_archive_identity_audit
from issue_agent.adapters.review_execution_checkpoint_commit import commit_review_execution_checkpoint
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.labels import Label
from issue_agent.domain.review_archive_identity import CompletedReviewArchiveIdentities
from issue_agent.domain.review_rearm import ReleaseStatus
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.workflow import WorkflowCommitted
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_archive_evidence import _complete, _complete_source_passes
from tests.adapters.test_review_execution_archive_round import _covered_rig, rig as activated_rig
from tests.adapters.test_review_execution_control import OWNER, dump
from tests.adapters.test_review_productive_settlement import confirmed_escalation
from tests.app.test_review_execution_activation import rig as activation_rig


def _complete_identities(store, now):
    for _ in range(64):
        with store.transaction() as db:
            state = advance_review_archive_identity_audit(db, now=now)
        if isinstance(state, CompletedReviewArchiveIdentities):
            return
    raise AssertionError("archive identity census did not complete")


def _ceiling_checkpoint(activated_rig):
    store, gateway, selection, _, round_plan, command = confirmed_escalation(
        _covered_rig(activated_rig), cause="ceiling",
    )
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    key = selection.expected.fence.key
    now = store.now()
    _complete_identities(store, now)
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None and before.gates
        first = commit_review_execution_checkpoint(
            db, key, before.revision, round_plan.round.sequence, now=now,
        )
    return store, gateway, key, round_plan, first


def test_generation_only_checkpoint_retires_zero_new_rounds_and_preserves_release_hold(activated_rig):
    store, gateway, key, round_plan, first = _ceiling_checkpoint(activated_rig)
    with store.transaction() as db:
        before_release = load_review_workflow_row(db, key)
    assert before_release is not None
    release_control = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=0,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    released = release_control.reconcile_ceiling(key)
    assert isinstance(released, WorkflowCommitted)
    assert released.snapshot.revision.fence.generation == before_release.revision.fence.generation + 1
    assert released.snapshot.execution.last_sequence == round_plan.round.sequence
    assert released.snapshot.gates == ()
    assert release_control.release_projection(key).status is ReleaseStatus.PENDING
    now = store.now()
    _complete_identities(store, now)
    _complete(store, key, now)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        second = commit_review_execution_checkpoint(
            db, key, before.revision, round_plan.round.sequence, now=now,
        )
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint.through_sequence == round_plan.round.sequence
        assert after.execution.checkpoint.boundary_cursor is not None
        assert after.execution.state == before.execution.state
        assert after.execution.last_sequence == before.execution.last_sequence
        assert after.gates == before.gates == ()
        assert after.episode.continuation == before.episode.continuation
        assert second.receipt_id != first.receipt_id
    assert release_control.release_projection(key).status is ReleaseStatus.PENDING


def test_checkpoint_refuses_open_prefix_and_accepts_finalized_prefix_with_later_tail(activated_rig):
    store, _, gateway, selection, control = _covered_rig(activated_rig)
    key = selection.expected.fence.key
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    now = store.now()
    _complete_identities(store, now)
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        baseline = load_review_workflow_row(db, key)
        assert baseline is not None
        commit_review_execution_checkpoint(db, key, baseline.revision, 1, now=now)
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    first_selection = replace(
        selection, request_id="frontier:first", expected=current.revision,
        binding=current.execution.binding, claim_occurrence_id=occurrence,
        observation=replace(selection.observation, observed_at=store.now()),
    )
    first = control.preflight(first_selection)
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    second_selection = replace(first_selection, request_id="frontier:second",
                               expected=current.revision)
    second = control.preflight(second_selection)
    assert (first.round.sequence, second.round.sequence) == (2, 3)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(second.round.round_id)
    control.finalize_abandoned_round(second.round.round_id)
    before_refusal = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        with store.transaction() as db:
            current = load_review_workflow_row(db, key)
            assert current is not None
            commit_review_execution_checkpoint(db, key, current.revision,
                                               second.round.sequence, now=store.now())
    assert dump(store) == before_refusal
    with store.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-checkpoint'",
        ).fetchone()[0] == 1
    control.seal_abandoned_planning(first.round.round_id)
    control.finalize_abandoned_round(first.round.round_id)
    now = store.now()
    _complete_identities(store, now)
    _complete(store, key, now)
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
        assert current is not None
        committed = commit_review_execution_checkpoint(
            db, key, current.revision, first.round.sequence, now=now,
        )
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint.through_sequence == first.round.sequence
        assert after.execution.last_sequence == second.round.sequence
        assert after.execution.state == current.execution.state
        assert committed.receipt_id == after.execution.checkpoint_receipt
