"""Behavioral contracts for resolve allowance and re-arm transitions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.contracts import CommandId, RunId, WorkflowAttemptIdentity
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason, ResolveFollowup
from issue_agent.domain.workflow import (
    CancelRearm, CancelledRearm, ClearGuidance, CompletedRearm, ContinueGrantedRearm,
    EvidenceUnavailable, HeldProjection, IssueAbsent, IssueUnavailable, KeepGuidance,
    LinkedPrPresent, LinkedPrUnavailable, NoAuthorizedRemediation, NoLinkedPr,
    ObservedIssue, OpenGate, OperatorActor, PendingRearm, RearmingGate,
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
    validate_resolve_transition,
)
from issue_agent.domain.world import IssueView

NOW = datetime(2026, 9, 14, 9, tzinfo=timezone.utc)
ACTOR = OperatorActor("operator:alice", "dashboard")


def snapshot(*, number=7, failures=0, guidance=""):
    key = WorkflowKey("owner/repo", IssueRef(number))
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(key, 0), 0),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1),
        ResolveEpisode("initial", failures, guidance=guidance),
    )


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
    transition = record_resolve_attempt(state, result(state, summary="failed experiment"), ceiling=3)
    assert isinstance(transition, WorkflowChanged)
    return transition.snapshot


def request(state, request_id="request:1", guidance=None):
    gate = next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, ResolveCeiling))
    return RequestRearm(request_id, state.revision, gate.gate_id, guidance or KeepGuidance())


def start(state, command=None, **kwargs):
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
    transition = record_resolve_attempt(state, classified, ceiling=3)
    if expected is ResolveResultKind.COUNTED_REQUEUE:
        assert transition.snapshot.episode.failures == 2
    else:
        assert isinstance(transition, WorkflowUnchanged)


@pytest.mark.parametrize("followup", [ResolveFollowup.BLOCKED, ResolveFollowup.PR_OPENED])
def test_blocked_and_observed_pr_success_do_not_consume_resolve_allowance(followup):
    state = snapshot(failures=2)
    classified = result(state, status="blocked", followup=followup)
    assert isinstance(record_resolve_attempt(state, classified, ceiling=3), WorkflowUnchanged)


def test_explicit_changed_requirements_requeue_counts_even_if_agent_claimed_blocked():
    state = snapshot()
    classified = result(state, status="blocked", followup=ResolveFollowup.RETRY)
    assert record_resolve_attempt(state, classified, ceiling=3).snapshot.episode.failures == 1


def test_unadmitted_result_without_harness_attempt_cannot_consume_allowance():
    assert classify_resolve_attempt(AgentResult(is_error=True), ResolveFollowup.RETRY) is None


def test_prior_proposal_validation_rejects_arbitrary_counter_replacement():
    before = snapshot()
    proposal = record_resolve_attempt(before, result(before), ceiling=3)
    assert isinstance(proposal, WorkflowChanged)
    validate_resolve_transition(before, proposal, ceiling=3, now=NOW)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, episode=replace(proposal.snapshot.episode, failures=10)))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW)


def test_prior_proposal_validation_requires_ceiling_gate_in_same_count_transaction():
    before = snapshot(failures=2)
    proposal = record_resolve_attempt(before, result(before), ceiling=3)
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, gates=()))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW)


def test_prior_proposal_validation_preserves_unrelated_hold_during_count():
    gate = OpenGate("dependency", 0, EvidenceUnavailable("linked_issue", NOW, 1, "unavailable"), NoAuthorizedRemediation("read required"))
    before = replace(snapshot(), gates=(gate,))
    proposal = record_resolve_attempt(before, result(before), ceiling=3)
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, gates=()))
    with pytest.raises(ValueError, match="pure transition"):
        validate_resolve_transition(before, forged, ceiling=3, now=NOW)


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
    after = record_resolve_attempt(before, failed, ceiling=3).snapshot
    assert isinstance(record_resolve_attempt(after, failed, ceiling=3, prior_result=failed), WorkflowUnchanged)
    conflict = replace(failed, summary="different result")
    assert isinstance(record_resolve_attempt(after, conflict, ceiling=3, prior_result=failed), WorkflowRejected)


def test_old_generation_completion_cannot_charge_new_episode_or_clear_hold():
    before = exhausted()
    old_result = result(before)
    after = start(before).snapshot
    transition = record_resolve_attempt(after, old_result, ceiling=3)
    assert isinstance(transition, WorkflowUnchanged)
    assert transition.snapshot.episode.failures == 0
    assert HeldProjection(transition.snapshot).held


def test_foreign_target_result_cannot_charge_this_workflow():
    assert isinstance(record_resolve_attempt(snapshot(), result(snapshot(number=999)), ceiling=3), WorkflowRejected)


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
    transition = request_resolve_rearm(state, request(state), ACTOR, issue, linkage, now=NOW, active_attempts=())
    assert isinstance(transition, WorkflowRejected)
    assert state.episode.failures == 3


def test_active_attempt_must_drain_before_grant_can_be_accepted():
    state = exhausted()
    active = WorkflowAttemptIdentity(state.binding, RunId("running"), "leg")
    transition = request_resolve_rearm(state, request(state), ACTOR, observed(state), no_pr(state), now=NOW, active_attempts=(active,))
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


def test_terminal_supersession_preserves_verified_effect_and_cannot_become_clean_completion():
    pending = start(exhausted()).snapshot
    ids = pending.intent.required_commands
    stopped = supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "closed:observation", verified_commands=ids).snapshot
    assert isinstance(stopped.intent, SupersededRearm)
    assert stopped.intent.verified_commands == ids
    assert stopped.intent.retired_obligations == ()
    assert stopped.episode == pending.episode
    assert HeldProjection(stopped).held
    result = complete_resolve_rearm(stopped, observed(stopped), no_pr(stopped), verified_commands=ids, evidence_ids=("receipt",), now=NOW)
    assert isinstance(result, WorkflowUnchanged)


def test_completion_requires_receipts_and_fresh_ready_projection():
    pending = start(exhausted()).snapshot
    ready = observed(pending, labels=frozenset({Label.AI_READY}))
    assert isinstance(complete_resolve_rearm(pending, ready, no_pr(pending), verified_commands=(), evidence_ids=("read",), now=NOW), WorkflowRejected)
    assert isinstance(complete_resolve_rearm(pending, observed(pending), no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("read",), now=NOW), WorkflowRejected)
    completed = complete_resolve_rearm(pending, ready, no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("receipt", "read"), now=NOW).snapshot
    assert isinstance(completed.intent, CompletedRearm)
    assert not HeldProjection(completed).held
    assert completed.episode == pending.episode


def test_selected_rearm_can_complete_while_an_independent_gate_remains_held():
    other = OpenGate("other", 0, EvidenceUnavailable("approval", NOW, 1, "read failed"), NoAuthorizedRemediation("read required"))
    pending = start(exhausted(extra_gates=(other,))).snapshot
    completed = complete_resolve_rearm(pending, observed(pending), no_pr(pending), verified_commands=pending.intent.required_commands, evidence_ids=("receipt", "read"), now=NOW).snapshot
    assert isinstance(completed.intent, CompletedRearm)
    assert other in completed.gates
    assert HeldProjection(completed).held


@pytest.mark.parametrize("ceiling", [None, True, -1, 2**63, 1.5])
def test_malformed_ceiling_is_rejected(ceiling):
    state = snapshot()
    with pytest.raises(ValueError):
        record_resolve_attempt(state, result(state), ceiling=ceiling)


def test_disabled_ceiling_preserves_existing_unbounded_configuration_semantics():
    state = snapshot(failures=99)
    after = record_resolve_attempt(state, result(state), ceiling=0).snapshot
    assert after.episode.failures == 100
    assert not HeldProjection(after).held


def test_counter_overflow_cannot_wrap_to_fresh_allowance():
    state = snapshot(failures=2**63 - 1)
    assert isinstance(record_resolve_attempt(state, result(state), ceiling=3), WorkflowRejected)


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
    ).snapshot
    for number in range(3):
        current = record_resolve_attempt(current, result(current, run=f"new:{number}"), ceiling=3).snapshot
    new_gate = next(g for g in current.gates if isinstance(g, OpenGate) and isinstance(g.cause, ResolveCeiling))
    assert new_gate.gate_id != old_gate.gate_id
    assert new_gate.opened_generation > old_gate.opened_generation
    assert HeldProjection(current).held
