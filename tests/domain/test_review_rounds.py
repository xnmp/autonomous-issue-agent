"""Finalization truth table over verified evidence, without inventing runtime receipts."""

from dataclasses import replace
from itertools import permutations

import pytest

from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain import review_execution as execution
from issue_agent.domain import review_rounds as rounds
from issue_agent.domain.review_return import review_returned_envelope
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)

HEAD = "a" * 40
REVIEWER = execution.ReviewChildKind.REVIEWER
GATE = execution.ReviewChildKind.GATE
DIMENSION = execution.ReviewChildKind.DIMENSION
VALIDATOR = execution.ReviewChildKind.VALIDATOR


def identity(sequence=1):
    parent = WorkflowBinding(WorkflowFence(WorkflowKey("o/r", PullRequestRef(9)), 0),
                             FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), "rejections")
    return execution.ReviewRoundIdentity(execution.ReviewExecutionBinding(parent, 1, "execution"),
                                         f"round:{sequence}", sequence)


def stage(round_, kind, count=1, *, name=None):
    name = name or kind.value
    children = tuple(execution.ReviewChildIdentity(round_, name, f"{name}:{number}", kind)
                     for number in range(count))
    return rounds.ReviewStagePlan(name, kind, children)


def plan(*, mode=ReviewMode.SINGLE, empty=False, submissions=(), ceiling=5, reuse=rounds.ReviewSessionReuse.REUSABLE):
    round_ = identity()
    stages = (() if empty else (stage(round_, GATE), stage(round_, DIMENSION, 2), stage(round_, VALIDATOR))
              if mode is ReviewMode.MULTI else (stage(round_, REVIEWER),))
    return rounds.ReviewRoundPlan(round_, HEAD, mode, RuntimeName.CLAUDE, reuse, 4, ceiling, stages, submissions)


def launch(child):
    return execution.LaunchedReviewChild(child, f"run:{child.child_id}", f"launch:{child.child_id}")


def returned(child, *, result=None, handle=None):
    launched = launch(child)
    if result is None:
        session = (execution.ReusableReviewSession(RuntimeName.CLAUDE, handle, launched)
                   if handle else execution.NoReviewSession())
        result = rounds.ReviewChildSucceeded(session)
    return rounds.ReviewChildReturned(launched, f"return:{child.child_id}", result)


@pytest.mark.parametrize("runtime,kind,handle,expected", [
    (RuntimeName.CLAUDE, REVIEWER, "session:1", True),
    (RuntimeName.CODEX, REVIEWER, "session:1", False),
    (RuntimeName.CLAUDE, GATE, "session:1", False),
    (RuntimeName.CLAUDE, REVIEWER, "multi", False),
    (RuntimeName.CLAUDE, REVIEWER, "invalid\nhandle", False),
])
def test_normal_success_only_retains_an_eligible_reviewer_session(runtime, kind, handle, expected):
    child = execution.ReviewChildIdentity(identity(), kind.value, "child", kind)
    launched = launch(child)
    envelope = review_returned_envelope(AgentResult(
        False, status="APPROVE", session_id=handle, exit_reason=ExitReason.OK,
    ))
    result = rounds.classify_normal_review_return(launched, runtime, envelope)
    assert isinstance(result, rounds.ReviewChildSucceeded)
    assert isinstance(result.session, execution.ReusableReviewSession) is expected
    if expected:
        assert result.session.source == launched


@pytest.mark.parametrize("is_error,reason,expected", [
    (True, ExitReason.AGENT_ERROR, rounds.ReviewChildFailed),
    (True, ExitReason.TRANSPORT_DEATH, rounds.ReviewChildInfrastructureFailure),
    (True, ExitReason.KILLED_EXTERNALLY, rounds.ReviewChildInfrastructureFailure),
    (True, ExitReason.RUNTIME_UNAVAILABLE, rounds.ReviewChildInfrastructureFailure),
    (True, ExitReason.BUDGET_EXHAUSTED, rounds.ReviewChildBudgetExhausted),
    (True, ExitReason.OPERATOR_CANCELLED, type(None)),
    (False, ExitReason.AGENT_ERROR, type(None)),
    (True, ExitReason.OK, type(None)),
])
def test_normal_return_classification_waits_for_special_or_contradictory_results(is_error, reason, expected):
    launched = launch(plan().children[0])
    envelope = review_returned_envelope(AgentResult(is_error, exit_reason=reason))
    assert isinstance(rounds.classify_normal_review_return(launched, RuntimeName.CLAUDE, envelope), expected)


