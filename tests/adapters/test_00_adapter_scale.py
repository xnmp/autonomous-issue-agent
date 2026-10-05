"""Full-scale review histories overlap smaller adapter contracts."""

from dataclasses import replace

import pytest

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_control import (
    OWNER, _measure_current_read, assert_unpaid, current, rig,
)


def test_real_zero_round_generations_have_no_lifetime_limit(rig):
    import time
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.adapters.workflow_rows import read_current_review_pending
    from issue_agent.domain.review_execution import MAX_EXECUTION_SUFFIX
    from issue_agent.domain.review_workflow import CountingRule
    from issue_agent.domain.workflow import (
        CancelRearm, ContinueGrantedRearm, KeepGuidance, OperatorActor, RequestRearm, WorkflowCommitted,
    )
    from tests.adapters.test_review_productive_settlement import confirmed_escalation

    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause='ceiling')
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter('o/r', 'review', OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule('review-bot[bot]'),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    initial = current(store, selection)
    key = initial.revision.fence.key
    actor = OperatorActor('operator', 'test')
    request_id = 's7:rearm:1'
    started = time.perf_counter()
    measurements = []
    assert isinstance(workflow.request_rearm(RequestRearm(
        request_id, initial.revision, initial.gates[0].gate_id, KeepGuidance(),
        expected_binding=initial.binding,
    ), actor), WorkflowCommitted)
    for advance in range(1, MAX_EXECUTION_SUFFIX + 2):
        if advance > 1:
            pending = current(store, selection)
            assert isinstance(workflow.cancel_rearm(CancelRearm(
                f's7:cancel:{advance}', pending.revision, request_id, 'stop', expected_binding=pending.binding,
            ), actor), WorkflowCommitted)
            stopped = current(store, selection)
            next_request = f's7:rearm:{advance}'
            assert isinstance(workflow.continue_rearm(ContinueGrantedRearm(
                next_request, stopped.revision, request_id, expected_binding=stopped.binding,
            ), actor), WorkflowCommitted)
            request_id = next_request
        if advance in (10, 1000, MAX_EXECUTION_SUFFIX + 1):
            advanced, measured = _measure_current_read(store, key)
            assert advanced.revision.fence.generation == initial.revision.fence.generation + advance
            assert advanced.execution.last_sequence == initial.execution.last_sequence == plan.round.sequence
            assert advanced.execution.state.failures == initial.execution.state.failures
            assert advanced.execution.state.pin_deferrals == initial.execution.state.pin_deferrals
            assert advanced.gates
            with pytest.raises(ValueError, match='held by workflow recovery'):
                control.preflight(replace(
                    selection, request_id=f's7:held:{advance}', expected=advanced.revision,
                    binding=advanced.execution.binding,
                ))
            with store.transaction() as db:
                assert read_current_review_pending(db, advanced.execution) == ()
                assert db.execute('SELECT COUNT(*) FROM review_execution_rounds').fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-generation'").fetchone()[0] == advance
                for table in ('runs', 'leases', 'budget_reservations', 'charges'):
                    assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round
            measurements.append(measured)
            print(f'S7 generations={advance} rows={measured[0]} bytes={measured[1]} vm={measured[2]} '
                  f'read_seconds={measured[3]:.6f} history_seconds={time.perf_counter()-started:.3f} '
                  f'plan={measured[4]}', flush=True)
    assert all(measured[0] == measurements[0][0] for measured in measurements)
    assert all(measured[1] <= measurements[0][1] + 1024 for measured in measurements)
    assert all(measured[2] <= measurements[0][2] + 128 for measured in measurements)


def test_preflight_opens_exactly_one_unpaid_round_and_preserves_both_family_allowances(rig):
    store, before, gateway, selection, control = rig
    plan = control.preflight(selection)
    after = current(store, selection)
    assert plan.round.binding == selection.binding and plan.round.sequence == 1
    assert plan.head_sha == gateway.view.head_ref_oid and plan.stages == () and plan.submissions == ()
    assert after.revision.version == before.revision.version + 1
    assert after.revision.fence == before.revision.fence
    assert after.episode == before.episode and after.execution.state == before.execution.state
    assert after.execution.last_sequence == 1
    with store.transaction() as db:
        assert db.execute("SELECT claim_occurrence_id FROM review_execution_rounds").fetchone()[0] == selection.claim_occurrence_id
    assert_unpaid(store)


def test_real_sequential_round_history_keeps_current_reads_bounded(rig):
    import time
    from issue_agent.domain.review_rounds import FinalizedReviewRound

    store, initial, _, selection, control = rig
    key = selection.expected.fence.key
    checkpoints = (10, 1000, 5000)
    measurements = []
    started = time.perf_counter()
    for sequence in range(1, checkpoints[-1] + 1):
        if sequence > 1:
            assert store.claim_dispatch('o/r', 'review', -9, AgentKind.REVIEW.name, OWNER, frozenset())
        occurrence = store.active_dispatch_claims('o/r', 'review')[0].claim_key
        snapshot = current(store, selection)
        plan = control.preflight(replace(
            selection, request_id=f's7:round:{sequence}', expected=snapshot.revision,
            binding=snapshot.execution.binding, claim_occurrence_id=occurrence,
            observation=replace(selection.observation, observed_at=store.now()), failure_ceiling=0,
        ))
        assert store.release_dispatch_claim('o/r', 'review', -9, OWNER)
        control.seal_abandoned_planning(plan.round.round_id)
        result = control.finalize_abandoned_round(plan.round.round_id)
        assert isinstance(result, FinalizedReviewRound)
        if sequence in checkpoints:
            completed, measured = _measure_current_read(store, key)
            assert completed.execution.last_sequence == sequence
            assert completed.execution.baseline.through_sequence == sequence
            assert completed.execution.state == initial.execution.state
            assert completed.episode == initial.episode
            with store.transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL').fetchone()[0] == sequence
                assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-outcome'").fetchone()[0] == sequence
            assert control.finalize_abandoned_round(plan.round.round_id) == result
            assert_unpaid(store)
            measurements.append(measured)
            print(f'S7 rounds={sequence} rows={measured[0]} bytes={measured[1]} vm={measured[2]} '
                  f'read_seconds={measured[3]:.6f} history_seconds={time.perf_counter()-started:.3f} '
                  f'plan={measured[4]}', flush=True)
    assert all(measured[0] == measurements[0][0] for measured in measurements)
    # Decimal sequence/version widths can change; retired rows cannot add selected sources.
    assert all(measured[1] <= measurements[0][1] + 256 for measured in measurements)
