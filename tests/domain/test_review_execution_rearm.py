"""Execution renewal changes only the selected allowance and its source receipt."""

from dataclasses import replace

import pytest

from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.review_execution import (
    CurrentReviewExecution, NoReviewSession, ReviewExecutionOrigin, ReviewExecutionOriginKind,
    ReusableReviewSession, RenewedReviewExecutionOriginReceipt, RenewedReviewSessionSource,
    ReviewExecutionCheckpoint, renew_current_review_execution,
)
from issue_agent.domain.review_workflow import (
    BoundReviewSubmission, CountedReview, CountingRule, ReviewedBaseline, ReviewExecutionRenewalGrant,
    ReviewRejectionCeiling, ReviewRejectionGrant, ReviewVerdictHold, ReviewVerdictReleaseGrant,
)
from issue_agent.domain.review_workflow_policy import (
    ReviewSubmissionReceipt, cancel_review_rearm, complete_review_rearm, continue_review_rearm,
    request_review_rearm, review_execution_ceiling_gate_id, review_verdict_release_gate_id,
)
from issue_agent.domain.contracts import CommandId, CommandKind
from issue_agent.domain.labels import Label
from issue_agent.domain.review import UNPRODUCTIVE_ROUND_HOLD
from issue_agent.domain.workflow import (
    CancelRearm, ContinueGrantedRearm, KeepGuidance, NoAuthorizedRemediation, OpenGate, OperatorActor,
    RequestReviewExecutionRenewal, RequestReviewVerdictRelease, WorkflowChanged, WorkflowRejected, WorkflowRejection,
)
from issue_agent.domain.workflow_identity import WorkflowBinding, WorkflowFence, WorkflowKey, PullRequestRef
from tests.domain.test_review_preflight import NOW, active, exhausted

ACTOR = OperatorActor("operator:test", "test")
RULE = CountingRule("review-bot")


def renewal_request(snapshot, observed, request_id="renew:1"):
    execution = snapshot.execution
    return RequestReviewExecutionRenewal(request_id, snapshot.revision,
        review_execution_ceiling_gate_id(snapshot, observed.head_sha, 5), KeepGuidance(),
        execution.binding.execution_epoch, execution.binding.execution_episode, expected_binding=snapshot.binding)


def accept_fixed(snapshot, command, observed, *, holds=(), receipt=None, prior=None):
    return request_review_rearm(snapshot, command, ACTOR, observed, now=NOW, active_reviews=(),
        evidence_id="observation:fixed", trigger_label=Label.AI_PR_OPEN, executor_identity="review:owner",
        external_holds=holds, execution_ceiling=5, rearm_rule=RULE, verdict_receipt=receipt, prior_request=prior)


def next_binding(snapshot):
    return WorkflowBinding(
        WorkflowFence(snapshot.revision.fence.key, snapshot.revision.fence.generation + 1),
        snapshot.authority, snapshot.episode.episode_id,
    )


@pytest.mark.parametrize("handle", [None, "compatible-continuation"])
def test_renewal_preserves_handle_and_display_but_resets_only_execution_allowance(handle):
    snapshot, _ = exhausted(*active())
    old = snapshot.execution
    assert isinstance(old, CurrentReviewExecution)
    origin = ReviewExecutionOrigin(ReviewExecutionOriginKind.RENEWED, "origin:renewed")
    session = (NoReviewSession() if handle is None else ReusableReviewSession(
        RuntimeName.CLAUDE, handle, RenewedReviewSessionSource(origin.evidence_id)))
    state = replace(old.state, pin_deferrals=3, displayed_round=7, session=session)
    old = replace(old, origin=origin, baseline=replace(old.baseline, state=state), state=state)
    after, receipt = renew_current_review_execution(old, snapshot.revision, next_binding(snapshot), "renew:1")
    assert after.state.failures == 0 and after.state.pin_deferrals == 0
    assert after.state.displayed_round == 7
    assert after.binding.execution_epoch == old.binding.execution_epoch
    assert after.binding.execution_episode != old.binding.execution_episode
    assert after.binding.rejection.episode_id == snapshot.episode.episode_id
    assert after.last_sequence == after.baseline.through_sequence == 0
    assert after.state.session_sequence == after.state.display_sequence == after.state.productive_sequence == 0
    if handle is None:
        assert after.state.session == NoReviewSession()
    else:
        assert after.state.session == ReusableReviewSession(
            RuntimeName.CLAUDE, handle, RenewedReviewSessionSource(receipt.origin.evidence_id))
    assert old.state.failures == 5 and old.state.pin_deferrals == 3