def failure(child, *, deterministic=False):
    determinism = rounds.DeterministicReviewFailure("deterministic:verified") if deterministic else rounds.OrdinaryReviewFailure()
    reason = ExitReason.TRANSPORT_DEATH if deterministic else ExitReason.AGENT_ERROR
    return returned(child, result=rounds.ReviewChildFailed(reason, determinism))


def submission(kind=rounds.ReviewSubmissionKind.PAID, command="submission"):
    return rounds.ReviewSubmissionPlan(command, kind)


def confirmed(command):
    return rounds.ReviewSubmissionEvidence(command, rounds.ReviewSubmissionConfirmed(f"confirmed:{command.command_id}"))


def refused(command):
    return rounds.ReviewSubmissionEvidence(command, rounds.ReviewSubmissionRefused(f"refused:{command.command_id}"))


def facts(planned, *, children=None, submissions=(), planning=None):
    return rounds.ReviewRoundFacts("complete-closure", planned,
                                  planning or rounds.ReviewPlanningSealed("planning-seal", rounds.ReviewPlanningComplete()),
                                  tuple(returned(child) for child in planned.children) if children is None else children,
                                  submissions)


def finalized(observed):
    result = rounds.finalize_review_round(observed)
    assert isinstance(result, rounds.FinalizedReviewRound)
    return result.outcome


def project(outcome, *, failures=2, pins=0):
    checkpoint = execution.ReviewExecutionCheckpoint(0, execution.ReviewExecutionState(
        outcome.round.binding, failures=failures, pin_deferrals=pins))
    return execution.project_review_execution(checkpoint, (outcome,)).state


@pytest.mark.parametrize("reason", [ExitReason.KILLED_EXTERNALLY, ExitReason.RUNTIME_UNAVAILABLE,
                                    ExitReason.TRANSPORT_DEATH])
def test_settled_infrastructure_only_round_preserves_failure_allowance(reason):
    planned = plan()
    child = planned.children[0]
    infra = returned(child, result=rounds.ReviewChildInfrastructureFailure(reason))
    outcome = finalized(facts(planned, children=(infra,)))
    assert outcome.disposition == execution.ReviewInfrastructureDeath((infra.launched,), "complete-closure")
    assert project(outcome, failures=3).failures == 3


def test_settled_budget_exhaustion_is_visible_without_charging_a_quality_strike():
    planned = plan()
    child = planned.children[0]
    exhausted = returned(child, result=rounds.ReviewChildBudgetExhausted())
    outcome = finalized(facts(planned, children=(exhausted,)))
    assert outcome.disposition == execution.ReviewBudgetExhausted((exhausted.launched,), "complete-closure")
    assert project(outcome, failures=3).failures == 3


def test_ordinary_failed_sibling_precedes_budget_exhaustion():
    planned = plan(mode=ReviewMode.MULTI)
    children = (returned(planned.children[0], result=rounds.ReviewChildBudgetExhausted()),
                failure(planned.children[1]), returned(planned.children[2]),
                returned(planned.children[3]))
    outcome = finalized(facts(planned, children=children))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome, failures=3).failures == 4


def test_ordinary_failed_sibling_still_charges_once_beside_infrastructure_loss():
    planned = plan(mode=ReviewMode.MULTI)
    children = (returned(planned.children[0], result=rounds.ReviewChildInfrastructureFailure(
        ExitReason.KILLED_EXTERNALLY)), failure(planned.children[1]),
        returned(planned.children[2]), returned(planned.children[3]))
    outcome = finalized(facts(planned, children=children))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome, failures=3).failures == 4


def test_refused_paid_submission_is_ordinary_failure_even_with_infrastructure_child():
    command = submission()
    planned = plan(submissions=(command,))
    child = planned.children[0]
    infra = returned(child, result=rounds.ReviewChildInfrastructureFailure(ExitReason.KILLED_EXTERNALLY))
    outcome = finalized(facts(planned, children=(infra,), submissions=(refused(command),)))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome, failures=3).failures == 4


def test_refused_paid_submission_precedes_budget_exhaustion():
    command = submission()
    planned = plan(submissions=(command,))
    exhausted = returned(planned.children[0], result=rounds.ReviewChildBudgetExhausted())
    outcome = finalized(facts(planned, children=(exhausted,), submissions=(refused(command),)))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome, failures=3).failures == 4


