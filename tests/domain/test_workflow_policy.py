"""Behavioral contracts for resolve allowance and re-arm transitions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from issue_agent.domain.contracts import CommandId, RunId, WorkflowAttemptIdentity
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason, ResolveFollowup
from issue_agent.domain.workflow import (
    AuthoredResolve, FreshResolve, ResolveAdmission, ResolveAttemptResult, ResolveSession, ResolveStage,
    CancelRearm, CancelledRearm, ClearGuidance, CompletedRearm, ContinueGrantedRearm,
    EvidenceUnavailable, HeldProjection, IssueAbsent, IssueUnavailable, KeepGuidance,
    LinkedPrPresent, LinkedPrUnavailable, NoAuthorizedRemediation, NoLinkedPr,
    ObservedIssue, OpenGate, OperatorActor, PendingRearm, RearmingGate,
    AuthorizedOperatorCommand, RenewResolveAllowance, ResolveRearmAcceptance, ResolveRearmPreflight,
    RemoteOutcomeUncertain, RequestRearm, ResolveCeiling, ResolveEpisode,
    ResolveResultKind, SetGuidance, StopReason, SupersededRearm, WorkflowChanged,
    WorkflowRejected, WorkflowRejection, WorkflowSnapshot, WorkflowUnchanged,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, classify_resolve_attempt, complete_resolve_rearm,
    continue_resolve_rearm, reconcile_resolve_ceiling, record_resolve_attempt, request_resolve_rearm,
    resolve_projection_command_id, supersede_resolve_rearm,
    initial_resolve_workflow, validate_resolve_transition,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.actions import Relabel, RemoveWorktree
from issue_agent.domain.resolve_return import PendingResolveReturn, ResolveInputs, ReturnedResolve
from issue_agent.domain.workflow_policy import decide_return, defer_return_observation
from tests.fakes.workflow_workspace import workspace_ack, workspace_observation
from issue_agent.domain.resolve_workspace import PrepareResolveWorkspace

NOW = datetime(2026, 9, 14, 9, tzinfo=timezone.utc)
ACTOR = OperatorActor("operator:alice", "dashboard")


def returned_result(*, stage=ResolveStage.RESOLVE, **changes):
    state = snapshot()
    attempt = WorkflowAttemptIdentity(state.binding, RunId("returned:1"), "leg:1")
    values = dict(attempt=attempt, stage=stage, inputs=ResolveInputs("title", "body", "alice"),
                  is_error=False, status="blocked", summary="summary", needs="which API?",
                  envelope_subtype="", exit_reason=None,
                  session=ResolveSession(attempt, "session", "codex", "model"), returned_at=NOW)
    values.update(changes)
    return ReturnedResolve(**values)


def completion_observation(returned, **changes):
    values = dict(number=returned.attempt.binding.fence.key.target.number, title="title", body="body", author="alice",
                  labels=frozenset({Label.AI_WORKING}), updated_at="revision:1")
    values.update(changes)
    return ObservedIssue(returned.attempt.binding.fence.key, IssueView(**values), NOW)


@pytest.mark.parametrize("stage,status,error,expected", [
    (ResolveStage.RESOLVE, "blocked", False, ResolveResultKind.BLOCKED),
    (ResolveStage.RESOLVE, "pushed", False, ResolveResultKind.COUNTED_REQUEUE),
    (ResolveStage.RESOLVE, "", True, ResolveResultKind.COUNTED_REQUEUE),
    (ResolveStage.RESUME, "blocked", False, ResolveResultKind.BLOCKED),
    (ResolveStage.RESUME, "pushed", False, ResolveResultKind.STAGE_PROGRESSED),
    (ResolveStage.RESUME, "", True, ResolveResultKind.BLOCKED),
])
def test_return_classification_preserves_ordinary_stage_semantics(stage, status, error, expected):
    returned = returned_result(stage=stage, status=status, is_error=error)
    classified, actions = decide_return(returned, completion_observation(returned),
                                       NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert classified.kind is expected
    assert len(actions) == 1 and isinstance(actions[0], Relabel)
    assert actions[0].expected_revision == "revision:1"
    assert actions[0].workflow == returned.attempt.binding
    if status == "blocked" and not error:
        assert "which API?" in actions[0].comment
        assert actions[0].add is Label.AI_BLOCKED


@pytest.mark.parametrize("stage", [ResolveStage.RESOLVE, ResolveStage.RESUME])
@pytest.mark.parametrize("reason,kind", [(ExitReason.BUDGET_EXHAUSTED, ResolveResultKind.BUDGET_REFUSAL),
                                        (ExitReason.TRANSPORT_DEATH, ResolveResultKind.INFRA_FAILURE)])
def test_return_verified_pr_wins_projection_but_never_charges_excluded_failures(stage, reason, kind):
    returned = returned_result(stage=stage, is_error=True, exit_reason=reason)
    classified, actions = decide_return(returned, completion_observation(returned),
        LinkedPrPresent(returned.attempt.binding.fence.key, 99, "head", NOW), now=NOW)
    assert classified.kind is kind and classified.session is None
    assert [action.add for action in actions] == [Label.AI_PR_OPEN, Label.AI_PR_OPEN]
    assert actions[1].expected_revision == "head"


def test_return_budget_projection_is_one_guarded_comment_and_multilabel_operation():
    returned = returned_result(is_error=True, exit_reason=ExitReason.BUDGET_EXHAUSTED)
    classified, actions = decide_return(returned, completion_observation(returned),
                                       NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert classified.kind is ResolveResultKind.BUDGET_REFUSAL
    assert len(actions) == 1 and isinstance(actions[0], Relabel)
    assert set(actions[0].added_labels) == {Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW}
    assert "Budget review needed" in actions[0].comment


def test_return_changed_requirements_requeues_without_repeating_obsolete_question():
    returned = returned_result()
    classified, actions = decide_return(returned, completion_observation(returned, body="new requirement"),
                                       NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert classified.kind is ResolveResultKind.COUNTED_REQUEUE
    assert len(actions) == 1 and actions[0].add is Label.AI_READY and not actions[0].comment


def test_successful_closed_return_proposes_local_cleanup_without_a_failure_strike():
    returned = returned_result(status="done")
    classified, actions = decide_return(returned, completion_observation(returned, state="closed"),
                                       NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert classified.kind is ResolveResultKind.STAGE_PROGRESSED
    assert classified.session is None
    assert isinstance(actions[0], RemoveWorktree)


@pytest.mark.parametrize("label", [Label.AI_PARKED, Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_BUDGET_REVIEW])
def test_return_completion_does_not_erase_fresh_external_holds(label):
    returned = returned_result(status="pushed")
    classified, actions = decide_return(returned, completion_observation(returned, labels=frozenset({Label.AI_WORKING, label})),
                                       NoLinkedPr(returned.attempt.binding.fence.key, NOW), now=NOW)
    assert actions == () and classified.kind is ResolveResultKind.COUNTED_REQUEUE


@pytest.mark.parametrize("missing", ["issue", "linkage"])
def test_unavailable_completion_observation_is_not_absence(missing):
    returned = returned_result(status="pushed")
    key = returned.attempt.binding.fence.key
    with pytest.raises(ValueError, match="complete observations"):
        if missing == "issue":
            decide_return(returned, IssueUnavailable(key, NOW, "unavailable"), NoLinkedPr(key, NOW), now=NOW)
        else:
            decide_return(returned, completion_observation(returned), LinkedPrUnavailable(key, NOW, "unavailable"), now=NOW)


@pytest.mark.parametrize("delta", [-1, 301])
def test_return_rejects_future_or_stale_observations(delta):
    returned = returned_result()
    with pytest.raises(ValueError, match="fresh"):
        decide_return(returned, completion_observation(returned), NoLinkedPr(returned.attempt.binding.fence.key, NOW),
                      now=NOW + timedelta(seconds=delta))


def test_pending_return_backoff_is_bounded_and_never_authorizes_execution():
    pending = PendingResolveReturn(returned_result(), NOW)
    for _ in range(30):
        now = pending.next_read_at
        pending = defer_return_observation(pending, "GitHub unavailable", now=now)
        assert 5 <= (pending.next_read_at - now).total_seconds() <= 3600
    assert pending.failed_reads == 30 and pending.returned == returned_result()


def snapshot(*, number=7, failures=0, guidance=""):
    key = WorkflowKey("owner/repo", IssueRef(number))
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(key, 0), 1),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1),
        ResolveEpisode("initial", failures, guidance=guidance),
    )


def admission(state, attempt_result):
    """Captured post-admission fixture; production reads the immutable SQL receipt."""
    return ResolveAdmission(attempt_result.attempt, WorkflowRevision(attempt_result.attempt.binding.fence, state.revision.version), attempt_result.stage,
                            3 if attempt_result.stage is ResolveStage.ADJUDICATE else None, input_fingerprint="a" * 64)


def observed(state, *, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), remote_state="open"):
    return ObservedIssue(
        state.revision.fence.key,
        IssueView(state.revision.fence.key.target.number, labels=labels, state=remote_state, updated_at="revision:1"),
        NOW,
    )


def no_pr(state):
    return NoLinkedPr(state.revision.fence.key, NOW)


def result(state, *, run="run:1", followup=ResolveFollowup.RETRY, **kwargs):
    agent = AgentResult(
        workflow_attempt=WorkflowAttemptIdentity(state.binding, RunId(run), "leg:1"),
        **{"is_error": False, **kwargs},
    )
    return classify_resolve_attempt(agent, followup)


def exhausted(*, extra_gates=(), guidance=""):
    state = snapshot(failures=2, guidance=guidance)
    state = replace(state, gates=extra_gates)
    transition = record_resolve_attempt(state, result(state, summary="failed experiment"), ceiling=3, admission=admission(state, result(state, summary="failed experiment")))
    assert isinstance(transition, WorkflowChanged) and isinstance(transition.snapshot, WorkflowSnapshot)
    return transition.snapshot


def request(state, request_id="request:1", guidance=None):
    gate = next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, ResolveCeiling))
    return RequestRearm(request_id, state.revision, gate.gate_id, guidance or KeepGuidance())


def start(state, command=None, **kwargs):
    kwargs.setdefault("workspace", workspace_observation(state.binding, NOW))
    return request_resolve_rearm(
        state, command or request(state), ACTOR, observed(state), no_pr(state),
        now=NOW, active_attempts=(), **kwargs,
    )


@pytest.mark.parametrize("reason, expected", [
    (ExitReason.AGENT_ERROR, ResolveResultKind.COUNTED_REQUEUE),
    (ExitReason.WALL_TIMEOUT, ResolveResultKind.COUNTED_REQUEUE),
    (ExitReason.IDLE_TIMEOUT, ResolveResultKind.COUNTED_REQUEUE),
    (ExitReason.KILLED_EXTERNALLY, ResolveResultKind.INFRA_FAILURE),
    (ExitReason.TRANSPORT_DEATH, ResolveResultKind.INFRA_FAILURE),
    (ExitReason.RUNTIME_UNAVAILABLE, ResolveResultKind.INFRA_FAILURE),
    (ExitReason.BUDGET_EXHAUSTED, ResolveResultKind.BUDGET_REFUSAL),
])
def test_existing_failure_classes_keep_their_resolve_allowance_semantics(reason, expected):
    state = snapshot(failures=1)
    classified = result(state, exit_reason=reason, is_error=True)
    assert classified.kind is expected
    transition = record_resolve_attempt(state, classified, ceiling=3, admission=admission(state, classified))
    if expected is ResolveResultKind.COUNTED_REQUEUE:
        assert transition.snapshot.episode.failures == 2
    else:
        assert isinstance(transition, WorkflowUnchanged)


@pytest.mark.parametrize("followup", [ResolveFollowup.BLOCKED, ResolveFollowup.PR_OPENED])
def test_blocked_and_observed_pr_success_do_not_consume_resolve_allowance(followup):
    state = snapshot(failures=2)
    classified = result(state, status="blocked", followup=followup)
    transition = record_resolve_attempt(state, classified, ceiling=3, admission=admission(state, classified))
    assert transition.snapshot.episode.failures == state.episode.failures


def test_explicit_changed_requirements_requeue_counts_even_if_agent_claimed_blocked():
    state = snapshot()
    classified = result(state, status="blocked", followup=ResolveFollowup.RETRY)
    assert record_resolve_attempt(state, classified, ceiling=3, admission=admission(state, classified)).snapshot.episode.failures == 1


def test_unadmitted_result_without_harness_attempt_cannot_consume_allowance():
    assert classify_resolve_attempt(AgentResult(is_error=True), ResolveFollowup.RETRY) is None


def test_initial_workflow_identity_depends_on_target_and_epoch_not_observation_timestamp():
    state = snapshot()
    first = initial_resolve_workflow(observed(state), state.authority, now=NOW)
    later = replace(observed(state), observed_at=NOW + timedelta(seconds=1))
    assert initial_resolve_workflow(later, state.authority, now=later.observed_at) == first
    assert first.revision.fence.generation == first.revision.version == 0
    assert first.episode.failures == 0
    another = initial_resolve_workflow(observed(snapshot(number=8)), state.authority, now=NOW)
    next_epoch = initial_resolve_workflow(observed(state), replace(state.authority, epoch=2), now=NOW)
    assert len({first.episode.episode_id, another.episode.episode_id, next_epoch.episode.episode_id}) == 3


@pytest.mark.parametrize("seconds", [-1, 301])
def test_initial_workflow_rejects_future_and_stale_observations(seconds):
    state = snapshot()
    with pytest.raises(ValueError, match="stale or from the future"):
        initial_resolve_workflow(observed(state), state.authority, now=NOW + timedelta(seconds=seconds))


@pytest.mark.parametrize("unavailable", [None, "issue:7"])
def test_initial_workflow_requires_a_typed_verified_present_issue(unavailable):
    with pytest.raises(ValueError, match="present typed issue"):
        initial_resolve_workflow(unavailable, snapshot().authority, now=NOW)


@pytest.mark.parametrize("now", [None, datetime(2026, 9, 14)])
def test_initial_workflow_requires_a_valid_aware_transaction_clock(now):
    state = snapshot()
    with pytest.raises(ValueError, match="aware current time"):
        initial_resolve_workflow(observed(state), state.authority, now=now)


def test_prior_proposal_validation_rejects_arbitrary_counter_replacement():
    before = snapshot()
    proposal = record_resolve_attempt(before, result(before), ceiling=3, admission=admission(before, result(before)))
    assert isinstance(proposal, WorkflowChanged) and isinstance(proposal.snapshot, WorkflowSnapshot)
    validate_resolve_transition(before, proposal, ceiling=3, now=NOW, admission=admission(before, proposal.audit))
    forged = replace(proposal, snapshot=replace(proposal.snapshot, episode=replace(proposal.snapshot.episode, failures=10)))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW, admission=admission(before, proposal.audit))


def test_prior_proposal_validation_requires_ceiling_gate_in_same_count_transaction():
    before = snapshot(failures=2)
    proposal = record_resolve_attempt(before, result(before), ceiling=3, admission=admission(before, result(before)))
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, gates=()))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW, admission=admission(before, proposal.audit))


def test_prior_proposal_validation_preserves_unrelated_hold_during_count():
    gate = OpenGate("dependency", 0, EvidenceUnavailable("linked_issue", NOW, 1, "unavailable"), NoAuthorizedRemediation("read required"))
    before = replace(snapshot(), gates=(gate,))
    proposal = record_resolve_attempt(before, result(before), ceiling=3, admission=admission(before, result(before)))
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, gates=()))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW, admission=admission(before, proposal.audit))


def test_unmaterialized_operator_proposal_cannot_be_committed():
    before = exhausted()
    proposal = start(before)
    with pytest.raises(ValueError, match="outbox obligations"):
        validate_resolve_transition(before, proposal, ceiling=3, now=NOW)


def test_failure_crossing_ceiling_opens_one_visible_gate_and_preserves_other_holds():
    other = OpenGate("dependency", 0, EvidenceUnavailable("linked_issue", NOW, 1, "read failed"), NoAuthorizedRemediation("read required"))
    state = exhausted(extra_gates=(other,))
    assert HeldProjection(state).held
    assert other in state.gates
    ceilings = [g for g in state.gates if isinstance(g.cause, ResolveCeiling)]
    assert len(ceilings) == 1
    assert ceilings[0].cause.failures == 3
    assert ceilings[0].cause.last_failure == "failed experiment"


def test_duplicate_delivery_does_not_charge_attempt_twice_and_conflict_is_not_ignored():
    before = snapshot()
    failed = result(before)
    after = record_resolve_attempt(before, failed, ceiling=3, admission=admission(before, failed)).snapshot
    assert isinstance(record_resolve_attempt(after, failed, ceiling=3, prior_result=failed, admission=admission(after, failed)), WorkflowUnchanged)
    conflict = replace(failed, summary="different result")
    assert isinstance(record_resolve_attempt(after, conflict, ceiling=3, prior_result=failed, admission=admission(after, conflict)), WorkflowRejected)


def test_old_generation_completion_cannot_charge_new_episode_or_clear_hold():
    before = exhausted()
    old_result = result(before)
    after = start(before).snapshot
    transition = record_resolve_attempt(after, old_result, ceiling=3, admission=admission(after, old_result))
    assert isinstance(transition, WorkflowUnchanged)
    assert transition.snapshot.episode.failures == 0
    assert HeldProjection(transition.snapshot).held


def test_foreign_target_result_cannot_charge_this_workflow():
    assert isinstance(record_resolve_attempt(snapshot(), result(snapshot(number=999)), ceiling=3, admission=admission(snapshot(), result(snapshot(number=999)))), WorkflowRejected)


@pytest.mark.parametrize("guidance, expected", [(KeepGuidance(), "prior scope"), (ClearGuidance(), ""), (SetGuidance("new scope"), "new scope")])
def test_rearm_grants_one_bounded_episode_with_durable_guidance_and_required_remote_work(guidance, expected):
    before = exhausted(guidance="prior scope")
    command = request(before, guidance=guidance)
    transition = start(before, command)
    after = transition.snapshot
    assert after.revision.fence.generation == before.revision.fence.generation + 1
    assert after.episode.failures == 0
    assert after.episode.guidance == expected
    assert isinstance(after.intent, PendingRearm)
    assert after.intent.required_commands == (resolve_projection_command_id(before.revision.fence.key, command.request_id),)
    assert after.intent.handoff.source == before.binding
    assert after.intent.handoff.destination == after.binding
    assert after.intent.handoff.grant_request_id == command.request_id
    assert isinstance(transition.audit, AuthorizedOperatorCommand)
    assert isinstance(transition.audit.preflight, ResolveRearmAcceptance)
    assert transition.audit.preflight.workspace == after.intent.handoff.observation
    assert HeldProjection(after).held
    assert isinstance(start(after, command, prior_request=command), WorkflowUnchanged)
    assert isinstance(start(after, replace(command, request_id="other")), WorkflowRejected)
    assert isinstance(start(after, replace(command, guidance=SetGuidance("different")), prior_request=command), WorkflowRejected)


def test_distinct_target_or_request_gets_distinct_projection_identity():
    key = snapshot().revision.fence.key
    assert resolve_projection_command_id(key, "a") != resolve_projection_command_id(key, "b")
    assert resolve_projection_command_id(key, "a") != resolve_projection_command_id(snapshot(number=8).revision.fence.key, "a")


@pytest.mark.parametrize("case", ["unavailable", "absent", "closed", "stale", "future", "linked_read_failed", "linked_pr"])
def test_rearm_never_grants_allowance_from_missing_stale_or_terminal_facts(case):
    state = exhausted()
    issue, linkage = observed(state), no_pr(state)
    key = state.revision.fence.key
    if case == "unavailable": issue = IssueUnavailable(key, NOW, "outage")
    if case == "absent": issue = IssueAbsent(key, NOW)
    if case == "closed": issue = observed(state, remote_state="closed")
    if case == "stale": issue = replace(issue, observed_at=NOW - timedelta(seconds=301))
    if case == "future": issue = replace(issue, observed_at=NOW + timedelta(seconds=1))
    if case == "linked_read_failed": linkage = LinkedPrUnavailable(key, NOW, "outage")
    if case == "linked_pr": linkage = LinkedPrPresent(key, 70, "head", NOW)
    transition = request_resolve_rearm(state, request(state), ACTOR, issue, linkage, now=NOW, active_attempts=(), workspace=workspace_observation(state.binding, NOW))
    assert isinstance(transition, WorkflowRejected)
    assert state.episode.failures == 3


def test_active_attempt_must_drain_before_grant_can_be_accepted():
    state = exhausted()
    active = WorkflowAttemptIdentity(state.binding, RunId("running"), "leg")
    transition = request_resolve_rearm(state, request(state), ACTOR, observed(state), no_pr(state), now=NOW, active_attempts=(active,), workspace=workspace_observation(state.binding, NOW))
    assert transition.code is WorkflowRejection.OTHER_HOLD


def test_cancellation_keeps_allowance_and_uncertainty_then_continuation_does_not_renew_it():
    pending = start(exhausted()).snapshot
    command_id = pending.intent.required_commands[0]
    uncertainty = RemoteOutcomeUncertain(command_id, NOW - timedelta(hours=1), 20, "receipt lost")
    cancel = CancelRearm("cancel:1", pending.revision, pending.intent.request_id, "scope changed")
    stopped = cancel_resolve_rearm(pending, cancel, ACTOR, uncertainty=(uncertainty,)).snapshot
    assert isinstance(stopped.intent, CancelledRearm)
    assert stopped.episode == pending.episode
    assert stopped.revision.fence == pending.revision.fence
    assert command_id in stopped.intent.retired_obligations
    assert any(isinstance(g.cause, RemoteOutcomeUncertain) for g in stopped.gates)
    command = ContinueGrantedRearm("continued", stopped.revision, stopped.intent.request_id)
    continued = continue_resolve_rearm(stopped, command, ACTOR, observed(stopped), no_pr(stopped), now=NOW, active_attempts=()).snapshot
    assert continued.episode == stopped.episode
    assert continued.revision.fence.generation == stopped.revision.fence.generation + 1
    assert HeldProjection(continued).held
    assert any(isinstance(g, OpenGate) and isinstance(g.cause, RemoteOutcomeUncertain) for g in continued.gates)
    assert continued.intent.required_commands != pending.intent.required_commands
    assert continued.intent.handoff == stopped.intent.handoff == pending.intent.handoff
    assert not any(isinstance(operation, RenewResolveAllowance) for operation in continued.intent.operations)


def test_terminal_supersession_preserves_verified_effect_and_cannot_become_clean_completion():
    pending = start(exhausted()).snapshot
    ids = pending.intent.required_commands
    stopped = supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "closed:observation", verified_commands=ids).snapshot
    assert isinstance(stopped.intent, SupersededRearm)
    assert stopped.intent.verified_commands == ids
    assert stopped.intent.retired_obligations == ()
    assert stopped.episode == pending.episode
    assert stopped.intent.handoff == pending.intent.handoff
    assert HeldProjection(stopped).held
    result = complete_resolve_rearm(stopped, observed(stopped), no_pr(stopped), verified_commands=ids, evidence_ids=("receipt",), now=NOW)
    assert isinstance(result, WorkflowUnchanged)


def test_completion_requires_receipts_and_fresh_ready_projection():
    pending = start(exhausted()).snapshot
    ready = observed(pending, labels=frozenset({Label.AI_READY}))
    ack = workspace_ack(pending.intent.handoff, NOW)
    assert isinstance(complete_resolve_rearm(pending, ready, no_pr(pending), verified_commands=(), evidence_ids=("read",), now=NOW, workspace=ack), WorkflowRejected)
    assert isinstance(complete_resolve_rearm(pending, observed(pending), no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("read",), now=NOW, workspace=ack), WorkflowRejected)
    completed = complete_resolve_rearm(pending, ready, no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("receipt", "read"), now=NOW, workspace=ack).snapshot
    assert isinstance(completed.intent, CompletedRearm)
    assert not HeldProjection(completed).held
    assert completed.episode == pending.episode
    assert completed.intent.workspace == ack


@pytest.mark.parametrize("workspace", [None, {}, "ready"])
def test_rearm_missing_workspace_cannot_renew_allowance(workspace):
    before = exhausted()
    rejected = start(before, workspace=workspace)
    assert isinstance(rejected, WorkflowRejected)
    assert rejected.code is WorkflowRejection.OBSERVATION_UNAVAILABLE
    assert before.episode.failures == 3


@pytest.mark.parametrize("field", ["target", "repository", "epoch", "generation", "episode"])
def test_rearm_workspace_must_match_exact_current_source(field):
    before = exhausted()
    source = before.binding
    if field == "target":
        source = snapshot(number=8).binding
    elif field == "repository":
        source = replace(source, fence=replace(source.fence, key=replace(source.fence.key, repository="other/repo")))
    elif field == "epoch":
        source = replace(source, authority=replace(source.authority, epoch=2))
    elif field == "generation":
        source = replace(source, fence=replace(source.fence, generation=1))
    else:
        source = replace(source, episode_id="foreign")
    rejected = start(before, workspace=workspace_observation(source, NOW))
    assert isinstance(rejected, WorkflowRejected) and rejected.code is WorkflowRejection.INVALID_SCOPE


@pytest.mark.parametrize("age,accepted", [(0, True), (300, True), (301, False), (-1, False)])
def test_rearm_workspace_base_must_be_fresh_at_acceptance(age, accepted):
    before = exhausted()
    proposal = start(before, workspace=workspace_observation(before.binding, NOW - timedelta(seconds=age)))
    assert isinstance(proposal, WorkflowChanged if accepted else WorkflowRejected)


def test_replay_returns_existing_grant_without_requiring_new_workspace_evidence():
    before = exhausted()
    command = request(before)
    accepted = start(before, command).snapshot
    replay = request_resolve_rearm(accepted, command, ACTOR, observed(accepted), no_pr(accepted),
                                   now=NOW, active_attempts=(), prior_request=command)
    assert isinstance(replay, WorkflowUnchanged) and replay.snapshot == accepted


@pytest.mark.parametrize("case", ["missing", "wrong_handoff", "future_generation", "future_time"])
def test_remote_success_cannot_finish_without_exact_nonfuture_workspace_ack(case):
    pending = start(exhausted()).snapshot
    ack = workspace_ack(pending.intent.handoff, NOW)
    if case == "missing":
        ack = None
    elif case == "wrong_handoff":
        ack = workspace_ack(replace(pending.intent.handoff, branch="fix/7-other"), NOW)
    elif case == "future_generation":
        ack = workspace_ack(pending.intent.handoff, NOW, performed_under=replace(pending.binding, fence=replace(pending.binding.fence, generation=2)))
    else:
        ack = workspace_ack(pending.intent.handoff, NOW + timedelta(seconds=1))
    result = complete_resolve_rearm(pending, observed(pending, labels=frozenset({Label.AI_READY})), no_pr(pending),
        verified_commands=pending.intent.required_commands, evidence_ids=("verified-remote",), now=NOW, workspace=ack)
    assert isinstance(result, WorkflowRejected)
    assert HeldProjection(pending).held


def test_continued_grant_completes_with_original_workspace_receipt_without_expiry_or_new_grant():
    pending = start(exhausted(guidance="retained guidance")).snapshot
    ack = workspace_ack(pending.intent.handoff, NOW)
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, pending.intent.request_id, "pause"), ACTOR).snapshot
    command = ContinueGrantedRearm("continue", stopped.revision, stopped.intent.request_id)
    continued = continue_resolve_rearm(stopped, command, ACTOR, observed(stopped), no_pr(stopped), now=NOW, active_attempts=()).snapshot
    later = NOW + timedelta(days=1)
    ready = replace(observed(continued, labels=frozenset({Label.AI_READY})), observed_at=later)
    no_link = replace(no_pr(continued), observed_at=later)
    completed = complete_resolve_rearm(continued, ready, no_link, verified_commands=continued.intent.required_commands,
                                     evidence_ids=("fresh-remote",), now=later, workspace=ack)
    assert isinstance(completed, WorkflowChanged)
    assert completed.snapshot.intent.workspace == ack
    assert completed.snapshot.episode == pending.episode
    assert not HeldProjection(completed.snapshot).held
    validate_resolve_transition(continued, completed, ceiling=3, now=later,
                               observations=(ResolveRearmPreflight(ready, no_link),))


def test_transition_validation_rejects_substituted_workspace_after_cancellation():
    pending = start(exhausted()).snapshot
    proposal = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, pending.intent.request_id, "pause"), ACTOR)
    assert isinstance(proposal, WorkflowChanged)
    assert isinstance(proposal.snapshot.intent, CancelledRearm)
    changed = replace(proposal.snapshot.intent.handoff, branch="fix/7-substituted")
    forged = replace(proposal, snapshot=replace(proposal.snapshot, intent=replace(proposal.snapshot.intent, handoff=changed)))
    with pytest.raises(ValueError):
        validate_resolve_transition(pending, forged, ceiling=3, now=NOW)


def test_transition_validation_rechecks_request_workspace_freshness_not_just_final_values():
    before = exhausted()
    old = NOW - timedelta(seconds=301)
    proposal = request_resolve_rearm(before, request(before), ACTOR,
        replace(observed(before), observed_at=old), replace(no_pr(before), observed_at=old),
        now=old, active_attempts=(), workspace=workspace_observation(before.binding, old))
    assert isinstance(proposal, WorkflowChanged)
    assert isinstance(proposal.audit, AuthorizedOperatorCommand)
    assert isinstance(proposal.audit.preflight, ResolveRearmAcceptance)
    refreshed_remote = replace(proposal.audit.preflight, preflight=ResolveRearmPreflight(observed(before), no_pr(before)))
    proposal = replace(proposal, audit=replace(proposal.audit, preflight=refreshed_remote))
    with pytest.raises(ValueError, match="authorized state transition"):
        validate_resolve_transition(before, proposal, ceiling=3, now=NOW)


@pytest.mark.parametrize("case", ["missing", "duplicate", "different_episode"])
def test_pending_value_cannot_omit_or_replace_its_workspace_obligation(case):
    pending = start(exhausted()).snapshot.intent
    assert isinstance(pending, PendingRearm)
    operation = next(item for item in pending.operations if isinstance(item, PrepareResolveWorkspace))
    with pytest.raises(ValueError):
        if case == "missing":
            replace(pending, operations=tuple(item for item in pending.operations if item != operation))
        elif case == "duplicate":
            replace(pending, operations=(*pending.operations, operation))
        else:
            handoff = replace(operation.handoff, destination=replace(operation.handoff.destination, episode_id="ungranted"))
            replace(pending, operations=tuple(PrepareResolveWorkspace(handoff) if item == operation else item for item in pending.operations))


def test_request_and_continue_audits_cannot_exchange_observation_purposes():
    before = exhausted()
    remote = ResolveRearmPreflight(observed(before), no_pr(before))
    acceptance = ResolveRearmAcceptance(remote, workspace_observation(before.binding, NOW))
    with pytest.raises(ValueError):
        AuthorizedOperatorCommand(ACTOR, request(before), remote)
    with pytest.raises(ValueError):
        AuthorizedOperatorCommand(ACTOR, ContinueGrantedRearm("continue", before.revision, "stopped"), acceptance)


def test_selected_rearm_can_complete_while_an_independent_gate_remains_held():
    other = OpenGate("other", 0, EvidenceUnavailable("approval", NOW, 1, "read failed"), NoAuthorizedRemediation("read required"))
    pending = start(exhausted(extra_gates=(other,))).snapshot
    completed = complete_resolve_rearm(pending, observed(pending), no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("receipt", "read"), now=NOW, workspace=workspace_ack(pending.intent.handoff, NOW)).snapshot
    assert isinstance(completed.intent, CompletedRearm)
    assert other in completed.gates
    assert HeldProjection(completed).held


@pytest.mark.parametrize("ceiling", [None, True, -1, 2**63, 1.5])
def test_malformed_ceiling_is_rejected(ceiling):
    state = snapshot()
    with pytest.raises(ValueError):
        record_resolve_attempt(state, result(state), ceiling=ceiling, admission=admission(state, result(state)))


def test_disabled_ceiling_preserves_existing_unbounded_configuration_semantics():
    state = snapshot(failures=99)
    after = record_resolve_attempt(state, result(state), ceiling=0, admission=admission(state, result(state))).snapshot
    assert after.episode.failures == 100
    assert not HeldProjection(after).held


def test_counter_overflow_cannot_wrap_to_fresh_allowance():
    state = snapshot(failures=2**63 - 1)
    assert isinstance(record_resolve_attempt(state, result(state), ceiling=3, admission=admission(state, result(state))), WorkflowRejected)


def test_long_result_summary_remains_bounded_operator_context():
    state = snapshot()
    classified = result(state, summary="line\n" * 100000)
    assert len(classified.summary) == 400
    assert "\n" not in classified.summary


def test_imported_exhausted_episode_gets_a_visible_gate_without_buying_a_new_attempt():
    state = snapshot(failures=3)
    held = reconcile_resolve_ceiling(state, ceiling=3, evidence_ids=("import:digest",)).snapshot
    assert held.episode == state.episode
    assert HeldProjection(held).held
    assert isinstance(reconcile_resolve_ceiling(held, ceiling=3, evidence_ids=("import:digest",)), WorkflowUnchanged)


def test_gate_cannot_grant_allowance_after_its_cause_no_longer_matches_the_episode():
    state = exhausted()
    command = request(state)
    changed_episode = replace(state, episode=replace(state.episode, failures=0))
    assert start(changed_episode, command).code is WorkflowRejection.NOT_EXHAUSTED


def test_new_exhaustion_after_rearm_opens_a_distinct_visible_gate():
    first = exhausted()
    old_gate = next(g for g in first.gates if isinstance(g, OpenGate) and isinstance(g.cause, ResolveCeiling))
    pending = start(first).snapshot
    current = complete_resolve_rearm(
        pending, observed(pending, labels=frozenset({Label.AI_READY})), no_pr(pending),
        verified_commands=pending.intent.required_commands, evidence_ids=("receipt", "read"), now=NOW,
        workspace=workspace_ack(pending.intent.handoff, NOW),
    ).snapshot
    for number in range(3):
        current = record_resolve_attempt(current, result(current, run=f"new:{number}"), ceiling=3, admission=admission(current, result(current, run=f"new:{number}"))).snapshot
    new_gate = next(g for g in current.gates if isinstance(g, OpenGate) and isinstance(g.cause, ResolveCeiling))
    assert new_gate.gate_id != old_gate.gate_id
    assert new_gate.opened_generation > old_gate.opened_generation
    assert HeldProjection(current).held


def staged_result(state, stage, *, kind=ResolveResultKind.STAGE_PROGRESSED, name="stage", session_id="session"):
    attempt = WorkflowAttemptIdentity(state.binding, RunId(name), f"leg:{name}")
    session = ResolveSession(attempt, session_id, "codex", "<runtime-default>") if session_id else None
    return ResolveAttemptResult(attempt, kind, stage=stage, session=session)


def authored_state():
    before = snapshot()
    author = staged_result(before, ResolveStage.AUTHOR)
    changed = record_resolve_attempt(before, author, ceiling=3, admission=admission(before, author))
    assert isinstance(changed, WorkflowChanged) and isinstance(changed.snapshot, WorkflowSnapshot)
    return changed.snapshot


def test_successful_author_owns_checkpoint_without_consuming_allowance():
    state = authored_state()
    continuation = state.episode.continuation
    assert isinstance(continuation, AuthoredResolve)
    assert continuation.author_attempt.run_id == "stage"
    assert continuation.session is not None and continuation.session.session_id == "session"
    assert state.episode.failures == 0
    assert continuation.last_applied_admission_version == 1


def test_blocked_author_cannot_claim_an_authored_checkpoint():
    state = snapshot()
    blocked = staged_result(state, ResolveStage.AUTHOR, kind=ResolveResultKind.BLOCKED)
    changed = record_resolve_attempt(state, blocked, ceiling=3, admission=admission(state, blocked))
    assert isinstance(changed.snapshot.episode.continuation, FreshResolve)
    assert changed.snapshot.episode.failures == 0


@pytest.mark.parametrize("stage", [ResolveStage.RESOLVE, ResolveStage.IMPLEMENT, ResolveStage.ADJUDICATE, ResolveStage.RESUME])
@pytest.mark.parametrize("session_id", [None, "new-session"])
def test_newer_leg_updates_or_clears_session_without_losing_authored_checkpoint(stage, session_id):
    before = authored_state()
    old = before.episode.continuation
    outcome = staged_result(before, stage, name="next", session_id=session_id)
    after = record_resolve_attempt(before, outcome, ceiling=3, admission=admission(before, outcome)).snapshot
    continuation = after.episode.continuation
    assert isinstance(continuation, AuthoredResolve) and isinstance(old, AuthoredResolve)
    assert continuation.author_attempt == old.author_attempt
    assert continuation.session == outcome.session
    assert continuation.last_applied_admission_version == before.revision.version


@pytest.mark.parametrize("kind", [ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL])
def test_infrastructure_or_budget_failure_cannot_replace_authored_session(kind):
    before = authored_state()
    failed = staged_result(before, ResolveStage.IMPLEMENT, kind=kind, name="failed", session_id="unusable")
    unchanged = record_resolve_attempt(before, failed, ceiling=3, admission=admission(before, failed))
    assert isinstance(unchanged, WorkflowUnchanged) and unchanged.snapshot == before


@pytest.mark.parametrize("kind", list(ResolveResultKind))
def test_router_result_is_audit_only_even_when_followup_is_requeue(kind):
    before = authored_state()
    routed = staged_result(before, ResolveStage.ROUTE, kind=kind, name="route", session_id=None)
    unchanged = record_resolve_attempt(before, routed, ceiling=3, admission=admission(before, routed))
    assert isinstance(unchanged, WorkflowUnchanged) and unchanged.snapshot == before


def test_reversed_result_order_counts_old_strike_but_preserves_newer_checkpoint():
    admitted = snapshot(failures=2)
    old = staged_result(admitted, ResolveStage.RESOLVE, kind=ResolveResultKind.COUNTED_REQUEUE, name="old", session_id="old-session")
    old_admission = admission(admitted, old)
    newer = replace(admitted, revision=replace(admitted.revision, version=2))
    author = staged_result(newer, ResolveStage.AUTHOR, name="author", session_id="new-session")
    after_author = record_resolve_attempt(newer, author, ceiling=3, admission=admission(newer, author)).snapshot
    after_old = record_resolve_attempt(after_author, old, ceiling=3, admission=old_admission).snapshot
    assert after_old.episode.continuation == after_author.episode.continuation
    assert after_old.episode.failures == 3
    assert HeldProjection(after_old).held


def test_blocked_resume_clears_even_returned_handle_and_fences_late_session_restore():
    before = authored_state()
    late = staged_result(before, ResolveStage.IMPLEMENT, name="late", session_id="old-handle")
    late_admission = admission(before, late)
    resumed = replace(before, revision=replace(before.revision, version=3))
    blocked = staged_result(resumed, ResolveStage.RESUME, kind=ResolveResultKind.BLOCKED, name="resume", session_id="blocked-handle")
    cleared = record_resolve_attempt(resumed, blocked, ceiling=3, admission=admission(resumed, blocked)).snapshot
    assert cleared.episode.continuation.session is None
    assert cleared.episode.continuation.last_applied_admission_version == 3
    replay = record_resolve_attempt(cleared, late, ceiling=3, admission=late_admission)
    assert isinstance(replay, WorkflowUnchanged)
    assert isinstance(cleared.episode.continuation, AuthoredResolve)


def test_new_episode_rearm_clears_checkpoint_but_continue_preserves_granted_episode():
    before = authored_state()
    before = replace(before, episode=replace(before.episode, failures=3))
    held = reconcile_resolve_ceiling(before, ceiling=3, evidence_ids=("import",)).snapshot
    pending = start(held).snapshot
    assert pending.episode.continuation == FreshResolve()
    # Continue retains an episode-owned checkpoint, including its earlier
    # generation provenance, rather than rewriting that provenance on re-arm.
    outcome = staged_result(pending, ResolveStage.AUTHOR)
    checkpoint = AuthoredResolve(admission(pending, outcome), pending.revision.version, outcome.session)
    pending = replace(pending, episode=replace(pending.episode, continuation=checkpoint))
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, pending.intent.request_id, "stop"), ACTOR).snapshot
    continued = continue_resolve_rearm(stopped, ContinueGrantedRearm("continue", stopped.revision, stopped.intent.request_id), ACTOR, observed(stopped), no_pr(stopped), now=NOW, active_attempts=()).snapshot
    assert continued.episode == stopped.episode


@pytest.mark.parametrize("changed", ["run", "stage", "future_version"])
def test_result_requires_matching_captured_admission(changed):
    before = snapshot()
    outcome = staged_result(before, ResolveStage.AUTHOR)
    proof = admission(before, outcome)
    if changed == "run":
        proof = replace(proof, attempt=replace(proof.attempt, run_id=RunId("different")))
    elif changed == "stage":
        proof = replace(proof, stage=ResolveStage.RESOLVE)
    else:
        proof = replace(proof, revision=replace(proof.revision, version=2))
    assert isinstance(record_resolve_attempt(before, outcome, ceiling=3, admission=proof), WorkflowRejected)


def test_replacement_checkpoint_cannot_be_committed_without_exact_admission_derivation():
    before = snapshot()
    outcome = staged_result(before, ResolveStage.AUTHOR)
    proof = admission(before, outcome)
    changed = record_resolve_attempt(before, outcome, ceiling=3, admission=proof)
    assert isinstance(changed, WorkflowChanged)
    forged = replace(changed, snapshot=replace(changed.snapshot, episode=replace(changed.snapshot.episode, continuation=FreshResolve())))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW, admission=proof)
    with pytest.raises(ValueError, match="captured admission"):
        validate_resolve_transition(before, changed, ceiling=3, now=NOW)


@pytest.mark.parametrize("order", [(1, 0), (0, 1)])
def test_late_author_checkpoint_converges_independently_of_newer_implementation_session(order):
    state = replace(snapshot(), revision=replace(snapshot().revision, version=2))
    author = staged_result(state, ResolveStage.AUTHOR, name="author")
    implement = staged_result(state, ResolveStage.IMPLEMENT, name="implement", session_id="implementation-session")
    results = (author, implement)
    proofs = tuple(ResolveAdmission(item.attempt, replace(state.revision, version=index + 1), item.stage, input_fingerprint="a" * 64) for index, item in enumerate(results))
    for index in order:
        state = record_resolve_attempt(state, results[index], ceiling=3, admission=proofs[index]).snapshot
    assert state.episode.continuation == AuthoredResolve(proofs[0], 2, implement.session)


@pytest.mark.parametrize("order", tuple(permutations(range(4))))
def test_multiple_authors_and_blocked_resume_converge_for_every_delivery_order(order):
    state = replace(snapshot(), revision=replace(snapshot().revision, version=4))
    results = (
        staged_result(state, ResolveStage.AUTHOR, name="first-author"),
        staged_result(state, ResolveStage.AUTHOR, name="second-author"),
        staged_result(state, ResolveStage.IMPLEMENT, name="implement"),
        staged_result(state, ResolveStage.RESUME, kind=ResolveResultKind.BLOCKED, name="resume"),
    )
    proofs = tuple(ResolveAdmission(item.attempt, replace(state.revision, version=index + 1), item.stage, input_fingerprint="a" * 64) for index, item in enumerate(results))
    for index in order:
        state = record_resolve_attempt(state, results[index], ceiling=3, admission=proofs[index]).snapshot
    assert state.episode.continuation == AuthoredResolve(proofs[1], 4, None)
    assert state.episode.failures == 0
