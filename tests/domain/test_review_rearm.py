"""Pending review re-arm recovery, projection obsolescence and dispatch holds (ADR 0015, increment 5)."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_rearm import RearmObservation, defer_rearm_observation
from issue_agent.domain.review_rearm import (
    PendingReviewRecovery, ReleaseProjection, ReleaseStatus, ReviewControlView, decide_review_rearm_reconciliation,
    update_review_rearm_observation,
)
from issue_agent.domain.review_workflow import (
    CompletedReviewRearm, PendingReviewRearm, PullRequestState, ReviewProjectionRefused, SupersededReviewRearm,
)
from issue_agent.domain.review_workflow_policy import (
    ReleaseProjectionPending, ReviewHeld, cancel_review_rearm, review_dispatch_decision,
    review_projection_obsolete, review_rearm_observation_gate_id,
)
from issue_agent.domain.workflow import (
    ActiveReconciliation, CancelRearm, RearmingGate, EvidenceUnavailable, NoAuthorizedRemediation, OpenGate, ReconciliationPolicy,
    RemoteOutcomeUncertain, RetryAuthorizationRequired, StopReason, WorkflowChanged, WorkflowRejected,
    WorkflowUnchanged,
)
from issue_agent.domain.workflow_reconciliation import is_prewrite_gate, prewrite_evidence_gate_id
from tests.domain.test_review_workflow_policy import (
    ACTOR, TRIGGER, accepted, dismissed, gated, observe, reconcile, rejections, uncertain_gate,
)
from tests.fakes.review_workflow import HEAD, NOW, OTHER_HEAD, PATCH

POLICY = ReconciliationPolicy(maximum_reads=3, maximum_age_seconds=3600)


def pending():
    change = accepted()
    return change.snapshot, change.audit.preflight


def prewrite_gate(command, generation=1):
    return OpenGate(prewrite_evidence_gate_id(command), generation,
                    EvidenceUnavailable(f"pre-write read of {command}", NOW, 5, "partial review read"),
                    NoAuthorizedRemediation("a complete read is required"))


def other_gate(generation=1):
    return OpenGate("gate:other", generation, EvidenceUnavailable("something else", NOW, 3, "down"),
                    NoAuthorizedRemediation("wait"))


def gid(gate):
    return (gate.gate if isinstance(gate, RearmingGate) else gate).gate_id


def seed(state, *, at=NOW):
    return RearmObservation(state.intent.request_id, state.binding, ActiveReconciliation(at, at, 0))


def failed(observation, times, *, start=NOW):
    for index in range(times):
        observation = defer_rearm_observation(observation, f"read {index} failed", now=start + timedelta(seconds=index),
                                              policy=POLICY)
    return observation


# -- bounded observation schedule ----------------------------------------------------------------

def test_failed_reads_below_the_bound_add_no_hold():
    state, _ = pending()
    assert update_review_rearm_observation(state, failed(seed(state), 2), now=NOW + timedelta(seconds=5),
                                           policy=POLICY) == state


def test_the_read_bound_opens_one_visible_hold_and_a_success_removes_it():
    state, _ = pending()
    gated_state = update_review_rearm_observation(state, failed(seed(state), 3), now=NOW + timedelta(seconds=5),
                                                  policy=POLICY)
    (gate,) = [g for g in gated_state.gates if gid(g) == review_rearm_observation_gate_id("request:1")]
    assert isinstance(gate.cause, EvidenceUnavailable) and gate.cause.failed_reads == 3
    assert gated_state.revision.version == state.revision.version + 1
    # Once open the hold persists through further failures, keeping its generation.
    again = update_review_rearm_observation(gated_state, failed(seed(state), 4), now=NOW + timedelta(seconds=9),
                                            policy=POLICY)
    assert [g.opened_generation for g in again.gates if gid(g) == gate.gate_id] == [gate.opened_generation]
    cleared = update_review_rearm_observation(again, None, now=NOW + timedelta(seconds=10), policy=POLICY)
    assert all(gid(g) != gate.gate_id for g in cleared.gates)
    assert cleared.revision.version == again.revision.version + 1


def test_the_age_bound_opens_the_hold_even_with_few_reads():
    state, _ = pending()
    late = NOW + timedelta(seconds=POLICY.maximum_age_seconds)
    held = update_review_rearm_observation(state, failed(seed(state), 1), now=late, policy=POLICY)
    assert any(gid(g) == review_rearm_observation_gate_id("request:1") for g in held.gates)


@pytest.mark.parametrize("breaks", ["request", "binding", "naive", "policy", "not-pending"])
def test_observation_updates_refuse_foreign_or_malformed_inputs(breaks):
    state, _ = pending()
    observation = failed(seed(state), 1)
    now = NOW + timedelta(seconds=5)
    policy = POLICY
    if breaks == "request":
        observation = replace(observation, request_id="request:other")
    elif breaks == "binding":
        observation = replace(observation, binding=gated().binding)
    elif breaks == "naive":
        now = datetime(2026, 9, 25, 12)
    elif breaks == "policy":
        policy = None
    else:
        state = gated()
    with pytest.raises(ValueError):
        update_review_rearm_observation(state, observation, now=now, policy=policy)


def test_pending_recovery_requires_its_exact_observation():
    state, _ = pending()
    PendingReviewRecovery(state, seed(state))
    with pytest.raises(ValueError):
        PendingReviewRecovery(state, replace(seed(state), request_id="request:2"))
    with pytest.raises(ValueError):
        PendingReviewRecovery(gated(), seed(state))


# -- reconciliation decisions --------------------------------------------------------------------

def decide(state, accepted_preflight, pr, *, verified=(), rejected=(), uncertainty=(), holds=(), now=NOW):
    return decide_review_rearm_reconciliation(
        state, accepted_preflight, pr, verified_commands=verified, rejected_commands=rejected,
        uncertainty=uncertainty, evidence_id="pr-observation:1", now=now, ceiling=3, trigger_label=TRIGGER,
        external_holds=holds,
    )


def released(labels=(TRIGGER,), **kwargs):
    return observe(dismissed(rejections(2), "r0", "r1"), labels=labels, **kwargs)


def test_every_verified_obligation_with_a_released_projection_completes():
    state, preflight = pending()
    result = decide(state, preflight, released(), verified=state.intent.required_commands)
    assert isinstance(result, WorkflowChanged) and isinstance(result.snapshot.intent, CompletedReviewRearm)


def test_unverified_obligations_keep_waiting():
    state, preflight = pending()
    assert isinstance(decide(state, preflight, released(), verified=state.intent.required_commands[:1]),
                      WorkflowUnchanged)


def test_a_verified_but_unreleased_projection_is_refused_not_completed():
    state, preflight = pending()
    result = decide(state, preflight, released(labels=(Label.NEEDS_HUMAN_REVIEW,)),
                    verified=state.intent.required_commands)
    assert isinstance(result, WorkflowRejected)


@pytest.mark.parametrize("pr,reason", [
    (observe(rejections(2), head=OTHER_HEAD), StopReason.DECISION_CHANGED),
    (observe(rejections(2), state=PullRequestState.CLOSED), StopReason.TARGET_TERMINAL),
])
def test_head_or_state_changes_supersede(pr, reason):
    state, preflight = pending()
    result = decide(state, preflight, pr)
    assert isinstance(result.snapshot.intent, SupersededReviewRearm) and result.snapshot.intent.reason is reason


def test_changed_external_holds_supersede_as_decision_changed():
    state, preflight = pending()
    result = decide(state, preflight, observe(rejections(2)), holds=("ci is red",))
    assert result.snapshot.intent.reason is StopReason.DECISION_CHANGED


def test_a_refused_obligation_supersedes_as_required_effect_rejected():
    state, preflight = pending()
    refused = state.intent.required_commands[0]
    result = decide(state, preflight, observe(rejections(2)), rejected=(refused,))
    intent = result.snapshot.intent
    assert intent.reason is StopReason.REQUIRED_EFFECT_REJECTED and refused in intent.retired_obligations


def test_a_moved_head_outranks_a_refusal():
    state, preflight = pending()
    result = decide(state, preflight, observe(rejections(2), head=OTHER_HEAD),
                    rejected=(state.intent.required_commands[0],))
    assert result.snapshot.intent.reason is StopReason.DECISION_CHANGED


def test_a_workflow_without_a_pending_intent_is_unchanged():
    state, preflight = pending()
    done = decide(state, preflight, released(), verified=state.intent.required_commands).snapshot
    assert isinstance(decide(done, preflight, released()), WorkflowUnchanged)


@pytest.mark.parametrize("breaks", ["stale", "future", "foreign", "duplicate", "unknown", "both", "untyped"])
def test_reconciliation_refuses_malformed_facts(breaks):
    state, preflight = pending()
    required = state.intent.required_commands
    kwargs = dict(verified=(), rejected=(), uncertainty=())
    pr = released()
    now = NOW
    if breaks == "stale":
        now = NOW + timedelta(seconds=301)
    elif breaks == "future":
        now = NOW - timedelta(seconds=1)
    elif breaks == "foreign":
        from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey
        pr = replace(pr, key=WorkflowKey("o/r", PullRequestRef(10)))
    elif breaks == "duplicate":
        kwargs["verified"] = (required[0], required[0])
    elif breaks == "unknown":
        kwargs["verified"] = (CommandId("not-required"),)
    elif breaks == "both":
        kwargs.update(verified=(required[0],), rejected=(required[0],))
    else:
        kwargs["uncertainty"] = ("timed out",)
    with pytest.raises(ValueError):
        decide(state, preflight, pr, now=now, **kwargs)


# -- projection obsolescence (the increment-3 residual) --------------------------------------------

def test_a_fresh_pending_projection_is_not_obsolete():
    state, _ = pending()
    assert state.intent.projection.remove is Label.NEEDS_HUMAN_REVIEW
    assert not review_projection_obsolete(state)


def test_the_intents_own_reads_and_uncertainty_are_not_hold_facts():
    state, _ = pending()
    own = state.intent.required_commands[0]
    own_gates = (
        prewrite_gate(own),
        OpenGate(review_rearm_observation_gate_id("request:1"), 1,
                 EvidenceUnavailable("reconciliation", NOW, 20, "down"), NoAuthorizedRemediation("wait")),
        replace(uncertain_gate(str(own), "gate:own-uncertain"), opened_generation=1),
    )
    assert not review_projection_obsolete(replace(state, gates=state.gates + own_gates))


def test_an_independent_hold_or_external_hold_makes_a_release_projection_obsolete():
    state, _ = pending()
    assert review_projection_obsolete(replace(state, gates=state.gates + (other_gate(),)))
    assert review_projection_obsolete(state, ("ci is red",))


def test_a_hold_projection_is_obsolete_once_every_independent_hold_is_gone():
    change = accepted(holds=("ci is red",))
    state = change.snapshot
    assert state.intent.projection.remove is None
    assert not review_projection_obsolete(state, ("ci is red",))
    assert review_projection_obsolete(state, ())


# -- stops and advances end holds nothing can clear ------------------------------------------------

def test_a_stop_drops_its_own_reads_and_proven_absence_but_keeps_uncertainty():
    state, _ = pending()
    first, second = state.intent.required_commands[:2]
    absence = OpenGate("gate:absent", 1, RetryAuthorizationRequired(first, "absence:1"),
                       NoAuthorizedRemediation("retry is unsupported for review commands"))
    uncertain = replace(uncertain_gate(str(second), "gate:uncertain"), opened_generation=1)
    observation_gate = OpenGate(review_rearm_observation_gate_id("request:1"), 1,
                                EvidenceUnavailable("reconciliation", NOW, 20, "down"), NoAuthorizedRemediation("wait"))
    before = replace(state, gates=state.gates + (prewrite_gate(first), absence, uncertain, observation_gate))
    uncertainty = (uncertain.cause,)
    stopped = cancel_review_rearm(before, CancelRearm("cancel:1", before.revision, "request:1", "stuck"), ACTOR,
                                  verified_commands=(), uncertainty=uncertainty)
    ids = {gid(g) for g in stopped.snapshot.gates}
    assert prewrite_evidence_gate_id(first) not in ids and "gate:absent" not in ids
    assert review_rearm_observation_gate_id("request:1") not in ids
    assert "gate:uncertain" in ids


def test_a_generation_advance_drops_every_prewrite_hold():
    state = gated()
    stuck = replace(prewrite_gate(CommandId("old-submission"), 0))
    with_hold = replace(state, gates=state.gates + (stuck,))
    release = reconcile(with_hold, observe(dismissed(rejections(2), "r0", "r1")))
    assert isinstance(release, WorkflowChanged)
    assert not any(is_prewrite_gate(g) for g in release.snapshot.gates)
    request = accepted(state=with_hold)
    assert not any(is_prewrite_gate(g) for g in request.snapshot.gates)


def test_only_evidence_holds_with_the_prewrite_prefix_are_prewrite_gates():
    assert is_prewrite_gate(prewrite_gate(CommandId("c")))
    assert not is_prewrite_gate(other_gate())
    assert not is_prewrite_gate(replace(uncertain_gate("c", prewrite_evidence_gate_id(CommandId("c")))))


# -- dispatch holds and the release projection -------------------------------------------------------

def test_an_unverified_release_holds_dispatch_after_gates():
    state = reconcile(gated(), observe(dismissed(rejections(2), "r0", "r1"))).snapshot
    pr = released()
    command = CommandId("release:1")
    assert review_dispatch_decision(state, pr, patch_id=PATCH, release_pending=command) == ReleaseProjectionPending(command)
    gated_state = replace(state, gates=(other_gate(state.revision.fence.generation),))
    assert isinstance(review_dispatch_decision(gated_state, pr, patch_id=PATCH, release_pending=command), ReviewHeld)


def test_a_refused_projection_is_a_review_gate_cause_only():
    cause = ReviewProjectionRefused("release:1", 1, "hold labels changed")
    OpenGate("review-projection-refused:x", 1, cause, NoAuthorizedRemediation("fix labels"))
    with pytest.raises(ValueError):
        ReviewProjectionRefused("", 1, "reason")
    from issue_agent.domain.workflow import WorkflowSnapshot
    from tests.fakes.workflow_authority import resolve_snapshot
    resolve = resolve_snapshot()
    with pytest.raises(ValueError):
        replace(resolve, gates=(OpenGate("g", resolve.revision.fence.generation, cause, NoAuthorizedRemediation("x")),))
    assert WorkflowSnapshot


def test_release_projection_and_view_invariants():
    assert ReleaseProjection(CommandId("release:1"), ReleaseStatus.VERIFIED).verified
    assert not ReleaseProjection(CommandId("release:1"), ReleaseStatus.REFUSED).verified
    with pytest.raises(ValueError):
        ReleaseProjection(CommandId("release:1"), "verified")
    state, _ = pending()
    trigger = Label.AI_PR_OPEN
    with pytest.raises(ValueError):
        ReviewControlView(state, "request:1", state.intent, None,  # pending control carries its schedule
                          trigger_label=trigger)
    ReviewControlView(state, "request:1", state.intent, seed(state), trigger_label=trigger)
    with pytest.raises(ValueError):
        ReviewControlView(gated(), None, None, seed(state), trigger_label=trigger)
    with pytest.raises(ValueError):
        ReviewControlView(state, "request:1", state.intent, seed(state), trigger_label=Label.NEEDS_HUMAN_REVIEW)
    assert HEAD and isinstance(state.intent, PendingReviewRearm) and RemoteOutcomeUncertain


# -- adversarial review fixes -----------------------------------------------------------------------

def refused_gate(generation=0, command="release:0"):
    return OpenGate(f"review-projection-refused:{command}", generation,
                    ReviewProjectionRefused(command, generation, "hold labels changed"),
                    NoAuthorizedRemediation("correct the labels by hand"))


def test_every_generation_advance_supersedes_a_refused_release_gate():
    state = gated()
    with_refused = replace(state, gates=state.gates + (refused_gate(),))
    release = reconcile(with_refused, observe(dismissed(rejections(2), "r0", "r1")))
    assert refused_gate() not in release.snapshot.gates
    request = accepted(state=with_refused)
    assert refused_gate() not in request.snapshot.gates
    # Not counted as a second, independent hold: the re-arm still plans a release.
    assert request.snapshot.intent.projection.remove is Label.NEEDS_HUMAN_REVIEW
    stopped = cancel_review_rearm(
        replace(request.snapshot, gates=request.snapshot.gates + (refused_gate(1, "release:1"),)),
        CancelRearm("c", replace(request.snapshot.revision), "request:1", "why"), ACTOR,
    )
    assert refused_gate(1, "release:1") in stopped.snapshot.gates  # a stop is no advance: it stays visible


def test_a_refused_projection_records_its_own_generation():
    with pytest.raises(ValueError):
        ReviewProjectionRefused("release:1", -1, "reason")
    with pytest.raises(ValueError):
        ReviewProjectionRefused("release:1", True, "reason")


def test_a_refusal_with_a_partial_read_is_undecidable_not_a_stop():
    state, preflight = pending()
    result = decide(state, preflight, observe(rejections(2), partial=True),
                    rejected=(state.intent.required_commands[0],))
    assert isinstance(result, WorkflowRejected)


def test_a_refusal_with_a_read_from_before_acceptance_is_undecidable():
    state, preflight = pending()
    early = observe(rejections(2), at=NOW - timedelta(seconds=5))
    result = decide(state, preflight, early, rejected=(state.intent.required_commands[0],))
    assert isinstance(result, WorkflowRejected)


def retry_setup(state, command):
    from issue_agent.domain.workflow import CommandAbsenceEvidence, RearmObligation
    from issue_agent.domain.workflow_retry import RetryAuthorizationFacts
    history = ActiveReconciliation(NOW - timedelta(hours=2), NOW, 3)
    gate_id = "gate:retry"
    evidence = CommandAbsenceEvidence("absence:1", command, state.binding, NOW, HEAD)
    gate = OpenGate(gate_id, state.revision.fence.generation, RetryAuthorizationRequired(command, "absence:1"),
                    NoAuthorizedRemediation("grant one attempt"))
    from issue_agent.domain.workflow import ReconciliationOnly
    facts = RetryAuthorizationFacts(command, state.binding, ReconciliationOnly(history, gate_id), evidence,
                                    RearmObligation(state.intent.request_id), obligation_active=True,
                                    workspace_ready=True)
    return replace(state, gates=state.gates + (gate,)), gate, facts


def test_review_retry_grants_share_the_resolve_contract_and_validate():
    from issue_agent.domain.review_workflow_policy import validate_review_transition
    from issue_agent.domain.workflow import AuthorizeAbsentRetry, WorkflowMutation
    from issue_agent.domain.workflow_retry import authorize_absent_retry, retry_authorization_available
    from tests.domain.test_review_workflow_policy import facts
    state, _ = pending()
    command = state.intent.required_commands[0]
    held, gate, retry = retry_setup(state, command)
    assert retry_authorization_available(held, gate, retry)
    request = AuthorizeAbsentRetry("grant:1", held.revision, command, gate.gate_id, "absence:1")
    change = authorize_absent_retry(held, request, ACTOR, retry)
    assert change.mutation is WorkflowMutation.AUTHORIZE_RETRY
    assert change.snapshot == replace(held, revision=replace(held.revision, version=held.revision.version + 1))
    validate_review_transition(held, change, facts(retry=retry))
    with pytest.raises(ValueError, match="retry facts"):
        validate_review_transition(held, change, facts())
    # Facts that no longer admit the grant (a retired obligation) refuse it at validation.
    with pytest.raises(ValueError):
        validate_review_transition(held, change, facts(retry=replace(retry, obligation_active=False)))
    wrong = AuthorizeAbsentRetry("grant:1", held.revision, command, gate.gate_id, "absence:other")
    assert isinstance(authorize_absent_retry(held, wrong, ACTOR, retry), WorkflowRejected)