@pytest.mark.parametrize("reason", ["killed_externally", ExitReason.AGENT_ERROR, None])
def test_infrastructure_child_requires_a_typed_non_striking_reason(reason):
    with pytest.raises(ValueError):
        rounds.ReviewChildInfrastructureFailure(reason)


@pytest.mark.parametrize("mode", list(ReviewMode))
def test_a_live_unsealed_parent_does_not_finalize_its_terminal_children(mode):
    observed = facts(plan(mode=mode), planning=rounds.ReviewPlanningOpen())
    result = rounds.finalize_review_round(observed)
    assert result == rounds.ReviewRoundAwaitingEvidence(frozenset({rounds.ReviewRoundWait.PLANNING}))


@pytest.mark.parametrize("missing_child,pending_child,missing_submission,pending_submission", [
    (True, False, False, False), (False, True, False, False),
    (False, False, True, False), (False, False, False, True), (True, False, True, False),
])
def test_a_seal_never_turns_missing_or_unresolved_evidence_into_failure(
    missing_child, pending_child, missing_submission, pending_submission,
):
    command = submission()
    planned = plan(submissions=(command,))
    children = (() if missing_child else (rounds.ReviewChildPending(planned.children[0]),)
                if pending_child else (returned(planned.children[0]),))
    submissions = (() if missing_submission else
                   (rounds.ReviewSubmissionEvidence(command, rounds.ReviewSubmissionPending()),)
                   if pending_submission else (confirmed(command),))
    result = rounds.finalize_review_round(facts(planned, children=children, submissions=submissions))
    expected = set()
    if missing_child or pending_child:
        expected.add(rounds.ReviewRoundWait.CHILDREN)
    if missing_submission or pending_submission:
        expected.add(rounds.ReviewRoundWait.SUBMISSIONS)
    assert result == rounds.ReviewRoundAwaitingEvidence(frozenset(expected))


def test_open_planning_and_missing_children_and_submissions_are_all_reported():
    observed = facts(plan(submissions=(submission(),)), children=(), planning=rounds.ReviewPlanningOpen())
    assert rounds.finalize_review_round(observed).reasons == frozenset(rounds.ReviewRoundWait)


def test_confirmed_blocking_submission_wins_over_failed_pin_and_cancelled_siblings():
    command = submission()
    planned = plan(mode=ReviewMode.MULTI, submissions=(command,))
    children = (returned(planned.children[0]), failure(planned.children[1], deterministic=True),
                rounds.ReviewChildPinDeferred(planned.children[2], "pin-failed"),
                returned(planned.children[3], result=rounds.ReviewChildCancelled(
                    execution.ReviewCancellationReason.SHUTDOWN, "shutdown:verified")))
    for delivery in permutations(children):
        outcome = finalized(facts(planned, children=delivery, submissions=(confirmed(command),)))
        assert isinstance(outcome.disposition, execution.PaidReviewProgress)
        assert len(outcome.disposition.launched) == 3
        assert isinstance(outcome.session, execution.PreserveReviewSession)
        assert outcome.display.number == 4
        assert project(outcome, failures=8, pins=7).failures == 0


@pytest.mark.parametrize("reason", [execution.ReviewCancellationReason.OPERATOR, execution.ReviewCancellationReason.SHUTDOWN])
def test_explicit_cancellation_wins_over_pin_and_failure_without_erasing_prior_debit(reason):
    planned = plan(mode=ReviewMode.MULTI)
    children = (returned(planned.children[0]), failure(planned.children[1], deterministic=True),
                rounds.ReviewChildPinDeferred(planned.children[2], "pin-failed"),
                returned(planned.children[3], result=rounds.ReviewChildCancelled(reason, "cancel:verified")))
    outcome = finalized(facts(planned, children=children))
    assert outcome.disposition == execution.ReviewRoundCancelled(reason, "complete-closure")
    assert (project(outcome, pins=3).failures, project(outcome, pins=3).pin_deferrals) == (2, 3)


def test_shutdown_cancellation_is_selected_consistently_when_children_have_both_reasons():
    planned = plan(mode=ReviewMode.MULTI)
    children = tuple(returned(child, result=rounds.ReviewChildCancelled(
        execution.ReviewCancellationReason.OPERATOR if index % 2 else execution.ReviewCancellationReason.SHUTDOWN,
        f"cancel:{index}")) for index, child in enumerate(planned.children))
    assert finalized(facts(planned, children=children)).disposition.reason is execution.ReviewCancellationReason.SHUTDOWN