def test_renewal_refuses_an_open_current_suffix_without_resetting_it():
    snapshot, _ = active()
    assert isinstance(snapshot.execution, CurrentReviewExecution)
    old = replace(snapshot.execution, last_sequence=1)
    with pytest.raises(ValueError, match="terminal current suffix"):
        renew_current_review_execution(old, snapshot.revision, next_binding(snapshot), "renew:1")
    assert old.last_sequence == 1 and old.baseline.through_sequence == 0


@pytest.mark.parametrize("epoch,episode", [(0, "episode"), (True, "episode"), (1, ""), (1, "x" * 513)])
def test_fixed_renewal_request_refuses_malformed_captured_execution(epoch, episode):
    snapshot, _ = active()
    with pytest.raises(ValueError):
        RequestReviewExecutionRenewal("renew:1", snapshot.revision, "gate:1", KeepGuidance(), epoch, episode,
                                     expected_binding=snapshot.binding)


def test_execution_renewal_grant_refuses_reusing_its_old_episode():
    with pytest.raises(ValueError, match="new episode"):
        ReviewExecutionRenewalGrant(1, "same", "same", "origin:1", "renew:1")


@pytest.mark.parametrize("stopped", [False, True])
def test_snapshot_refuses_a_renewal_grant_naming_another_origin(stopped):
    snapshot, selection = exhausted(*active())
    result = accept_fixed(snapshot, renewal_request(snapshot, selection.observation), selection.observation)
    after = result.snapshot
    if stopped:
        after = cancel_review_rearm(after,
            CancelRearm("cancel:bad-origin", after.revision, after.intent.request_id, "stop"), ACTOR).snapshot
    with pytest.raises(ValueError, match="exact origin"):
        replace(after, intent=replace(after.intent, grant=replace(after.intent.grant, origin_evidence_id="other:origin")))


def test_snapshot_refuses_a_selected_verdict_grant_bound_to_another_target():
    snapshot, observed, receipt = verdict_snapshot()
    request = RequestReviewVerdictRelease("release:bad-target", snapshot.revision,
        review_verdict_release_gate_id(snapshot, receipt.submission), KeepGuidance(),
        snapshot.execution.binding.execution_epoch, snapshot.execution.binding.execution_episode,
        receipt.submission.command_id, expected_binding=snapshot.binding)
    after = accept_fixed(snapshot, request, observed.observation, receipt=receipt).snapshot
    foreign = replace(receipt.submission.binding, fence=WorkflowFence(WorkflowKey("o/r", PullRequestRef(999)), 0))
    with pytest.raises(ValueError, match="this rejection component"):
        replace(after, intent=replace(after.intent,
            grant=replace(after.intent.grant, submission=replace(receipt.submission, binding=foreign))))


def test_snapshot_refuses_a_renewal_grant_with_an_initialized_origin_kind():
    from issue_agent.domain.review_execution import ReviewExecutionOriginKind

    snapshot, selection = exhausted(*active())
    after = accept_fixed(snapshot, renewal_request(snapshot, selection.observation), selection.observation).snapshot
    wrong = replace(after.execution, origin=replace(after.execution.origin, kind=ReviewExecutionOriginKind.INITIALIZED))
    with pytest.raises(ValueError, match="exact origin"):
        replace(after, execution=wrong)


