from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.domain.resolve_rearm import (
    PendingResolveRearm, RearmObservation, UnavailableResolveRearm,
    decide_rearm_reconciliation, defer_rearm_observation,
    schedule_rearm_observation, update_rearm_observation,
)
from issue_agent.domain.workflow import (
    ActiveReconciliation, CancelledRearm, CancelRearm, CompletedRearm, ContinueGrantedRearm, EvidenceUnavailable, LinkedPrPresent,
    NoAuthorizedRemediation, OpenGate, PendingRearm, ReconciliationPolicy, RemoteOutcomeUncertain, ResolveRearmPreflight, StopReason,
    SupersededRearm, WorkflowChanged, WorkflowUnchanged,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.contracts import CommandId
from issue_agent.domain.workflow_policy import cancel_resolve_rearm, continue_resolve_rearm, supersede_resolve_rearm, validate_resolve_transition
from tests.domain.test_workflow_policy import ACTOR, NOW, exhausted, start, observed, no_pr
from tests.fakes.workflow_workspace import workspace_ack


def pending():
    return start(exhausted())


def reading(state):
    return RearmObservation(state.intent.request_id, state.binding, ActiveReconciliation(NOW, NOW, 0))


def reconcile(proposal, *, workspace=None, verified=(), rejected=(), uncertainty=(), issue=None, linkage=None, now=NOW,
              external_holds=()):
    state = proposal.snapshot
    return decide_rearm_reconciliation(
        state, proposal.audit.preflight, issue or observed(state, labels=frozenset({Label.AI_READY})),
        linkage or no_pr(state), verified_commands=verified, rejected_commands=rejected,
        uncertainty=uncertainty, workspace=workspace, evidence_id="observation", now=now,
        external_holds=external_holds,
    )


@pytest.mark.parametrize("missing", ["workspace", "receipt", "both"])
def test_reconciliation_never_completes_without_both_local_and_remote_evidence(missing):
    proposal = pending()
    state = proposal.snapshot
    result = reconcile(proposal, workspace=None if missing in ("workspace", "both") else workspace_ack(state.intent.handoff, NOW),
                       verified=() if missing in ("receipt", "both") else state.intent.required_commands)
    assert isinstance(result, WorkflowUnchanged)
    assert result.snapshot == state


def test_matching_workspace_and_all_receipts_complete_selected_rearm():
    proposal = pending()
    state = proposal.snapshot
    result = reconcile(proposal, workspace=workspace_ack(state.intent.handoff, NOW), verified=state.intent.required_commands)
    assert isinstance(result, WorkflowChanged)
    assert isinstance(result.snapshot.intent, CompletedRearm)
    assert result.snapshot.gates == ()


@pytest.mark.parametrize("change,reason", [("closed", StopReason.TARGET_TERMINAL),
                                           ("hold", StopReason.DECISION_CHANGED),
                                           ("pr", StopReason.DECISION_CHANGED),
                                           ("rejected", StopReason.REQUIRED_EFFECT_REJECTED)])
def test_obsolete_decision_stops_before_workspace_preparation(change, reason):
    proposal = pending()
    state = proposal.snapshot
    issue = observed(state)
    if change == "closed":
        issue = replace(issue, issue=replace(issue.issue, state="closed"))
    result = reconcile(proposal, issue=issue,
                       linkage=LinkedPrPresent(issue.key, 99, "head", NOW) if change == "pr" else None,
                       rejected=state.intent.required_commands if change == "rejected" else (),
                       external_holds=("ai-parked",) if change == "hold" else ())
    assert isinstance(result.snapshot.intent, SupersededRearm)
    assert result.snapshot.intent.reason is reason
    assert result.snapshot.episode == state.episode


@pytest.mark.parametrize("edit", ["body", "title", "comment"])
def test_conversation_and_requirement_edits_are_not_hold_facts(edit):
    """ADR 0015: only hold facts supersede a pending re-arm."""
    from issue_agent.domain.world import Comment

    proposal = pending()
    state = proposal.snapshot
    issue = observed(state)
    changed = (replace(issue.issue, body="new requirements") if edit == "body" else
               replace(issue.issue, title="new title") if edit == "title" else
               replace(issue.issue, comments=(Comment("human", "any update?", "2026-09-14T00:00:00Z"),)))
    result = reconcile(proposal, issue=replace(issue, issue=changed))
    assert isinstance(result, WorkflowUnchanged) and result.snapshot == state


@pytest.mark.parametrize("seconds", [-1, 301])
def test_freshness_failure_is_not_a_supersession_or_completion(seconds):
    with pytest.raises(ValueError, match="fresh"):
        reconcile(pending(), now=NOW + timedelta(seconds=seconds))


def test_failed_reads_back_off_and_only_actual_elapsed_time_can_open_age_hold():
    state = pending().snapshot
    history = reading(state)
    policy = ReconciliationPolicy(initial_delay_seconds=30, maximum_delay_seconds=1800, maximum_age_seconds=60)
    first = defer_rearm_observation(history, "offline", now=NOW, policy=policy)
    assert first.history.next_read_at == NOW + timedelta(seconds=30)
    assert update_rearm_observation(state, first, now=NOW, policy=policy) == state
    second = defer_rearm_observation(first, "offline", now=NOW + timedelta(seconds=30), policy=policy)
    assert second.history.next_read_at == NOW + timedelta(seconds=90)
    assert update_rearm_observation(state, second, now=NOW + timedelta(seconds=30), policy=policy) == state
    held = update_rearm_observation(state, second, now=NOW + timedelta(seconds=60), policy=policy)
    assert any(isinstance(g, OpenGate) and isinstance(g.cause, EvidenceUnavailable) for g in held.gates)
    assert held.episode == state.episode


def test_recovery_removes_its_own_observation_hold_without_clearing_another():
    state = pending().snapshot
    other = OpenGate("independent", state.binding.fence.generation,
                     EvidenceUnavailable("other", NOW, 1, "unknown"), NoAuthorizedRemediation("read required"))
    state = replace(state, gates=state.gates + (other,))
    policy = ReconciliationPolicy(maximum_reads=1)
    failed = defer_rearm_observation(reading(state), "git unavailable", now=NOW, policy=policy)
    held = update_rearm_observation(state, failed, now=NOW, policy=policy)
    assert len(held.gates) == len(state.gates) + 1
    recovered = update_rearm_observation(held, None, now=NOW, policy=policy)
    assert recovered.gates == state.gates
    assert recovered.episode == state.episode
    next_read = schedule_rearm_observation(failed, now=NOW + timedelta(seconds=90))
    assert next_read.last_error == "" and next_read.history.failed_reads == 0
    assert next_read.history.next_read_at > NOW + timedelta(seconds=90)


def test_excessive_failed_reads_saturate_and_cannot_overflow_backoff():
    history = replace(reading(pending().snapshot), history=ActiveReconciliation(NOW, NOW, 2**63 - 1), last_error="offline")
    result = defer_rearm_observation(history, "still offline", now=NOW)
    assert result.history.failed_reads == 2**63 - 1
    assert result.history.next_read_at == NOW + timedelta(seconds=1800)


@pytest.mark.parametrize("error", [None, "", "\x00", "x" * 16385])
def test_invalid_observation_failure_is_refused(error):
    with pytest.raises(ValueError):
        defer_rearm_observation(reading(pending().snapshot), error, now=NOW)


def test_observation_scope_mismatch_cannot_replace_another_hold():
    state = pending().snapshot
    wrong = replace(reading(state), request_id="another-request")
    with pytest.raises(ValueError):
        PendingResolveRearm(state, wrong)
    with pytest.raises(ValueError):
        update_rearm_observation(state, wrong, now=NOW)
    with pytest.raises(ValueError):
        UnavailableResolveRearm("request", "offline", NOW.replace(tzinfo=None))


def test_continued_handoff_keeps_observation_gate_identity_and_original_generation():
    state = pending().snapshot
    other = OpenGate("independent", state.binding.fence.generation,
                     EvidenceUnavailable("other", NOW, 1, "unknown"), NoAuthorizedRemediation("read required"))
    state = replace(state, gates=state.gates + (other,))
    policy = ReconciliationPolicy(maximum_reads=1)
    failed = defer_rearm_observation(reading(state), "git unavailable", now=NOW, policy=policy)
    held = update_rearm_observation(state, failed, now=NOW, policy=policy)
    assert isinstance(held.intent, PendingRearm)
    owned_gate = next(g for g in held.gates if g not in state.gates)
    assert isinstance(owned_gate, OpenGate)
    stopped = cancel_resolve_rearm(
        held, CancelRearm("cancel", held.revision, held.intent.request_id, "pause"), ACTOR,
    )
    assert isinstance(stopped, WorkflowChanged)
    assert isinstance(stopped.snapshot.intent, CancelledRearm)
    resumed = continue_resolve_rearm(
        stopped.snapshot,
        ContinueGrantedRearm("continued", stopped.snapshot.revision, stopped.snapshot.intent.request_id),
        ACTOR, observed(stopped.snapshot), no_pr(stopped.snapshot), now=NOW, active_attempts=(),
    )
    assert isinstance(resumed, WorkflowChanged)
    assert isinstance(resumed.snapshot.intent, PendingRearm)
    assert resumed.snapshot.binding.fence.generation > held.binding.fence.generation
    assert resumed.snapshot.intent.handoff == held.intent.handoff
    # Continue has a fresh request/read budget, not permission to drop the
    # already visible uncertainty of its retained workspace obligation.
    policy = ReconciliationPolicy(maximum_reads=10)
    again = defer_rearm_observation(reading(resumed.snapshot), "still unavailable", now=NOW, policy=policy)
    assert again.history.failed_reads < policy.maximum_reads
    still_held = update_rearm_observation(resumed.snapshot, again, now=NOW, policy=policy)
    retained = next(g for g in still_held.gates if isinstance(g, OpenGate) and g.gate_id == owned_gate.gate_id)
    assert retained.opened_generation == owned_gate.opened_generation
    assert sum(isinstance(g, OpenGate) and g.gate_id == owned_gate.gate_id for g in still_held.gates) == 1
    cleared = update_rearm_observation(still_held, None, now=NOW, policy=policy)
    assert all(not isinstance(g, OpenGate) or g.gate_id != owned_gate.gate_id for g in cleared.gates)
    assert other in cleared.gates
    assert isinstance(cleared.intent, PendingRearm)
    assert cleared.intent.handoff == state.intent.handoff


@pytest.mark.parametrize("case", ["list", "wrong-type", "foreign-command", "duplicate", "verified-overlap", "rejected-overlap"])
@pytest.mark.parametrize("terminal", [False, True])
def test_malformed_uncertainty_cannot_complete_or_supersede_rearm(case, terminal):
    proposal = pending()
    state = proposal.snapshot
    required = state.intent.required_commands[0]
    uncertain = RemoteOutcomeUncertain(required, NOW, 1, "read failed")
    malformed = {
        "list": [uncertain],
        "wrong-type": ("not an uncertainty observation",),
        "foreign-command": (replace(uncertain, command_id=CommandId("foreign-command")),),
        "duplicate": (uncertain, uncertain),
        "verified-overlap": (uncertain,),
        "rejected-overlap": (uncertain,),
    }[case]
    issue = observed(state, labels=frozenset({Label.AI_READY}))
    if terminal:
        issue = replace(issue, issue=replace(issue.issue, state="closed"))
    with pytest.raises(ValueError, match="uncertainty"):
        reconcile(
            proposal, uncertainty=malformed, issue=issue,
            verified=(required,) if case == "verified-overlap" else (),
            rejected=(required,) if case == "rejected-overlap" else (),
            workspace=workspace_ack(state.intent.handoff, NOW),
        )


def test_unchanged_preflight_cannot_fabricate_decision_changed_supersession():
    proposal = pending()
    state = proposal.snapshot
    stopped = supersede_resolve_rearm(state, StopReason.DECISION_CHANGED, "claimed-change")
    assert isinstance(stopped, WorkflowChanged)
    accepted = proposal.audit.preflight
    unchanged = ResolveRearmPreflight(accepted.issue, accepted.linkage, accepted.external_holds)
    with pytest.raises(ValueError, match="changed hold facts"):
        validate_resolve_transition(
            state, stopped, ceiling=3, now=NOW, observations=(unchanged,),
            accepted_preflight=accepted,
        )