@pytest.mark.parametrize("at", [0, 1, 3])
def test_pin_precedes_sibling_death_and_counts_only_the_outer_pin_at_sixth_deferral(at):
    planned = plan(mode=ReviewMode.MULTI)
    children = tuple(rounds.ReviewChildPinDeferred(child, f"pin:{index}") if index == at
                     else failure(child, deterministic=True) for index, child in enumerate(planned.children))
    outcome = finalized(facts(planned, children=children))
    assert outcome.disposition == execution.ReviewPinDeferred("complete-closure")
    state = project(outcome, failures=2, pins=5)
    assert state.failures == 3 and state.pin_deferrals == 6


@pytest.mark.parametrize("ceiling,expected", [(0, 3), (1, 2), (5, 5), (2**63 - 1, 2**63 - 1)])
def test_deterministic_multi_failure_saturates_once_at_captured_ceiling_or_adds_one_when_disabled(ceiling, expected):
    planned = plan(mode=ReviewMode.MULTI, ceiling=ceiling)
    observed = facts(planned, children=tuple(failure(child, deterministic=True) for child in planned.children))
    outcome = finalized(observed)
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome).failures == expected
    if ceiling:
        assert outcome.disposition.debit == execution.SaturateReviewFailures(ceiling, "complete-closure")
    else:
        assert isinstance(outcome.disposition.debit, execution.AddReviewStrike)


def test_paid_gate_with_only_refused_or_released_siblings_still_charges_one_outer_failure():
    planned = plan(mode=ReviewMode.MULTI)
    children = (returned(planned.children[0]), rounds.ReviewChildRefused(planned.children[1], "budget:1"),
                rounds.ReviewChildReleased(planned.children[2], "unlaunched:2", "release:2"),
                rounds.ReviewChildRefused(planned.children[3], "budget:3"))
    outcome = finalized(facts(planned, children=children))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert outcome.disposition.launched == (children[0].launched,)
    assert project(outcome).failures == 3


@pytest.mark.parametrize("mode", [ReviewMode.SINGLE, ReviewMode.DELEGATED])
@pytest.mark.parametrize("handle,reuse,expected", [
    ("verified-handle", rounds.ReviewSessionReuse.REUSABLE, execution.ReplaceReviewSession),
    (None, rounds.ReviewSessionReuse.REUSABLE, execution.ClearReviewSession),
    ("verified-handle", rounds.ReviewSessionReuse.FRESH, execution.ClearReviewSession),
])
def test_only_productive_normal_reviewer_updates_the_session(mode, handle, reuse, expected):
    command = submission()
    planned = plan(mode=mode, submissions=(command,), reuse=reuse)
    reviewer = returned(planned.children[0], handle=handle)
    outcome = finalized(facts(planned, children=(reviewer,), submissions=(confirmed(command),)))
    assert isinstance(outcome.session, expected)
    if isinstance(outcome.session, execution.ReplaceReviewSession):
        assert outcome.session.session.handle == handle
    refused_outcome = finalized(facts(planned, children=(reviewer,), submissions=(refused(command),)))
    assert isinstance(refused_outcome.session, execution.PreserveReviewSession)
    assert isinstance(refused_outcome.display, execution.PreserveDisplayedRound)
    assert project(refused_outcome).failures == 3


def test_failed_normal_reviewer_cannot_update_session_even_with_a_confirmed_submission():
    command = submission()
    planned = plan(submissions=(command,))
    outcome = finalized(facts(planned, children=(failure(planned.children[0]),), submissions=(confirmed(command),)))
    assert isinstance(outcome.session, execution.PreserveReviewSession)
    assert isinstance(outcome.disposition, execution.PaidReviewProgress)


def test_paid_gate_short_circuit_preserves_session_and_advances_displayed_round():
    command = submission()
    planned = plan(mode=ReviewMode.MULTI, submissions=(command,))
    planned = replace(planned, stages=planned.stages[:1])
    outcome = finalized(facts(planned, submissions=(confirmed(command),)))
    assert isinstance(outcome.session, execution.PreserveReviewSession)
    assert outcome.display.number == 4 and project(outcome).failures == 0


