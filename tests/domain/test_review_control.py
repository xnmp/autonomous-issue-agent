"""Operator actions and typed hold kinds over a review control view (ADR 0015, increment 7)."""

from dataclasses import replace

import pytest

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_rearm import PendingRetry, RearmObservation
from issue_agent.domain.review_control import (
    ActionOffered, ActionRefused, DismissalObligation, EvidenceHold, ProjectionObligation, RefusedProjection,
    ReviewOperatorCommand, UnlabelledObligation, evidence_hold, rearm_obligations, refused_projection,
    review_gate_actions,
)
from issue_agent.domain.review import UNPRODUCTIVE_ROUND_HOLD
from issue_agent.domain.review_dispatch_record import (
    DecisionBasis, DecisionRead, RecordedReviewDecision, ReviewNotAdmitted, ReviewTerminal,
)
from issue_agent.domain.review_evidence import ReviewEvidenceKind, evidence_gate_id
from issue_agent.domain.review_rearm import (
    GateFactsUnavailable, HoldRestoration, MigrationRequiredReviewControl, RearmCommandProgress, RearmCommandStatus,
    RetryRefusal, ReviewControlView, UnreadableHoldRestoration, UnreadableReviewDecision,
)
from issue_agent.domain.review_rejection import MAX_COUNTED_REVIEWS
from issue_agent.domain.review_workflow import PendingReviewRearm, PullRequestState, ReviewProjectionRefused
from issue_agent.domain.review_workflow_policy import (
    ReviewHeld, review_dismissal_command_id, review_rearm_observation_gate_id, review_release_command_id,
)
from issue_agent.domain.workflow import (
    ActiveReconciliation, EvidenceUnavailable, NoAuthorizedRemediation, OpenGate, RearmNeedsDecision,
    RemoteOutcomeUncertain, RetryAuthorizationRequired,
)
from issue_agent.domain.workflow_identity import WorkflowFamily, WorkflowKey, PullRequestRef
from issue_agent.domain.workflow_reconciliation import prewrite_evidence_gate_id
from tests.domain import test_review_workflow_policy as policy
from tests.fakes.review_workflow import (
    GATE, HEAD, NOW, PR_KEY, cancelled, ceiling_cause, ceiling_gate, completed, counted, held,
    pending, pending_intent, superseded,
)


TRIGGER = Label.AI_PR_OPEN


def gated(*gates, generation=0):
    return replace(held(generation=generation), gates=tuple(gates))


def decided(decision=None, *, state=PullRequestState.OPEN, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}),
            holds=(), key=PR_KEY):
    """The review daemon's recorded decision, as the plan would write it."""
    if decision is None:
        decision = (ReviewTerminal(state) if state is not PullRequestState.OPEN
                    else ReviewNotAdmitted((Label.NEEDS_HUMAN_REVIEW.value,)))
    return RecordedReviewDecision(key, decision, DecisionBasis(HEAD, state, labels, holds),
                                  DecisionRead.REVIEW_READ, NOW, NOW)


def view(snapshot, **kwargs):
    if isinstance(snapshot.intent, PendingReviewRearm) and "observation" not in kwargs:
        kwargs["observation"] = RearmObservation(snapshot.intent.request_id, snapshot.binding,
                                                 ActiveReconciliation(NOW, NOW, 0))
    observation = kwargs.pop("observation", None)
    kwargs.setdefault("trigger_label", TRIGGER)
    return ReviewControlView(snapshot, None, None, observation, **kwargs)


def retry_gate(gate_id="gate:retry", command="cmd:lost", generation=0):
    return OpenGate(gate_id, generation, RetryAuthorizationRequired(CommandId(command), "absence:1"),
                    NoAuthorizedRemediation("an operator grant is required"))


def evidence_gate(gate_id, generation=0):
    return OpenGate(gate_id, generation, EvidenceUnavailable("a read", NOW, 3, "down"),
                    NoAuthorizedRemediation("wait for a read"))


def only(actions):
    (action,) = actions
    return action


# -- actions -----------------------------------------------------------------------------------------