def test_execution_renewal_preserves_rejection_and_external_holds_with_one_projection():
    snapshot, selection = exhausted(*active())
    cause = ReviewRejectionCeiling(snapshot.episode.episode_id, selection.observation.head_sha, RULE.review_bot,
        (CountedReview("review:0", None), CountedReview("review:1", None)), 3, ())
    rejection = OpenGate("rejection:held", 0, cause, NoAuthorizedRemediation("rejection allowance stays exhausted"))
    snapshot = replace(snapshot, gates=(rejection,))
    result = accept_fixed(snapshot, renewal_request(snapshot, selection.observation), selection.observation,
                          holds=(UNPRODUCTIVE_ROUND_HOLD, Label.AI_PARKED.value))
    assert isinstance(result, WorkflowChanged)
    after = result.snapshot
    assert after.gates[0] == rejection and after.episode.episode_id == snapshot.episode.episode_id
    assert after.execution.state.failures == 0
    assert after.intent.authorized == after.intent.dismissals == ()
    assert len(result.commands) == 1 and result.commands[0].kind is CommandKind.RELABEL
    assert after.intent.projection.remove is None
    assert result.audit.preflight.external_holds == (Label.AI_PARKED.value,)
    completed = complete_review_rearm(after,
        replace(selection.observation, labels=frozenset({Label.NEEDS_HUMAN_REVIEW, Label.AI_PARKED})),
        verified_commands=after.intent.required_commands, evidence_ids=("readback:1",), now=NOW,
        ceiling=3, trigger_label=Label.AI_PR_OPEN, external_holds=(Label.AI_PARKED.value,))
    assert isinstance(completed, WorkflowChanged)
    assert completed.snapshot.gates == (rejection,)
    assert completed.snapshot.intent.grant == after.intent.grant


def test_cancel_and_continue_preserve_the_same_execution_grant_without_renewing_again():
    snapshot, selection = exhausted(*active())
    result = accept_fixed(snapshot, renewal_request(snapshot, selection.observation), selection.observation)
    assert isinstance(result, WorkflowChanged)
    first = result.snapshot
    cancelled = cancel_review_rearm(first,
        CancelRearm("cancel:1", first.revision, first.intent.request_id, "pause this projection"), ACTOR)
    assert isinstance(cancelled, WorkflowChanged)
    stopped = cancelled.snapshot
    assert stopped.intent.grant == first.intent.grant
    continued = continue_review_rearm(stopped,
        ContinueGrantedRearm("continue:1", stopped.revision, stopped.intent.request_id), ACTOR,
        selection.observation, now=NOW, active_reviews=(), evidence_id="read:continue",
        trigger_label=Label.AI_PR_OPEN, executor_identity="review:owner")
    assert isinstance(continued, WorkflowChanged)
    after = continued.snapshot
    assert after.intent.grant == first.intent.grant
    assert after.execution.origin == first.execution.origin
    assert after.execution.binding.execution_episode == first.execution.binding.execution_episode
    assert after.execution.state.failures == 0 and after.intent.dismissals == ()


def test_exact_renewal_request_is_inert_and_changed_input_conflicts():
    snapshot, selection = exhausted(*active())
    request = renewal_request(snapshot, selection.observation)
    result = accept_fixed(snapshot, request, selection.observation)
    assert isinstance(result, WorkflowChanged)
    replay = accept_fixed(result.snapshot, request, selection.observation, prior=request)
    assert replay.snapshot == result.snapshot
    conflict = accept_fixed(result.snapshot, replace(request, execution_episode="other"),
                            selection.observation, prior=request)
    assert isinstance(conflict, WorkflowRejected)
    assert conflict.code is WorkflowRejection.INVALID_SCOPE
    pending = accept_fixed(result.snapshot,
        renewal_request(result.snapshot, selection.observation, "renew:second"), selection.observation)
    assert isinstance(pending, WorkflowRejected) and pending.code is WorkflowRejection.REARM_PENDING


