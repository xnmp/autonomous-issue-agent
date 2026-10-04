from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.review_test_execution import read_test_completion
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import TestExecution as Execution, TestExecutionStatus as ExecutionStatus
from issue_agent.domain.review_evidence import ReviewEvidenceKind
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.review_rounds import ReviewPlanningRefused
from issue_agent.domain.review_test_evidence import ReviewTestInvocation, ReviewTestSubject, ReviewTestCompletion, ReviewTestOutcome, ReviewTestTreeProof
from issue_agent.domain.workflow import OpenGate, ReconciliationPolicy
from tests.adapters.test_review_execution_control import rig, current
from tests.domain.test_review_test_evidence import configuration


def setup(rig, *, ceiling=3):
    store, snapshot, gateway, selection, execution = rig
    workflow = SQLiteReviewWorkflowControl(store, execution._writer, gateway=gateway, ceiling=3,
        trigger_label=selection.trigger_label, rule=CountingRule("review-bot"),
        policy=ReconciliationPolicy(maximum_reads=ceiling))
    plan = execution.preflight(selection)
    config = configuration()
    subject = ReviewTestSubject(plan.round.binding.rejection, plan.round.binding, plan.head_sha, "d" * 64, config.identity)
    invocation = ReviewTestInvocation(subject, config, plan.round, "test-invocation:1", store.now())
    return store, workflow, execution, invocation, selection


def result(invocation, outcome):
    if outcome is ReviewTestOutcome.UNAVAILABLE:
        return ReviewTestCompletion(invocation, outcome, None, None, None, "infrastructure unavailable")
    proof = ReviewTestTreeProof(invocation.subject.head_sha, "e" * 40, "e" * 40, "e" * 40)
    execution = Execution(ExecutionStatus.PASSED if outcome is ReviewTestOutcome.PASSED else ExecutionStatus.TEST_FAILED,
        ("bash", "-c", invocation.configuration.scope.command), 0 if outcome is ReviewTestOutcome.PASSED else 1,
        1, "f" * 64, "a" * 64, invocation.configuration.executor_policy, invocation.subject.head_sha)
    return ReviewTestCompletion(invocation, outcome, execution, proof, proof, "test result")


@pytest.mark.parametrize("outcome", [ReviewTestOutcome.PASSED, ReviewTestOutcome.FAILED])
def test_stable_completion_reused_after_restart_and_compatible_new_round(rig, outcome):
    store, workflow, execution, invocation, selection = setup(rig)
    selected, schedule = execution.begin_test(invocation, workflow)
    receipt = execution.finish_test(result(selected, outcome), schedule, workflow)
    state = current(store, selection)
    execution.seal_completed_planning(invocation.round.round_id, state.revision, ReviewPlanningRefused("test-close"))
    execution.finalize_completed_round(invocation.round.round_id)
    state = current(store, selection)
    plan = execution.preflight(replace(selection, request_id="new-round", expected=state.revision, binding=state.execution.binding))
    proposed = replace(invocation, round=plan.round, invocation_id="test-invocation:2")
    restarted = SQLiteReviewExecutionControl(store, execution._writer, execution._gateway)
    selected, _ = restarted.begin_test(proposed, workflow)
    assert selected == receipt


def test_unavailable_schedule_survives_restart_and_ceiling_stops_invocations(rig):
    store, workflow, execution, invocation, selection = setup(rig, ceiling=1)
    selected, schedule = execution.begin_test(invocation, workflow)
    execution.finish_test(result(selected, ReviewTestOutcome.UNAVAILABLE), schedule, workflow)
    state = current(store, selection)
    assert any(isinstance(gate, OpenGate) and gate.gate_id == invocation.subject.gate_id for gate in state.gates)
    restarted = SQLiteReviewExecutionControl(store, execution._writer, execution._gateway)
    assert restarted.begin_test(replace(invocation, invocation_id="test-invocation:2"), workflow)[0] is None
    assert not workflow._reread_evidence(state, ReviewEvidenceKind.ORDINARY_TEST)[0]
    assert invocation.subject.gate_id in {gate.gate_id for gate in current(store, selection).gates}


def test_completion_schedule_cas_failure_rolls_back_payload_and_hold(rig):
    store, workflow, execution, invocation, selection = setup(rig)
    execution.begin_test(invocation, workflow)
    with pytest.raises(WorkflowAuthorityError, match="schedule changed"):
        execution.finish_test(result(invocation, ReviewTestOutcome.UNAVAILABLE), object(), workflow)
    with store.transaction() as db:
        assert read_test_completion(db, "o/r", invocation.invocation_id) is None
    assert current(store, selection).gates == ()


def test_late_completion_cannot_clear_changed_authority(rig):
    store, workflow, execution, invocation, selection = setup(rig)
    execution.begin_test(invocation, workflow)
    changed = replace(invocation, subject=replace(invocation.subject, head_sha="c" * 40))
    with pytest.raises(WorkflowAuthorityError, match="no longer selected"):
        execution.finish_test(result(changed, ReviewTestOutcome.PASSED), None, workflow)


def test_real_sql_test_ceiling_offers_and_accepts_existing_renewal_preserving_other_evidence(rig):
    from issue_agent.domain.review_control import execution_recovery_choices, ActionOffered, ReviewOperatorCommand
    from issue_agent.domain.review_dispatch_record import DecisionBasis, DecisionRead, ReviewNotAdmitted
    from issue_agent.domain.workflow import RequestReviewExecutionRenewal, KeepGuidance, OperatorActor, WorkflowCommitted
    store, workflow, execution, invocation, selection = setup(rig, ceiling=1)
    selected, schedule = execution.begin_test(invocation, workflow)
    execution.finish_test(result(selected, ReviewTestOutcome.UNAVAILABLE), schedule, workflow)
    state = current(store, selection)
    execution.seal_completed_planning(invocation.round.round_id, state.revision, ReviewPlanningRefused("ordinary-test-stop"))
    execution.finalize_completed_round(invocation.round.round_id)
    workflow.record_evidence(selection.observation.key, invocation.subject.head_sha, ReviewEvidenceKind.EXEMPTION_TRAILERS, error="trailers unavailable")
    workflow.record_dispatch_decision(selection.observation.key, ReviewNotAdmitted(("ordinary tests unavailable",)),
        DecisionBasis(invocation.subject.head_sha, selection.observation.state, selection.observation.labels, ()), DecisionRead.REVIEW_READ)
    before = current(store, selection)
    choices = execution_recovery_choices(workflow.review_control_view(selection.observation.key))
    choice = next((gate, action) for gate, action in choices if gate.gate_id == invocation.subject.gate_id)
    assert isinstance(choice[1], ActionOffered) and choice[1].command is ReviewOperatorCommand.RENEW_EXECUTION
    command = RequestReviewExecutionRenewal("renew:test", before.revision, invocation.subject.gate_id, KeepGuidance(),
        before.execution.binding.execution_epoch, before.execution.binding.execution_episode, expected_binding=before.binding)
    accepted = workflow.request_rearm(command, OperatorActor("operator:test", "test"))
    assert isinstance(accepted, WorkflowCommitted)
    after = accepted.snapshot
    other_ids = {gate.gate_id for gate in before.gates if gate.gate_id != invocation.subject.gate_id}
    assert other_ids <= {(gate.gate if hasattr(gate, "gate") else gate).gate_id for gate in after.gates}
    assert after.execution.binding.execution_episode != before.execution.binding.execution_episode
    assert after.episode.episode_id == before.episode.episode_id
    assert workflow.request_rearm(command, OperatorActor("operator:test", "test")).idempotent