def test_copied_verdict_is_free_productive_progress_and_refusal_can_fall_back_to_paid_review():
    copy = submission(rounds.ReviewSubmissionKind.COPIED, "copy")
    free = plan(empty=True, submissions=(copy,))
    outcome = finalized(facts(free, submissions=(confirmed(copy),)))
    assert isinstance(outcome.disposition, execution.CopiedReviewProgress)
    assert isinstance(outcome.session, execution.PreserveReviewSession)
    assert isinstance(outcome.display, execution.PreserveDisplayedRound)
    assert project(outcome, pins=6).failures == project(outcome, pins=6).pin_deferrals == 0
    open_fallback = facts(free, submissions=(refused(copy),), planning=rounds.ReviewPlanningOpen())
    assert rounds.finalize_review_round(open_fallback).reasons == frozenset({rounds.ReviewRoundWait.PLANNING})
    paid = submission(command="fallback")
    fallback = plan(submissions=(copy, paid))
    resolved = finalized(facts(fallback, submissions=(refused(copy), confirmed(paid))))
    assert isinstance(resolved.disposition, execution.PaidReviewProgress)


@pytest.mark.parametrize("kind,empty", [(rounds.ReviewSubmissionKind.COPIED, False), (rounds.ReviewSubmissionKind.PAID, True)])
def test_confirmed_submission_cannot_lie_about_whether_the_round_launched_work(kind, empty):
    command = submission(kind)
    with pytest.raises(ValueError, match="launch"):
        finalized(facts(plan(empty=empty, submissions=(command,)), submissions=(confirmed(command),)))


def test_two_confirmed_commands_cannot_establish_one_productive_outer_round():
    commands = (submission(command="first"), submission(command="second"))
    with pytest.raises(ValueError, match="multiple productive"):
        finalized(facts(plan(submissions=commands), submissions=tuple(confirmed(command) for command in commands)))


@pytest.mark.parametrize("kind", list(execution.ReviewPreparationKind))
def test_verified_preparation_failure_is_noncharging_and_requests_bounded_evidence_scheduling(kind):
    failure_fact = execution.ReviewPreparationFailure(kind, "preparation:verified")
    conclusion = execution.ReviewPreparationDeferred(failure_fact)
    observed = facts(plan(empty=True), planning=rounds.ReviewPlanningSealed("seal", conclusion))
    result = rounds.finalize_review_round(observed)
    assert isinstance(result, rounds.DeferredReviewPreparation)
    assert result.failures == (failure_fact,) and result.outcome.disposition == conclusion
    state = project(result.outcome, failures=3, pins=2)
    assert (state.failures, state.pin_deferrals) == (3, 2)


def test_mixed_settled_without_launch_release_and_refusal_retains_full_closure_without_a_paid_failure():
    planned = plan(mode=ReviewMode.MULTI)
    preparation = execution.ReviewPreparationFailure(execution.ReviewPreparationKind.RUNTIME, "runtime:unavailable")
    children = (rounds.ReviewChildSettledWithoutLaunch(planned.children[0], "settled:0", "synthetic:0", "no-launch:0", preparation),
                rounds.ReviewChildReleased(planned.children[1], "released:1", "release:1"),
                rounds.ReviewChildRefused(planned.children[2], "budget:2"),
                rounds.ReviewChildSettledWithoutLaunch(planned.children[3], "settled:3", "synthetic:3", "no-launch:3", preparation))
    for delivery in permutations(children):
        result = rounds.finalize_review_round(facts(planned, children=delivery))
        assert isinstance(result, rounds.DeferredReviewPreparation)
        assert result.outcome.disposition == execution.ReviewUnlaunchedRound("complete-closure")
        assert result.failures == (preparation,)
        assert project(result.outcome).failures == 2


@pytest.mark.parametrize("override,expected", [
    ("none", execution.ReviewRoundFailure), ("pin", execution.ReviewPinDeferred),
    ("cancel", execution.ReviewRoundCancelled),
])
def test_paid_work_and_preparation_failure_keep_the_bounded_schedule_without_an_extra_strike(override, expected):
    planned = plan(mode=ReviewMode.MULTI)
    preparation = execution.ReviewPreparationFailure(execution.ReviewPreparationKind.RUNTIME, "runtime:unavailable")
    children = [returned(planned.children[0]),
                rounds.ReviewChildSettledWithoutLaunch(planned.children[1], "settled:1", "synthetic:1", "no-launch:1", preparation),
                rounds.ReviewChildRefused(planned.children[2], "budget:2"),
                failure(planned.children[3])]
    if override == "pin":
        children[3] = rounds.ReviewChildPinDeferred(planned.children[3], "pin:3")
    elif override == "cancel":
        children[3] = returned(planned.children[3], result=rounds.ReviewChildCancelled(
            execution.ReviewCancellationReason.SHUTDOWN, "shutdown:3"))
    result = rounds.finalize_review_round(facts(planned, children=tuple(children)))
    assert isinstance(result, rounds.DeferredReviewPreparation)
    assert isinstance(result.outcome.disposition, expected)
    assert result.failures == (preparation,)
    state = project(result.outcome, failures=2, pins=5)
    assert state.failures == (2 if override == "cancel" else 3)
    assert state.pin_deferrals == (6 if override == "pin" else 5)


