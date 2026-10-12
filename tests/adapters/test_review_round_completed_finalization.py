"""Completed planning finalizes only from its sealed, settled source facts."""

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review_execution import ReviewRoundFailure, ReviewUnlaunchedRound
from issue_agent.domain.review_rounds import (
    FinalizedReviewRound, ReviewRoundAwaitingEvidence, ReviewRoundWait, ReviewSubmissionKind,
)
from tests.adapters.test_review_execution_admission import planned, request
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_submission import command_for


def test_refused_admission_finalizes_completed_round_and_replays_without_writes(planned):
    store, selection, control, child = planned
    refusal = store.record_review_admission_refusal(request(child, cap=59))
    assert refusal is not None
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)

    result = control.finalize_completed_round(child.round.round_id)

    assert isinstance(result, FinalizedReviewRound)
    assert result.outcome.disposition == ReviewUnlaunchedRound(
        control.read_round_facts(child.round.round_id).closure_receipt,
    )
    before = dump(store)
    assert control.finalize_completed_round(child.round.round_id) == result
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-refusal'")
        assert load_review_workflow_row(db, selection.expected.fence.key) is not None
    with pytest.raises(WorkflowAuthorityError):
        control.finalize_completed_round(child.round.round_id)


def test_pending_completed_child_waits_for_terminal_accounting(planned):
    store, selection, control, child = planned
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    before = current(store, selection)

    assert control.finalize_completed_round(child.round.round_id) == ReviewRoundAwaitingEvidence(
        frozenset({ReviewRoundWait.CHILDREN}),
    )
    assert current(store, selection) == before
    with store.transaction() as db:
        row = db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds").fetchone()
        assert tuple(row) == (None, None)
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-recovery-wait'").fetchone()[0] == 1


def test_composed_sweep_finalizes_sealed_completed_round_after_refusal(planned):
    store, selection, control, child = planned
    assert store.record_review_admission_refusal(request(child, cap=59)) is not None
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)

    sweep = control.reconcile_review_rounds()

    assert sweep.finalized == (child.round.round_id,)
    assert sweep.blocked == ()
    assert current(store, selection).execution.state.failures == 0
    assert control.reconcile_review_rounds().finalized == ()


def test_settled_failed_child_finalizes_completed_round_with_one_strike(planned):
    store, selection, control, child = planned
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    proposed = request(child)
    assert store.reserve(proposed).admitted
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(
        True, status="error", exit_reason=ExitReason.AGENT_ERROR,
    ))
    store.settle_review_return(settlement(proposed), raw)
    before_failures = current(store, selection).execution.state.failures

    sweep = control.reconcile_review_rounds()
    assert sweep.finalized == (child.round.round_id,)
    result = control.finalize_completed_round(child.round.round_id)

    assert isinstance(result, FinalizedReviewRound)
    assert isinstance(result.outcome.disposition, ReviewRoundFailure)
    assert current(store, selection).execution.state.failures == before_failures + 1


def test_confirmed_copy_requires_joint_productive_settlement(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER,
                                      store.now(), store.now(), EffectStatus.SUCCEEDED, remote_object_id="review:9"))
    before = dump(store)

    with pytest.raises(WorkflowAuthorityError, match="joint settlement receipt"):
        control.finalize_completed_round(plan.round.round_id)
    assert dump(store) == before