def test_an_open_ceiling_offers_a_rearm_naming_how_many_reviews_it_dismisses():
    state = held()
    action = only(review_gate_actions(view(state), state.gates[0]))
    assert isinstance(action, ActionOffered) and action.command is ReviewOperatorCommand.REQUEST_REARM
    assert "2 counted bot reviews" in action.effect and "replaces" not in action.effect


def test_a_ceiling_beyond_the_authorization_bound_is_refused_with_its_visible_reason():
    gate = ceiling_gate(cause=ceiling_cause(count=MAX_COUNTED_REVIEWS + 1, ceiling=3))
    state = gated(gate)
    action = only(review_gate_actions(view(state), gate))
    assert isinstance(action, ActionRefused) and action.command is ReviewOperatorCommand.REQUEST_REARM
    assert f"{MAX_COUNTED_REVIEWS + 1} counted reviews" in action.reason


def test_exactly_the_bound_is_still_offered():
    gate = ceiling_gate(cause=ceiling_cause(count=MAX_COUNTED_REVIEWS, ceiling=3))
    state = gated(gate)
    assert isinstance(only(review_gate_actions(view(state), gate)), ActionOffered)


def test_a_live_review_run_refuses_rearm_and_continuation_until_it_drains():
    state = held()
    refused = only(review_gate_actions(view(state, review_runs_active=True), state.gates[0]))
    assert isinstance(refused, ActionRefused) and "drain" in refused.reason
    stopped = cancelled()
    decision = only(review_gate_actions(view(stopped, review_runs_active=True), stopped.gates[0]))
    assert isinstance(decision, ActionRefused) and decision.command is ReviewOperatorCommand.CONTINUE_REARM


def test_a_pending_rearm_offers_only_its_cancellation():
    state = pending()
    action = only(review_gate_actions(view(state), state.gates[0]))
    assert isinstance(action, ActionOffered) and action.command is ReviewOperatorCommand.CANCEL_REARM
    assert "request:1" in action.effect


def test_a_pending_rearm_refuses_a_second_request_on_another_ceiling():
    state = pending()
    other = replace(ceiling_gate(generation=1), gate_id="gate:review-ceiling-2")
    try:
        both = replace(state, gates=(*state.gates, other))
    except ValueError:
        pytest.skip("a pending review re-arm cannot coexist with a second open ceiling")
    action = only(review_gate_actions(view(both), other))
    assert isinstance(action, ActionRefused) and "request:1" in action.reason


@pytest.mark.parametrize("stopped", [cancelled, superseded])
def test_a_stopped_rearm_offers_its_continuation(stopped):
    state = stopped()
    action = only(review_gate_actions(view(state), state.gates[0]))
    assert isinstance(action, ActionOffered) and action.command is ReviewOperatorCommand.CONTINUE_REARM
    assert "request:1" in action.effect


def test_a_request_after_a_stop_says_it_replaces_the_stopped_request():
    state = cancelled()
    ceiling = ceiling_gate(generation=1)
    reopened = replace(state, gates=(*state.gates, ceiling))
    action = only(review_gate_actions(view(reopened), ceiling))
    assert isinstance(action, ActionOffered) and "replaces stopped request request:1" in action.effect
    continuation = only(review_gate_actions(view(reopened), state.gates[0]))
    assert continuation.command is ReviewOperatorCommand.CONTINUE_REARM


def test_a_retry_gate_is_offered_exactly_when_the_grant_policy_would_admit_it():
    gate = retry_gate()
    state = gated(gate)
    offered = only(review_gate_actions(view(state, retry_authorizable=frozenset({gate.gate_id})), gate))
    assert isinstance(offered, ActionOffered) and "cmd:lost" in offered.effect


def test_a_retry_gate_with_a_waiting_grant_names_it():
    gate = retry_gate()
    state = gated(gate)
    waiting = view(state, retry_pending=(PendingRetry(gate.gate_id, "grant:1", "cmd:lost"),))
    action = only(review_gate_actions(waiting, gate))
    assert isinstance(action, ActionRefused) and "grant:1" in action.reason