def verdict_snapshot():
    snapshot, selection = exhausted(*active())
    receipt = ReviewSubmissionReceipt(BoundReviewSubmission(CommandId("verdict:confirmed"), snapshot.binding),
        selection.observation.head_sha, "b" * 64, "receipt:confirmed", escalated=True)
    baseline = ReviewedBaseline(receipt.head_sha, receipt.patch_id, snapshot.revision.fence.generation,
                                receipt.submission)
    return replace(snapshot, episode=replace(snapshot.episode, continuation=baseline)), selection, receipt


def verdict_request(snapshot, receipt, request_id="release:1"):
    execution = snapshot.execution
    return RequestReviewVerdictRelease(request_id, snapshot.revision,
        review_verdict_release_gate_id(snapshot, receipt.submission), KeepGuidance(),
        execution.binding.execution_epoch, execution.binding.execution_episode, receipt.submission.command_id,
        expected_binding=snapshot.binding)


@pytest.mark.parametrize("escalated", [False, True])
def test_selected_verdict_release_preserves_execution_allowance_and_dismisses_nothing(escalated):
    snapshot, selection, receipt = verdict_snapshot()
    receipt = replace(receipt, escalated=escalated)
    result = accept_fixed(snapshot, verdict_request(snapshot, receipt), selection.observation,
                          holds=(UNPRODUCTIVE_ROUND_HOLD,), receipt=receipt)
    assert isinstance(result, WorkflowChanged)
    after = result.snapshot
    assert isinstance(after.intent.grant, ReviewVerdictReleaseGrant)
    assert after.execution.state.failures == snapshot.execution.state.failures == 5
    assert after.execution.binding.execution_episode == snapshot.execution.binding.execution_episode
    assert after.intent.authorized == after.intent.dismissals == ()
    assert len(result.commands) == 1 and result.commands[0].kind is CommandKind.RELABEL
    assert after.intent.projection.remove is None


def test_execution_renewal_retains_confirmed_verdict_through_cancel_continue_and_exact_release():
    snapshot, selection, receipt = verdict_snapshot()
    renewed = accept_fixed(snapshot, renewal_request(snapshot, selection.observation), selection.observation,
                           receipt=receipt)
    assert isinstance(renewed, WorkflowChanged)
    state = renewed.snapshot
    retained = next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, ReviewVerdictHold))
    assert retained.cause.submission_command_id == receipt.submission.command_id
    assert state.intent.projection.remove is None
    stop = cancel_review_rearm(state, CancelRearm("cancel:1", state.revision, state.intent.request_id, "stop"), ACTOR)
    assert isinstance(stop, WorkflowChanged)
    continued = continue_review_rearm(stop.snapshot,
        ContinueGrantedRearm("continue:1", stop.snapshot.revision, stop.snapshot.intent.request_id), ACTOR,
        selection.observation, now=NOW, active_reviews=(), evidence_id="read:continue",
        trigger_label=Label.AI_PR_OPEN, executor_identity="review:owner")
    assert isinstance(continued, WorkflowChanged)
    state = continued.snapshot
    assert retained in state.gates and state.intent.grant == renewed.snapshot.intent.grant
    completed = complete_review_rearm(state,
        replace(selection.observation, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})),
        verified_commands=state.intent.required_commands, evidence_ids=("read:complete",), now=NOW,
        ceiling=3, trigger_label=Label.AI_PR_OPEN)
    assert isinstance(completed, WorkflowChanged)
    state = completed.snapshot
    released = accept_fixed(state, verdict_request(state, receipt), selection.observation, receipt=receipt)
    assert isinstance(released, WorkflowChanged)
    assert not any(isinstance(g, OpenGate) and g == retained for g in released.snapshot.gates)
    assert released.snapshot.execution.state.failures == 0
    assert released.snapshot.execution.origin == renewed.snapshot.execution.origin
    refused = accept_fixed(state, verdict_request(state, receipt), selection.observation,
                           receipt=None)
    assert isinstance(refused, WorkflowRejected)