def test_confirmed_progress_with_preparation_failed_sibling_does_not_open_a_new_preparation_hold():
    command = submission()
    planned = plan(mode=ReviewMode.MULTI, submissions=(command,))
    preparation = execution.ReviewPreparationFailure(execution.ReviewPreparationKind.RUNTIME, "runtime:unavailable")
    children = (returned(planned.children[0]),
                rounds.ReviewChildSettledWithoutLaunch(planned.children[1], "settled:1", "synthetic:1", "no-launch:1", preparation),
                failure(planned.children[2]), returned(planned.children[3]))
    result = rounds.finalize_review_round(facts(planned, children=children, submissions=(confirmed(command),)))
    assert isinstance(result, rounds.FinalizedReviewRound)
    assert isinstance(result.outcome.disposition, execution.PaidReviewProgress)
    assert project(result.outcome, failures=4).failures == 0


@pytest.mark.parametrize("evidence", ["refused", "released", "mixed"])
def test_all_unlaunched_children_preserve_allowance_with_compound_closure_evidence(evidence):
    planned = plan(mode=ReviewMode.MULTI)
    children = tuple(rounds.ReviewChildReleased(child, f"run:{n}", f"release:{n}")
                     if evidence == "released" or (evidence == "mixed" and n % 2)
                     else rounds.ReviewChildRefused(child, f"refused:{n}")
                     for n, child in enumerate(planned.children))
    outcome = finalized(facts(planned, children=children))
    assert outcome.disposition == execution.ReviewUnlaunchedRound("complete-closure")
    assert project(outcome).failures == 2


def test_abandonment_requires_explicit_late_admission_fence_and_each_unadmitted_child_fact():
    planned = plan(mode=ReviewMode.MULTI)
    abandoned = rounds.ReviewParentAbandoned("parent:dead", "late-admission:fenced")
    children = tuple(rounds.ReviewChildNotAdmitted(child, "parent:dead") for child in planned.children)
    outcome = finalized(facts(planned, children=children, planning=abandoned))
    assert outcome.disposition == execution.ReviewRoundCancelled(execution.ReviewCancellationReason.UNPAID_ABANDONMENT,
                                                                 "complete-closure")
    assert project(outcome).failures == 2
    assert rounds.finalize_review_round(facts(planned, children=children[:-1], planning=abandoned)).reasons == frozenset({rounds.ReviewRoundWait.CHILDREN})
    with pytest.raises(ValueError):
        rounds.ReviewParentAbandoned("parent:dead", "")


def test_post_seal_parent_loss_retains_completed_decision_and_exact_absence_proof():
    planned = plan()
    sealed = rounds.ReviewPlanningSealed("seal:completed", rounds.ReviewPlanningComplete())
    abandoned = rounds.ReviewSealedParentAbandoned("parent:dead", "late:fenced", sealed)
    child = rounds.ReviewChildNotAdmitted(planned.children[0], abandoned.abandonment_receipt)

    outcome = finalized(facts(planned, children=(child,), planning=abandoned))

    assert outcome.disposition == execution.ReviewRoundCancelled(
        execution.ReviewCancellationReason.UNPAID_ABANDONMENT, "complete-closure",
    )
    with pytest.raises(ValueError, match="completed planning"):
        rounds.ReviewSealedParentAbandoned(
            "parent:dead", "late:fenced",
            rounds.ReviewPlanningSealed("seal:refused", rounds.ReviewPlanningRefused("refused")),
        )
    with pytest.raises(ValueError, match="exact abandoned parent"):
        facts(planned, children=(rounds.ReviewChildNotAdmitted(planned.children[0], "another-parent"),),
              planning=abandoned)