def test_a_retry_gate_the_policy_refuses_shows_that_reason_or_a_fallback():
    gate = retry_gate()
    state = gated(gate)
    explained = view(state, retry_refusals=(RetryRefusal(gate.gate_id, "absence evidence expired"),))
    assert only(review_gate_actions(explained, gate)).reason == "absence evidence expired"
    fallback = only(review_gate_actions(view(state), gate))
    assert isinstance(fallback, ActionRefused) and fallback.reason


@pytest.mark.parametrize("gate", [
    evidence_gate("review-history:x"),
    OpenGate("uncertain:1", 0, RemoteOutcomeUncertain(CommandId("cmd"), NOW, 2, "timeout"),
             NoAuthorizedRemediation("reconciling")),
    OpenGate("review-projection-refused:x", 0, ReviewProjectionRefused("cmd", 0, "labels changed"),
             NoAuthorizedRemediation("fix labels")),
])
def test_holds_that_only_reconciliation_ends_offer_no_operator_command(gate):
    state = gated(gate)
    assert review_gate_actions(view(state), gate) == ()


def test_actions_are_derived_only_for_a_gate_of_the_view():
    state = held()
    with pytest.raises(ValueError):
        review_gate_actions(view(state), retry_gate())
    with pytest.raises(ValueError):
        review_gate_actions(state, state.gates[0])  # type: ignore[arg-type]


# -- the recorded decision gates what is offered ------------------------------------------------------

def _terminal_cases():
    retry = retry_gate()
    return [(snapshot, {"retry_authorizable": frozenset({retry.gate_id})} if snapshot.gates[0] == retry else {})
            for snapshot in (held(), pending(), cancelled(), gated(retry))]


def test_a_pull_request_the_daemon_recorded_merged_offers_nothing_on_any_gate():
    decision = decided(state=PullRequestState.MERGED)
    for snapshot, extra in _terminal_cases():
        actions = review_gate_actions(view(snapshot, decision=decision, **extra), snapshot.gates[0])
        assert actions and all(isinstance(action, ActionRefused) for action in actions)
        for action in actions:
            assert "merged" in action.reason and NOW.isoformat() in action.reason
            assert "left as they are" in action.reason and "retire" not in action.reason


def test_a_pull_request_the_daemon_recorded_closed_keeps_its_lever_and_says_acceptance_refuses_while_closed():
    decision = decided(state=PullRequestState.CLOSED)
    for snapshot, extra in _terminal_cases():
        open_actions = review_gate_actions(view(snapshot, **extra), snapshot.gates[0])
        closed_actions = review_gate_actions(view(snapshot, decision=decision, **extra), snapshot.gates[0])
        # The same commands in the same states: a reopen finds its lever intact.
        assert [(type(a), a.command) for a in closed_actions] == [(type(a), a.command) for a in open_actions]
        for opened, closed in zip(open_actions, closed_actions):
            if isinstance(closed, ActionOffered) and closed.command is not ReviewOperatorCommand.CANCEL_REARM:
                # Request, continuation and retry acceptance each re-read the pull request.
                assert closed.effect.startswith(opened.effect)
                assert f"closed (as of {NOW.isoformat()}); acceptance re-reads GitHub and refuses while it stays closed" \
                    in closed.effect
            else:
                assert closed == opened  # cancellation never reads the pull request
    retry = retry_gate()
    closed_retry = only(review_gate_actions(view(gated(retry), decision=decision,
                                                 retry_authorizable=frozenset({retry.gate_id})), retry))
    assert closed_retry.command is ReviewOperatorCommand.AUTHORIZE_RETRY and "refuses while it stays closed" in \
        closed_retry.effect


def test_an_open_decision_or_none_leaves_the_lever_offered():
    state = held()
    for decision in (None, decided(), UnreadableReviewDecision("malformed row")):
        assert isinstance(only(review_gate_actions(view(state, decision=decision), state.gates[0])), ActionOffered)


def test_a_gate_whose_facts_disagree_offers_nothing_and_leaves_other_gates_alone():
    retry = retry_gate()
    state = gated(ceiling_gate(), retry)
    degraded = view(state, gate_facts_unavailable=(GateFactsUnavailable(retry.gate_id, "2 open retry grants"),))
    refused = only(review_gate_actions(degraded, retry))
    assert isinstance(refused, ActionRefused) and "2 open retry grants" in refused.reason
    assert isinstance(only(review_gate_actions(degraded, state.gates[0])), ActionOffered)


