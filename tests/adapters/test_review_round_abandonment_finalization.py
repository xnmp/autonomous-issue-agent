"""Unpaid parent abandonment commits one source-backed outer outcome."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.review_execution import (
    ReviewCancellationReason, ReviewRoundCancelled, ReviewUnlaunchedRound,
)
from issue_agent.domain.review_rounds import (
    FinalizedReviewRound, ReviewRoundAwaitingEvidence, ReviewRoundWait,
    ReviewSubmissionKind,
)
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_launch import register


def test_open_or_unresolved_abandoned_round_does_not_write_an_outcome(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    before = dump(store)
    assert control.finalize_abandoned_round(plan.round.round_id) == ReviewRoundAwaitingEvidence(
        frozenset({ReviewRoundWait.PLANNING}),
    )
    assert dump(store) == before

    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    before = dump(store)
    assert control.finalize_abandoned_round(plan.round.round_id) == ReviewRoundAwaitingEvidence(
        frozenset({ReviewRoundWait.SUBMISSIONS}),
    )
    assert dump(store) != before
    with store.transaction() as db:
        row = db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        assert row["closure_json"] is row["outcome_json"] is None
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-recovery-wait'").fetchone()[0] == 1


@pytest.mark.parametrize("planned_child", [False, True])
def test_proven_unpaid_abandonment_commits_and_replays_exactly(rig, planned_child):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    if planned_child:
        control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    result = control.finalize_abandoned_round(plan.round.round_id)
    assert isinstance(result, FinalizedReviewRound)
    assert result.outcome.disposition == ReviewRoundCancelled(
        ReviewCancellationReason.UNPAID_ABANDONMENT,
        control.read_round_facts(plan.round.round_id).closure_receipt,
    )
    after = current(store, selection)
    assert after.execution.checkpoint.through_sequence == 0
    assert after.execution.checkpoint_receipt is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-outcome'").fetchone()[0] == 1
        assert load_review_workflow_row(db, selection.expected.fence.key) == after
    before = dump(store)
    gateway.error = AssertionError("replay must not read the gateway")
    restarted = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", OWNER), gateway)
    assert restarted.finalize_abandoned_round(plan.round.round_id) == result
    assert dump(store) == before


def test_later_abandonment_remains_in_suffix_until_earlier_round_closes(rig):
    store, _, _, selection, control = rig
    first = control.preflight(selection)
    second = control.preflight(replace(selection, request_id="preflight:second",
                                       expected=current(store, selection).revision))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(first.round.round_id)
    control.seal_abandoned_planning(second.round.round_id)
    assert isinstance(control.finalize_abandoned_round(second.round.round_id), FinalizedReviewRound)
    middle = current(store, selection)
    assert middle.execution.checkpoint.through_sequence == 0
    assert middle.execution.last_sequence == 2
    assert isinstance(control.finalize_abandoned_round(first.round.round_id), FinalizedReviewRound)
    after = current(store, selection)
    assert after.execution.checkpoint.through_sequence == 0
    assert after.execution.last_sequence == 2


def test_abandoned_round_with_refused_copy_commits_unlaunched_outcome(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.PERMANENT_FAILURE, error="head changed"))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    result = control.finalize_abandoned_round(plan.round.round_id)
    assert isinstance(result, FinalizedReviewRound)
    assert result.outcome.disposition == ReviewUnlaunchedRound(
        control.read_round_facts(plan.round.round_id).closure_receipt,
    )
    assert current(store, selection).execution.state.failures == 3


def test_unresolved_submission_records_bounded_wait_and_later_settles(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    for _ in range(7):
        assert isinstance(control.finalize_abandoned_round(plan.round.round_id), ReviewRoundAwaitingEvidence)
    sweep = control.reconcile_review_rounds()
    assert sweep.finalized == ()
    assert sweep.blocked == ((plan.round.round_id,
                              "round evidence remains pending after bounded recovery observations"),)
    with store.transaction() as db:
        payload = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-recovery-wait'",
        ).fetchone()[0]
        assert decode_workflow(payload, tuple)[3] == 8
    before = dump(store)
    assert control.reconcile_review_rounds().blocked == sweep.blocked
    assert dump(store) == before
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.PERMANENT_FAILURE, error="head changed"))
    assert control.reconcile_review_rounds().finalized == (plan.round.round_id,)
    assert current(store, selection).execution.state.failures == 3


def test_command_attempt_progress_resets_recovery_wait_ceiling(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    command = command_for(current(store, selection))
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    for _ in range(8):
        control.finalize_abandoned_round(plan.round.round_id)
    assert control.reconcile_review_rounds().blocked
    store.begin_command(command.command_id)
    assert control.reconcile_review_rounds().blocked == ()
    with store.transaction() as db:
        payload = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-recovery-wait'",
        ).fetchone()[0]
    assert decode_workflow(payload, tuple)[3] == 1


def test_live_leased_child_does_not_consume_recovery_wait_ceiling(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    planned = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    for _ in range(9):
        assert isinstance(control.finalize_abandoned_round(plan.round.round_id), ReviewRoundAwaitingEvidence)
    assert control.reconcile_review_rounds().blocked == ()
    with store.transaction() as db:
        payload = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-recovery-wait'",
        ).fetchone()[0]
    assert decode_workflow(payload, tuple)[3] == 1


def test_abandoned_round_with_refused_child_commits_unlaunched_outcome(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    planned = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    child = planned.children[0]
    proposed = request(child, cap=59)
    assert not store.reserve(proposed).admitted
    assert store.record_review_admission_refusal(proposed) is not None
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    result = control.finalize_abandoned_round(plan.round.round_id)
    assert isinstance(result, FinalizedReviewRound)
    assert isinstance(result.outcome.disposition, ReviewUnlaunchedRound)
    assert current(store, selection).execution.state.failures == 3


@pytest.mark.parametrize("corruption", ["source", "receipt", "closure"])
def test_finalized_abandonment_loader_refuses_changed_provenance(rig, corruption):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    control.finalize_abandoned_round(plan.round.round_id)
    with store.transaction() as db:
        if corruption == "source":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-round-abandonment'")
        elif corruption == "receipt":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-round-outcome'")
        else:
            db.execute("UPDATE review_execution_rounds SET closure_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        load_review_workflow_row(db, selection.expected.fence.key)


def test_finalization_rolls_back_closure_receipt_and_projection_together(rig, monkeypatch):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    before = dump(store)

    def refuse(*args, **kwargs):
        raise RuntimeError("transition refused")

    monkeypatch.setattr(store, "_transition", refuse)
    with pytest.raises(RuntimeError, match="transition refused"):
        control.finalize_abandoned_round(plan.round.round_id)
    assert dump(store) == before