def test_paid_gate_before_parent_death_is_one_failure_only_after_proven_abandonment():
    planned = plan(mode=ReviewMode.MULTI)
    planned = replace(planned, stages=planned.stages[:1])
    assert isinstance(rounds.finalize_review_round(facts(planned, planning=rounds.ReviewPlanningOpen())),
                      rounds.ReviewRoundAwaitingEvidence)
    outcome = finalized(facts(planned, planning=rounds.ReviewParentAbandoned("parent:dead", "late:fenced")))
    assert isinstance(outcome.disposition, execution.ReviewRoundFailure)
    assert project(outcome).failures == 3


def test_empty_seal_requires_an_explicit_unpaid_reason_and_cannot_fake_abandonment():
    with pytest.raises(ValueError, match="empty finished"):
        finalized(facts(plan(empty=True)))
    outcome = finalized(facts(plan(empty=True), planning=rounds.ReviewPlanningSealed(
        "seal", rounds.ReviewPlanningRefused("admission:refused"))))
    assert outcome.disposition == execution.ReviewAdmissionRefused("admission:refused")
    with pytest.raises(ValueError, match="abandonment"):
        rounds.ReviewPlanningSealed("seal", execution.ReviewRoundCancelled(
            execution.ReviewCancellationReason.UNPAID_ABANDONMENT, "unfenced"))


@pytest.mark.parametrize("bad_stages", ["dimensions_first", "validator_before_dimensions", "reviewer_in_multi", "two_gates", "two_dimensions"])
def test_multi_stage_plan_rejects_impossible_mode_or_stage_order(bad_stages):
    planned = plan(mode=ReviewMode.MULTI)
    stages = {
        "dimensions_first": (planned.stages[1],),
        "validator_before_dimensions": (planned.stages[0], planned.stages[2], planned.stages[1]),
        "reviewer_in_multi": (planned.stages[0], stage(planned.round, REVIEWER)),
        "two_gates": (planned.stages[0], stage(planned.round, GATE, name="gate-two")),
        "two_dimensions": (planned.stages[0], planned.stages[1], stage(planned.round, DIMENSION, name="dimensions-two")),
    }[bad_stages]
    with pytest.raises(ValueError):
        replace(planned, stages=stages)


@pytest.mark.parametrize("mode", [ReviewMode.SINGLE, ReviewMode.DELEGATED])
def test_normal_review_plan_cannot_admit_auxiliary_or_multiple_reviewer_stages(mode):
    planned = plan(mode=mode)
    for stages in ((stage(planned.round, GATE),), (planned.stages[0], stage(planned.round, REVIEWER, name="second"))):
        with pytest.raises(ValueError):
            replace(planned, stages=stages)


@pytest.mark.parametrize("bad", ["foreign_round", "foreign_stage", "wrong_kind", "duplicate_child", "empty", "two_reviewers"])
def test_stage_requires_distinct_matching_children_and_singleton_reviewer_or_gate(bad):
    round_ = identity()
    base = stage(round_, REVIEWER)
    child = base.children[0]
    children = {
        "foreign_round": (child, replace(child, round=identity(2), child_id="another")),
        "foreign_stage": (replace(child, stage_id="other"),),
        "wrong_kind": (replace(child, kind=GATE),),
        "duplicate_child": (child, child), "empty": (),
        "two_reviewers": (child, replace(child, child_id="another")),
    }[bad]
    with pytest.raises(ValueError):
        replace(base, children=children)


@pytest.mark.parametrize("corruption", ["foreign_child", "duplicate_child", "foreign_submission", "duplicate_submission", "same_run_twice"])
def test_finalization_facts_reject_evidence_outside_or_conflicting_with_the_plan(corruption):
    command = submission()
    planned = plan(mode=ReviewMode.MULTI, submissions=(command,))
    observed = facts(planned, submissions=(refused(command),))
    if corruption == "foreign_child":
        children = (returned(replace(planned.children[0], round=identity(2))),) + observed.children[1:]
        change = {"children": children}
    elif corruption == "duplicate_child":
        change = {"children": observed.children + observed.children[:1]}
    elif corruption == "foreign_submission":
        change = {"submissions": (confirmed(submission(command="unknown")),)}
    elif corruption == "duplicate_submission":
        change = {"submissions": observed.submissions + observed.submissions}
    else:
        first, second = observed.children[:2]
        assert isinstance(first, rounds.ReviewChildReturned) and isinstance(second, rounds.ReviewChildReturned)
        duplicate = replace(second, launched=replace(second.launched, run_id=first.launched.run_id))
        change = {"children": (first, duplicate) + observed.children[2:]}
    with pytest.raises(ValueError):
        replace(observed, **change)