def test_a_rearm_with_no_known_remaining_hold_says_it_returns_the_pull_request_to_review():
    state = held()
    effect = only(review_gate_actions(view(state, decision=decided()), state.gates[0])).effect
    assert "returns to review" in effect and "buys no review" not in effect


@pytest.mark.parametrize("labels,holds,named", [
    (frozenset({Label.NEEDS_HUMAN_REVIEW, Label.AI_PARKED}), (Label.AI_PARKED.value,), Label.AI_PARKED.value),
    (frozenset({Label.NEEDS_HUMAN_REVIEW}), (UNPRODUCTIVE_ROUND_HOLD,), UNPRODUCTIVE_ROUND_HOLD),
])
def test_a_rearm_never_promises_a_review_while_a_hold_it_keeps_remains(labels, holds, named):
    state = held()
    effect = only(review_gate_actions(view(state, decision=decided(labels=labels, holds=holds)),
                                      state.gates[0])).effect
    assert "buys no review" in effect and "holds a re-arm keeps" in effect and named in effect
    assert NOW.isoformat() in effect and "returns to review" not in effect


@pytest.mark.parametrize("labels,named", [
    (frozenset({Label.NEEDS_HUMAN_REVIEW, Label.NO_AI_REVIEW}), Label.NO_AI_REVIEW.value),
    (frozenset({Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_DESIGN, Label.AI_DONE}), "ai-done, needs-design"),
    # Only the read's trusted external holds are kept by a re-arm; an untrusted parked label is not.
    (frozenset({Label.NEEDS_HUMAN_REVIEW, Label.AI_PARKED}), Label.AI_PARKED.value),
])
def test_a_rearm_says_dispatch_still_skips_labels_it_does_not_keep(labels, named):
    state = held()
    effect = only(review_gate_actions(view(state, decision=decided(labels=labels, holds=())),
                                      state.gates[0])).effect
    assert f"review dispatch still skips it for {named}" in effect and "no review is bought" in effect
    assert "keeps needs-human-review" not in effect and "one fresh review is owed" not in effect


def test_the_ceilings_own_needs_human_review_is_never_described_as_kept():
    state = held()
    effect = only(review_gate_actions(view(state, decision=decided(labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))),
                                      state.gates[0])).effect
    assert "returns to review" in effect and "one fresh review is owed" in effect
    assert "needs-human-review" not in effect.split("unless")[0]


def test_a_gate_a_rearm_keeps_is_named_but_gates_its_advance_ends_or_absorbs_are_not():
    kept = retry_gate()
    state = gated(ceiling_gate(), kept)
    effect = only(review_gate_actions(view(state), state.gates[0])).effect
    assert "1 other open gate" in effect and "buys no review" in effect
    # Unreadable review evidence belongs to the old generation's dispatch: the advance ends it.
    ended = gated(ceiling_gate(), evidence_gate(evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, 9, HEAD)))
    assert "returns to review" in only(review_gate_actions(view(ended), ended.gates[0])).effect
    stopped = cancelled()
    ceiling = ceiling_gate(generation=1)
    reopened = replace(stopped, gates=(*stopped.gates, ceiling))
    assert "returns to review" in only(review_gate_actions(view(reopened), ceiling)).effect


def _paired(other):
    base = policy.gated()
    return replace(base, gates=(policy.ceiling_gate_of(base), other(base.revision.fence.generation)))


def _stopped_and_reopened(*, holds_then, uncertain):
    """A stopped re-arm whose retired obligation is uncertain, with the ceiling reopened for a new request."""
    pending_state = policy.accepted(holds=holds_then).snapshot
    intent = pending_state.intent
    command = intent.projection.command_id if uncertain == "relabel" else intent.required_commands[0]
    stopped = policy.stop(pending_state, uncertainty=(RemoteOutcomeUncertain(command, NOW, 4, "timed out"),)).snapshot
    return policy.changed(policy.reconcile(stopped, policy.observe(policy.rejections(2)))).snapshot


_OTHER_GATES = {
    "review history evidence": (lambda g: OpenGate(
        evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, PR_KEY.target.number, HEAD), g,
        EvidenceUnavailable("the complete review list", NOW, 3, "page 2 failed"), NoAuthorizedRemediation("wait")),
        False),
    "exemption trailer evidence": (lambda g: OpenGate(
        evidence_gate_id(ReviewEvidenceKind.EXEMPTION_TRAILERS, PR_KEY.target.number, HEAD), g,
        EvidenceUnavailable("commit trailers", NOW, 3, "timeout"), NoAuthorizedRemediation("wait")), False),
    "pre-write evidence": (lambda g: OpenGate(
        prewrite_evidence_gate_id(CommandId("dismiss:old")), g, EvidenceUnavailable("guard read", NOW, 3, "timeout"),
        NoAuthorizedRemediation("wait")), False),
    "refused projection": (lambda g: OpenGate(
        "review-projection-refused:x", g, ReviewProjectionRefused("escalation:1", g, "hold removed"),
        NoAuthorizedRemediation("wait")), False),
    "other evidence": (lambda g: OpenGate(
        "some-other-read:1", g, EvidenceUnavailable("a read", NOW, 3, "down"), NoAuthorizedRemediation("wait")), True),
    "retry authorization": (lambda g: retry_gate(generation=g), True),
    "unrelated uncertainty": (lambda g: OpenGate(
        "gate:uncertain", g, RemoteOutcomeUncertain(CommandId("submit:other"), NOW, 3, "timed out"),
        NoAuthorizedRemediation("reconcile only")), True),
}


def _parity_cases():
    for name, (build, kept) in _OTHER_GATES.items():
        yield pytest.param(lambda build=build: _paired(build), (), kept, id=name)
    yield pytest.param(lambda: policy.gated(), ("ai-parked",), True, id="trusted external hold")
    yield pytest.param(lambda: policy.gated(), (), False, id="ceiling alone")
    yield pytest.param(lambda: _stopped_and_reopened(holds_then=(), uncertain="dismissal"), (), False,
                       id="uncertainty over an absorbed retired dismissal")
    yield pytest.param(lambda: _stopped_and_reopened(holds_then=("ai-parked",), uncertain="relabel"), (), True,
                       id="uncertain relabel in the other direction")


@pytest.mark.parametrize("build,holds,kept", list(_parity_cases()))
def test_the_rearm_text_agrees_with_the_projection_acceptance_plans(build, holds, kept):
    state = build()
    ceiling = policy.ceiling_gate_of(state)
    effect = only(review_gate_actions(view(state, decision=decided(holds=holds)), ceiling)).effect
    projection = policy.accepted(state, command=policy.request_for(state, "request:parity"),
                                 holds=holds).snapshot.intent.projection
    assert (projection.remove is None) is kept
    if kept:
        assert "buys no review" in effect and "keeps needs-human-review" in effect
        assert "one fresh review is owed" not in effect
    else:
        assert projection.add is TRIGGER
        assert "returns to review and one fresh review is owed" in effect and "buys no review" not in effect


def _stopped(*, holds_then=(), uncertain=None, extra=None):
    """A stopped re-arm with its decision gate, optionally with an uncertain retired obligation or another gate."""
    pending_state = policy.accepted(holds=holds_then).snapshot
    intent = pending_state.intent
    uncertainty = ()
    if uncertain is not None:
        command = intent.projection.command_id if uncertain == "relabel" else intent.required_commands[0]
        uncertainty = (RemoteOutcomeUncertain(command, NOW, 4, "timed out"),)
    stopped = policy.stop(pending_state, uncertainty=uncertainty).snapshot
    if extra is not None:
        stopped = replace(stopped, gates=(*stopped.gates, extra(stopped.revision.fence.generation)))
    return stopped