def test_no_admission_requires_the_same_abandoned_parent_not_a_missing_child_guess():
    planned = plan()
    child = rounds.ReviewChildNotAdmitted(planned.children[0], "parent:one")
    for planning in (rounds.ReviewPlanningOpen(), rounds.ReviewPlanningSealed("seal", rounds.ReviewPlanningComplete()),
                     rounds.ReviewParentAbandoned("parent:two", "fence")):
        with pytest.raises(ValueError, match="exact abandoned parent"):
            facts(planned, children=(child,), planning=planning)


def test_session_evidence_must_match_exact_launched_reviewer_and_captured_runtime():
    planned = plan()
    child = returned(planned.children[0], handle="session")
    other_run = replace(child.launched, run_id="other-run")
    with pytest.raises(ValueError, match="exact launched"):
        replace(child, launched=other_run)
    other_runtime = replace(child, result=rounds.ReviewChildSucceeded(replace(child.result.session, runtime=RuntimeName.GLM)))
    with pytest.raises(ValueError, match="runtime"):
        facts(planned, children=(other_runtime,))


@pytest.mark.parametrize("bad", [None, True, -1, 2**63, "5"])
def test_round_plan_rejects_malformed_numeric_authority(bad):
    with pytest.raises(ValueError):
        replace(plan(), failure_ceiling=bad)
    with pytest.raises(ValueError):
        replace(plan(), displayed_round=bad)


@pytest.mark.parametrize("field,bad", [("head_sha", "bad"), ("head_sha", "a" * 100000), ("mode", "single"),
                                      ("runtime", "claude"), ("session_reuse", None), ("stages", []), ("submissions", [None])])
def test_round_plan_rejects_untyped_or_malformed_inputs(field, bad):
    with pytest.raises(ValueError):
        replace(plan(), **{field: bad})


def test_stage_child_and_submission_limits_are_enforced_across_the_whole_round():
    round_ = identity()
    base = plan(mode=ReviewMode.MULTI)
    at_limit = (stage(round_, GATE), stage(round_, DIMENSION, execution.MAX_ROUND_CHILDREN - 1))
    assert len(replace(base, stages=at_limit).children) == execution.MAX_ROUND_CHILDREN
    with pytest.raises(ValueError):
        replace(base, stages=at_limit + (stage(round_, VALIDATOR),))
    too_many_stages = base.stages[:2] + tuple(stage(round_, VALIDATOR, name=f"validate:{n}")
                                            for n in range(rounds.MAX_REVIEW_STAGES))
    with pytest.raises(ValueError):
        replace(base, stages=too_many_stages)
    with pytest.raises(ValueError):
        replace(base, submissions=tuple(submission(command=f"command:{n}") for n in range(rounds.MAX_ROUND_SUBMISSIONS + 1)))


@pytest.mark.parametrize("reason", [ExitReason.OK, ExitReason.OPERATOR_CANCELLED,
                                     ExitReason.TRANSPORT_DEATH, ExitReason.KILLED_EXTERNALLY,
                                     ExitReason.RUNTIME_UNAVAILABLE, ExitReason.BUDGET_EXHAUSTED,
                                     "transport_death", None])
def test_failed_child_cannot_claim_success_or_operator_cancellation(reason):
    with pytest.raises(ValueError):
        rounds.ReviewChildFailed(reason)


@pytest.mark.parametrize("bad", [None, "", " ", "nul\x00", "x" * 513])
def test_unlaunched_and_abandonment_provenance_requires_real_bounded_receipt_references(bad):
    child = plan().children[0]
    preparation = execution.ReviewPreparationFailure(execution.ReviewPreparationKind.RUNTIME, "verified")
    for construct in (lambda: rounds.ReviewParentAbandoned("abandoned", bad),
                      lambda: rounds.ReviewChildSettledWithoutLaunch(child, "run", "settlement", bad, preparation),
                      lambda: rounds.ReviewChildReleased(child, "run", bad),
                      lambda: rounds.ReviewChildRefused(child, bad),
                      lambda: rounds.ReviewSubmissionConfirmed(bad)):
        with pytest.raises(ValueError):
            construct()