@pytest.mark.parametrize("build,holds,kept", [
    pytest.param(lambda: _stopped(), (), False, id="decision gate alone"),
    pytest.param(lambda: _stopped(), ("ai-parked",), True, id="trusted external hold"),
    pytest.param(lambda: _stopped(uncertain="dismissal"), (), False, id="uncertainty over its own retired dismissal"),
    pytest.param(lambda: _stopped(holds_then=("ai-parked",), uncertain="relabel"), (), True,
                 id="uncertain relabel in the other direction"),
    pytest.param(lambda: _stopped(extra=lambda g: retry_gate(generation=g)), (), True, id="retry authorization"),
    pytest.param(lambda: _stopped(extra=_OTHER_GATES["review history evidence"][0]), (), False,
                 id="review history evidence"),
])
def test_the_continuation_text_agrees_with_the_projection_acceptance_plans(build, holds, kept):
    state = build()
    decision_gate = next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision))
    effect = only(review_gate_actions(view(state, decision=decided(holds=holds)), decision_gate)).effect
    projection = policy.changed(policy.carry_on(state, policy.rejections(2), holds=holds)).snapshot.intent.projection
    assert (projection.remove is None) is kept
    if kept:
        assert "buys no review" in effect and "one fresh review is owed" not in effect
    else:
        assert "returns to review and one fresh review is owed" in effect and "buys no review" not in effect


# -- typed hold kinds --------------------------------------------------------------------------------

@pytest.mark.parametrize("gate_id,kind", [
    (evidence_gate_id(ReviewEvidenceKind.EXEMPTION_TRAILERS, 9, HEAD), EvidenceHold.EXEMPTION_TRAILERS),
    (evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, 9, HEAD), EvidenceHold.REVIEW_HISTORY),
    (prewrite_evidence_gate_id(CommandId("cmd:1")), EvidenceHold.PREWRITE_READ),
    (review_rearm_observation_gate_id("request:1"), EvidenceHold.REARM_OBSERVATION),
    ("some-other-read:1", EvidenceHold.OTHER),
])
def test_each_evidence_gate_names_the_read_it_waits_on(gate_id, kind):
    assert evidence_hold(evidence_gate(gate_id)) is kind


def test_only_evidence_gates_have_an_evidence_kind():
    assert evidence_hold(ceiling_gate()) is None
    assert evidence_hold(retry_gate()) is None


def test_a_refused_release_is_distinguished_from_an_exhausted_escalation():
    state = held(generation=2)
    release = ReviewProjectionRefused(review_release_command_id(PR_KEY, 2), 2, "labels changed")
    escalation = ReviewProjectionRefused("escalation:1", 2, "hold removed")
    generation_zero = ReviewProjectionRefused("escalation:0", 0, "hold removed")
    assert refused_projection(state, release) is RefusedProjection.CEILING_RELEASE
    assert refused_projection(state, escalation) is RefusedProjection.ESCALATION_HOLD
    assert refused_projection(state, generation_zero) is RefusedProjection.ESCALATION_HOLD
    other = WorkflowKey("o/r", PullRequestRef(10))
    foreign = ReviewProjectionRefused(review_release_command_id(other, 2), 2, "labels changed")
    assert refused_projection(state, foreign) is RefusedProjection.ESCALATION_HOLD


# -- pending obligations -----------------------------------------------------------------------------

def test_each_required_command_names_the_obligation_it_discharges():
    reviews = counted()
    dismissals = tuple(review_dismissal_command_id(PR_KEY, "request:1", review.review_id) for review in reviews)
    intent = pending_intent(commands=(*dismissals, CommandId("request:1:projection")))
    state = replace(pending(), intent=intent)
    labelled = rearm_obligations(state, intent)
    assert [command for command, _ in labelled] == list(intent.required_commands)
    assert [o.review for _, o in labelled if isinstance(o, DismissalObligation)] == list(reviews)
    (projection,) = [o for _, o in labelled if isinstance(o, ProjectionObligation)]
    assert projection.plan.add is Label.AI_PR_OPEN and projection.plan.remove is Label.NEEDS_HUMAN_REVIEW


def test_a_command_no_obligation_derives_is_labelled_unknown_not_dropped():
    state = pending()
    labelled = rearm_obligations(state, state.intent)
    assert len(labelled) == len(state.intent.required_commands)
    assert any(isinstance(o, UnlabelledObligation) for _, o in labelled)  # fixture ids are not derived


# -- view invariants ---------------------------------------------------------------------------------

def test_migration_required_names_only_other_active_families():
    MigrationRequiredReviewControl(PR_KEY, (WorkflowFamily.RESOLVE,))
    for families in [(), (WorkflowFamily.REVIEW_REJECTIONS,), (WorkflowFamily.RESOLVE, WorkflowFamily.RESOLVE),
                     ["resolve"], ("resolve",)]:
        with pytest.raises(ValueError):
            MigrationRequiredReviewControl(PR_KEY, families)  # type: ignore[arg-type]


@pytest.mark.parametrize("restores,contested", [(-1, False), (2**31 + 1, False), (True, False), (1, "yes"),
                                               (1.0, False)])
def test_hold_restorations_are_bounded_typed_facts(restores, contested):
    with pytest.raises(ValueError):
        HoldRestoration(GATE, restores, contested)


def test_view_facts_must_belong_to_visible_gates_and_never_contradict():
    gate = retry_gate()
    state = gated(ceiling_gate(), gate)
    with pytest.raises(ValueError):  # a refusal for a gate that is not a retry gate
        view(state, retry_refusals=(RetryRefusal(GATE, "no"),))
    with pytest.raises(ValueError):  # offered and refused at once
        view(state, retry_authorizable=frozenset({gate.gate_id}), retry_refusals=(RetryRefusal(gate.gate_id, "no"),))
    with pytest.raises(ValueError):  # offered while a grant already waits
        view(state, retry_authorizable=frozenset({gate.gate_id}),
             retry_pending=(PendingRetry(gate.gate_id, "grant:1", "cmd:lost"),))
    with pytest.raises(ValueError):
        view(state, hold_restorations=(HoldRestoration("gate:gone", 1, False),))
    with pytest.raises(ValueError):
        view(state, hold_restorations=(HoldRestoration(GATE, 1, False), HoldRestoration(GATE, 2, True)))
    with pytest.raises(ValueError):  # another pull request's decision
        view(state, decision=decided(key=WorkflowKey("o/r", PullRequestRef(10))))
    with pytest.raises(ValueError):
        view(state, decision=ReviewHeld((GATE,)))  # type: ignore[arg-type]  # not a recorded decision
    with pytest.raises(ValueError):  # unavailable facts on a gate that is not visible
        view(state, gate_facts_unavailable=(GateFactsUnavailable("gate:gone", "x"),))
    with pytest.raises(ValueError):  # unavailable and offered at once
        view(state, retry_authorizable=frozenset({gate.gate_id}),
             gate_facts_unavailable=(GateFactsUnavailable(gate.gate_id, "x"),))
    with pytest.raises(ValueError):  # unavailable and waiting at once
        view(state, retry_pending=(PendingRetry(gate.gate_id, "grant:1", "cmd:lost"),),
             gate_facts_unavailable=(GateFactsUnavailable(gate.gate_id, "x"),))
    with pytest.raises(ValueError):
        view(state, hold_restorations=(HoldRestoration(GATE, 1, False), UnreadableHoldRestoration(GATE, "bad")))
    with pytest.raises(ValueError):
        view(state, review_runs_active=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        view(state, retry_refusals=tuple(RetryRefusal(gate.gate_id, "x") for _ in range(10_000)))


def test_command_progress_follows_exactly_the_pending_obligations():
    state = pending()
    commands = state.intent.required_commands
    view(state, command_progress=tuple(RearmCommandProgress(c, RearmCommandStatus.PENDING) for c in commands))
    with pytest.raises(ValueError):
        view(state, command_progress=(RearmCommandProgress(commands[0], RearmCommandStatus.VERIFIED),))
    with pytest.raises(ValueError):
        view(held(), command_progress=(RearmCommandProgress(CommandId("x"), RearmCommandStatus.PENDING),))
    with pytest.raises(ValueError):
        RearmCommandProgress(CommandId("x"), "verified")  # type: ignore[arg-type]


def test_a_huge_counted_set_projects_every_review_and_refuses_the_request():
    gate = ceiling_gate(cause=ceiling_cause(count=512, ceiling=3))
    state = gated(gate)
    assert len(state.gates[0].cause.counted) == 512
    action = only(review_gate_actions(view(state), gate))
    assert isinstance(action, ActionRefused)
    with pytest.raises(ValueError):
        ceiling_cause(count=513, ceiling=3)

