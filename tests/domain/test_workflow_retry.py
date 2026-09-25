"""Pure one-attempt retry authorization policy (ADR 0015)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.workflow import (
    ActiveReconciliation, AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm, ClosedRetryAuthorization,
    CommandAbsenceEvidence, RetryDisposition,
    ConsumedRetryAuthorization, NoAuthorizedRemediation, OpenGate, PendingRearm, RearmObligation,
    ReconciliationOnly, RemediationAttempted, RemediationOperation, RemediationOutcome,
    RemoteOutcomeUncertain, RetiredSettledCommand, RetiredUncertainCommand, RetryAuthorizationRequired,
    StandaloneCommand, UnconsumedRetryAuthorization, WorkflowChanged, WorkflowMutation, WorkflowRejected,
    WorkflowRejection,
)
from issue_agent.domain.workflow_policy import cancel_resolve_rearm, validate_resolve_transition
from issue_agent.domain.workflow_retry import (
    MAX_AUTHORIZED_ATTEMPTS, MAX_GRANT_AGE_SECONDS, MAX_GRANT_UNAVAILABLE_READS, BeginRetry, DeclineRetry,
    RetryAbsenceRead, grant_unavailable_void, RetryAuthorizationFacts, authorize_absent_retry,
    minted_authorization, retry_authorization_available, retry_spend_decision,
)
from tests.domain.test_workflow_policy import ACTOR, NOW, exhausted, snapshot, start

COMMAND = CommandId("command-1")
GATE = "uncertain-gate"
EVIDENCE = "absence-1"
SINCE = NOW - timedelta(hours=2)


def gated(state, command_id=COMMAND, evidence_id=EVIDENCE, *, gate_id=GATE):
    gate = OpenGate(gate_id, state.revision.fence.generation, RetryAuthorizationRequired(command_id, evidence_id),
                    RemediationAttempted(RemediationOperation.RECONCILE_REMOTE, (evidence_id,),
                                         RemediationOutcome.VERIFIED_ABSENT))
    return replace(state, gates=tuple(g for g in state.gates if getattr(g, "gate_id", None) != gate_id) + (gate,))


def facts(state, command_id=COMMAND, *, evidence_id=EVIDENCE, observed=NOW, association=StandaloneCommand(),
          reconciliation=None, prior=(), binding=None, evidence=True, **known):
    binding = binding or state.binding
    if isinstance(association, RearmObligation):
        known = {"obligation_active": True, "workspace_ready": True, **known}
    return RetryAuthorizationFacts(
        command_id, binding,
        reconciliation if reconciliation is not None else ReconciliationOnly(ActiveReconciliation(SINCE, NOW, 20), GATE),
        CommandAbsenceEvidence(evidence_id, command_id, binding, observed, "current") if evidence else None,
        association, prior, **known,
    )


def command(state, command_id=COMMAND, **changes):
    values = dict(authorization_id="grant-1", expected=state.revision, command_id=command_id,
                  gate_id=GATE, evidence_id=EVIDENCE)
    values.update(changes)
    return AuthorizeAbsentRetry(**values)


def grant(state, authorization_id="old-grant", evidence_id="old-evidence"):
    return UnconsumedRetryAuthorization(authorization_id, COMMAND, GATE, evidence_id, state.binding, StandaloneCommand())


def test_standalone_authorization_advances_version_only_and_validates_as_the_single_transition():
    state = gated(snapshot())
    known = facts(state)
    accepted = authorize_absent_retry(state, command(state), ACTOR, known)
    assert isinstance(accepted, WorkflowChanged) and accepted.mutation is WorkflowMutation.AUTHORIZE_RETRY
    after = accepted.snapshot
    assert after.revision.fence == state.revision.fence and after.revision.version == state.revision.version + 1
    assert (after.episode, after.gates, after.intent) == (state.episode, state.gates, state.intent)
    assert accepted.commands == ()
    validate_resolve_transition(state, accepted, ceiling=3, now=NOW, retry_facts=known)
    assert retry_authorization_available(state, state.gates[-1], known)
    minted = minted_authorization(command(state), known)
    assert minted.binding == state.binding and minted.association == StandaloneCommand()


def test_validator_requires_harness_facts_and_rejects_a_widened_proposal():
    state = gated(snapshot())
    accepted = authorize_absent_retry(state, command(state), ACTOR, facts(state))
    assert isinstance(accepted, WorkflowChanged)
    with pytest.raises(ValueError, match="harness-owned"):
        validate_resolve_transition(state, accepted, ceiling=3, now=NOW)
    widened = replace(accepted, snapshot=replace(accepted.snapshot, gates=()))
    with pytest.raises(ValueError, match="disagrees"):
        validate_resolve_transition(state, widened, ceiling=3, now=NOW, retry_facts=facts(state))
    with pytest.raises(ValueError):
        validate_resolve_transition(state, accepted, ceiling=3, now=NOW,
                                    retry_facts=facts(state, reconciliation=RetiredSettledCommand("r", "o")))


def test_stale_revision_is_rejected_before_any_other_check():
    state = gated(snapshot())
    stale = command(state, expected=replace(state.revision, version=state.revision.version - 1))
    assert authorize_absent_retry(state, stale, ACTOR, facts(state)).code is WorkflowRejection.STALE_REVISION


@pytest.mark.parametrize("changes, reason", [
    ({"gate_id": "another-gate"}, "does not require"),
    ({"evidence_id": "newer-evidence"}, "another command or a newer"),
])
def test_wrong_gate_or_evidence_is_invalid_scope(changes, reason):
    state = gated(snapshot())
    rejected = authorize_absent_retry(state, command(state, **changes), ACTOR, facts(state, evidence_id=changes.get("evidence_id", EVIDENCE)))
    assert isinstance(rejected, WorkflowRejected) and rejected.code is WorkflowRejection.INVALID_SCOPE
    assert reason in rejected.reason


def test_gate_naming_another_command_is_rejected():
    state = gated(snapshot(), command_id=CommandId("other-command"))
    rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state))
    assert rejected.code is WorkflowRejection.INVALID_SCOPE


def test_uncertain_gate_without_verified_absence_is_not_authorizable():
    state = snapshot()
    gate = OpenGate(GATE, 0, RemoteOutcomeUncertain(COMMAND, SINCE, 20, "lost"), NoAuthorizedRemediation("observe"))
    state = replace(state, gates=(gate,))
    assert authorize_absent_retry(state, command(state), ACTOR, facts(state)).code is WorkflowRejection.INVALID_SCOPE
    assert not retry_authorization_available(state, gate, facts(state))


@pytest.mark.parametrize("reconciliation", [
    RetiredUncertainCommand(ReconciliationOnly(ActiveReconciliation(SINCE, NOW, 20), GATE), "retired"),
    RetiredSettledCommand("retired", "outcome"),
])
def test_retired_commands_can_never_be_authorized(reconciliation):
    state = gated(snapshot())
    rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state, reconciliation=reconciliation))
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "retired" in rejected.reason


@pytest.mark.parametrize("reconciliation", [
    ActiveReconciliation(SINCE, NOW, 1),
    ReconciliationOnly(ActiveReconciliation(SINCE, NOW, 20), "another-gate"),
])
def test_command_must_be_reconciliation_only_under_the_named_gate(reconciliation):
    state = gated(snapshot())
    rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state, reconciliation=reconciliation))
    assert rejected.code is WorkflowRejection.INVALID_SCOPE


def test_missing_or_foreign_evidence_is_not_proof():
    state = gated(snapshot())
    assert authorize_absent_retry(state, command(state), ACTOR, facts(state, evidence=False)).code is WorkflowRejection.INVALID_SCOPE
    other = replace(state.binding, episode_id="other-episode")
    foreign = replace(facts(state), evidence=CommandAbsenceEvidence(EVIDENCE, COMMAND, other, NOW, "current"))
    assert authorize_absent_retry(state, command(state), ACTOR, foreign).code is WorkflowRejection.INVALID_SCOPE
    predates = facts(state, observed=SINCE - timedelta(seconds=1))
    assert "predates" in authorize_absent_retry(state, command(state), ACTOR, predates).reason


def test_command_from_another_generation_or_episode_is_rejected():
    state = gated(snapshot())
    stale = replace(state.binding, episode_id="old-episode")
    rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state, binding=stale))
    assert rejected.code is WorkflowRejection.INVALID_SCOPE


def test_outstanding_grant_conflicts_and_spent_evidence_cannot_buy_another_attempt():
    state = gated(snapshot())
    outstanding = facts(state, prior=(grant(state),))
    assert authorize_absent_retry(state, command(state), ACTOR, outstanding).code is WorkflowRejection.AUTHORIZATION_OUTSTANDING
    spent = facts(state, prior=(ConsumedRetryAuthorization(grant(state, evidence_id=EVIDENCE), 2),))
    rejected = authorize_absent_retry(state, command(state), ACTOR, spent)
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "already bought" in rejected.reason
    older = facts(state, prior=(ConsumedRetryAuthorization(grant(state), 2),))
    assert isinstance(authorize_absent_retry(state, command(state), ACTOR, older), WorkflowChanged)


def test_rearm_obligation_requires_its_original_request_still_pending():
    pending = start(exhausted()).snapshot
    assert isinstance(pending.intent, PendingRearm)
    obligation = pending.intent.required_commands[0]
    state = gated(pending, obligation)
    linked = facts(state, obligation, association=RearmObligation(pending.intent.request_id))
    assert isinstance(authorize_absent_retry(state, command(state, obligation), ACTOR, linked), WorkflowChanged)
    foreign = facts(state, obligation, association=RearmObligation("another-request"))
    assert authorize_absent_retry(state, command(state, obligation), ACTOR, foreign).code is WorkflowRejection.INVALID_SCOPE
    unlinked = facts(state, obligation)
    assert authorize_absent_retry(state, command(state, obligation), ACTOR, unlinked).code is WorkflowRejection.INVALID_SCOPE
    uncertainty = RemoteOutcomeUncertain(obligation, SINCE, 20, "lost")
    cancel = CancelRearm("cancel-1", pending.revision, pending.intent.request_id, "operator stopped it")
    stopped = cancel_resolve_rearm(pending, cancel, ACTOR, uncertainty=(uncertainty,)).snapshot
    stopped = gated(stopped, obligation)
    cancelled = facts(stopped, obligation, association=RearmObligation(pending.intent.request_id))
    rejected = authorize_absent_retry(stopped, command(stopped, obligation), ACTOR, cancelled)
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "no longer pending" in rejected.reason


@pytest.mark.parametrize("value", ["", "x" * 513, "bad\x00id", "tab\tid", 7, None])
def test_malformed_or_huge_identifiers_cannot_construct_a_command(value):
    state = gated(snapshot())
    for field in ("authorization_id", "command_id", "gate_id", "evidence_id"):
        with pytest.raises((ValueError, TypeError)):
            command(state, **{field: value})


@pytest.mark.parametrize("changes", [
    {"command_id": ""}, {"association": None}, {"reconciliation": "retired"},
    {"prior": tuple(UnconsumedRetryAuthorization(f"g{i}", COMMAND, GATE, f"e{i}", snapshot().binding, StandaloneCommand()) for i in range(65))},
    {"prior": (UnconsumedRetryAuthorization("g", CommandId("other"), GATE, "e", snapshot().binding, StandaloneCommand()),)},
    {"evidence": CommandAbsenceEvidence(EVIDENCE, CommandId("other"), snapshot().binding, NOW, "current")},
    {"command_binding": None},
])
def test_malformed_facts_are_refused_at_construction(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(facts(gated(snapshot())), **changes)


def test_facts_for_another_command_are_a_programming_error():
    state = gated(snapshot())
    with pytest.raises(ValueError, match="another command"):
        authorize_absent_retry(state, command(state), ACTOR, facts(state, CommandId("other")))


def test_untyped_actor_or_command_is_refused():
    state = gated(snapshot())
    with pytest.raises(ValueError):
        authorize_absent_retry(state, command(state), "operator", facts(state))
    with pytest.raises(ValueError):
        AuthorizedOperatorCommand(ACTOR, "authorize_retry")


# --- one scope contract: acceptance, view and spend (review 2026-09-25) ---

def other_gate(state, gate_id="other-gate"):
    return replace(state, gates=state.gates + (OpenGate(
        gate_id, state.revision.fence.generation, RemoteOutcomeUncertain(CommandId("sibling"), SINCE, 1, "lost"),
        NoAuthorizedRemediation("read-only"),
    ),))


def read(at=NOW, revision="revision-1", guard=True):
    return RetryAbsenceRead(at, revision, guard)


def spend(state, known, *, at=NOW, **changes):
    return retry_spend_decision(state, grant(state, "grant-1", EVIDENCE), known, read(at, **changes), now=NOW)


def test_returned_completion_held_by_another_gate_is_never_offered_accepted_or_spent():
    state = other_gate(gated(snapshot()))
    held = facts(state, returns_resolve_result=True)
    rejected = authorize_absent_retry(state, command(state), ACTOR, held)
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "another gate" in rejected.reason
    assert not retry_authorization_available(state, state.gates[-2], held)
    decision = spend(state, held)
    assert decision == DeclineRetry(RetryDisposition.VOID, rejected.reason, True)
    # The same hold does not block an ordinary standalone command.
    assert isinstance(authorize_absent_retry(state, command(state), ACTOR, facts(state)), WorkflowChanged)
    alone = gated(snapshot())
    assert isinstance(authorize_absent_retry(alone, command(alone), ACTOR, facts(alone, returns_resolve_result=True)), WorkflowChanged)


def test_rearm_obligation_needs_its_active_link_and_verified_workspace():
    pending = start(exhausted()).snapshot
    obligation = pending.intent.required_commands[0]
    state = gated(pending, obligation)
    association = RearmObligation(pending.intent.request_id)
    for missing, reason in (("obligation_active", "no longer pending"), ("workspace_ready", "workspace handoff")):
        known = facts(state, obligation, association=association, **{missing: False})
        rejected = authorize_absent_retry(state, command(state, obligation), ACTOR, known)
        assert rejected.code is WorkflowRejection.INVALID_SCOPE and reason in rejected.reason
        decision = retry_spend_decision(
            state, UnconsumedRetryAuthorization("grant-1", obligation, GATE, EVIDENCE, state.binding, association),
            known, read(), now=NOW,
        )
        assert isinstance(decision, DeclineRetry) and decision.disposition is RetryDisposition.VOID and reason in decision.reason


def test_absence_must_postdate_the_last_authorized_attempt():
    state = gated(snapshot())
    for last in (NOW, NOW + timedelta(seconds=1)):
        rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state, last_attempt_at=last))
        assert rejected.code is WorkflowRejection.INVALID_SCOPE and "last authorized attempt" in rejected.reason
    later = facts(state, last_attempt_at=NOW - timedelta(microseconds=1))
    assert isinstance(authorize_absent_retry(state, command(state), ACTOR, later), WorkflowChanged)


def test_an_inapplicable_judgement_is_terminal_for_the_command_even_with_new_evidence():
    state = gated(snapshot())
    judged = ClosedRetryAuthorization(grant(state, "old", "old-evidence"), RetryDisposition.INAPPLICABLE, "guard changed")
    rejected = authorize_absent_retry(state, command(state), ACTOR, facts(state, prior=(judged,)))
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "inapplicable" in rejected.reason
    for disposition in (RetryDisposition.VOID, RetryDisposition.NOT_NEEDED):
        closed = ClosedRetryAuthorization(grant(state, "old", "old-evidence"), disposition, "ended")
        assert isinstance(authorize_absent_retry(state, command(state), ACTOR, facts(state, prior=(closed,))), WorkflowChanged)
        reused = ClosedRetryAuthorization(grant(state, "old", EVIDENCE), disposition, "ended")
        reason = authorize_absent_retry(state, command(state), ACTOR, facts(state, prior=(reused,))).reason
        # An unspent grant never claims to have bought an attempt.
        assert "ended unspent" in reason and "bought" not in reason


def test_spend_begins_only_for_a_fresh_applicable_read_of_the_granted_scope():
    state = gated(snapshot())
    assert spend(state, facts(state)) == BeginRetry()
    assert spend(state, facts(state, decision_revision="revision-1")) == BeginRetry()


@pytest.mark.parametrize("at, reason", [
    (NOW - timedelta(seconds=301), "not fresh"),
    (NOW + timedelta(seconds=1), "not fresh"),
    (NOW - timedelta(seconds=1), "not fresh"),  # predates the grant's own evidence
])
def test_a_stale_or_future_read_voids_without_backing_new_evidence(at, reason):
    state = gated(snapshot())
    decision = spend(state, facts(state), at=at)
    assert decision == DeclineRetry(RetryDisposition.VOID, "the executor's absence read is not fresh", False)


def test_a_read_before_the_last_attempt_cannot_spend():
    state = gated(snapshot())
    decision = spend(state, facts(state, observed=NOW - timedelta(seconds=10), last_attempt_at=NOW))
    assert isinstance(decision, DeclineRetry) and not decision.fresh_read


def test_scope_changes_after_the_grant_void_it_with_fresh_evidence():
    state = gated(snapshot())
    moved = replace(state, revision=replace(state.revision, fence=replace(state.revision.fence, generation=state.revision.fence.generation + 1)))
    assert "generation" in spend(moved, facts(moved, binding=state.binding)).reason
    superseded = gated(snapshot(), evidence_id="absence-2")
    decision = spend(superseded, facts(superseded))
    assert decision.disposition is RetryDisposition.VOID and "superseded" in decision.reason and decision.fresh_read
    retired = spend(state, facts(state, reconciliation=RetiredUncertainCommand(
        ReconciliationOnly(ActiveReconciliation(SINCE, NOW, 20), GATE), "retired-1")))
    assert retired.disposition is RetryDisposition.VOID and "retired" in retired.reason


def test_cancelled_rearm_after_the_grant_voids_the_obligation_grant():
    pending = start(exhausted()).snapshot
    obligation = pending.intent.required_commands[0]
    association = RearmObligation(pending.intent.request_id)
    uncertainty = RemoteOutcomeUncertain(obligation, SINCE, 20, "lost")
    cancel = CancelRearm("cancel-1", pending.revision, pending.intent.request_id, "operator stopped it")
    stopped = gated(cancel_resolve_rearm(pending, cancel, ACTOR, uncertainty=(uncertainty,)).snapshot, obligation)
    decision = retry_spend_decision(
        stopped, UnconsumedRetryAuthorization("grant-1", obligation, GATE, EVIDENCE, stopped.binding, association),
        facts(stopped, obligation, association=association), read(), now=NOW,
    )
    assert decision.disposition is RetryDisposition.VOID and "no longer pending" in decision.reason


@pytest.mark.parametrize("guard, revision", [(False, "revision-1"), (True, "revision-2")])
def test_changed_original_guard_is_inapplicable_not_void(guard, revision):
    state = gated(snapshot())
    decision = spend(state, facts(state, decision_revision="revision-1"), guard=guard, revision=revision)
    assert decision.disposition is RetryDisposition.INAPPLICABLE and decision.fresh_read


@pytest.mark.parametrize("changes", [
    {"observed_at": NOW.replace(tzinfo=None)}, {"target_revision": ""}, {"target_revision": "x" * 513},
    {"guard_holds": 1}, {"observed_at": None},
])
def test_malformed_reads_are_refused_at_construction(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(read(), **changes)


def test_malformed_terminal_grants_and_facts_are_refused():
    state = gated(snapshot())
    with pytest.raises(ValueError):
        ClosedRetryAuthorization(grant(state), "void", "ended")
    with pytest.raises(ValueError):
        ClosedRetryAuthorization(grant(state), RetryDisposition.VOID, "")
    with pytest.raises(ValueError):
        DeclineRetry(RetryDisposition.NOT_NEEDED, "x", True)
    for changes in ({"last_attempt_at": NOW.replace(tzinfo=None)}, {"workspace_ready": 1}, {"decision_revision": ""}):
        with pytest.raises((ValueError, TypeError)):
            replace(facts(state), **changes)


def test_attempt_ceiling_and_execution_refusal_share_the_one_validator():
    state = gated(snapshot())
    ceiling = facts(state, authorized_attempts=MAX_AUTHORIZED_ATTEMPTS)
    rejected = authorize_absent_retry(state, command(state), ACTOR, ceiling)
    assert rejected.code is WorkflowRejection.INVALID_SCOPE and "authorized attempts" in rejected.reason
    assert not retry_authorization_available(state, state.gates[-1], ceiling)
    below = facts(state, authorized_attempts=MAX_AUTHORIZED_ATTEMPTS - 1)
    assert isinstance(authorize_absent_retry(state, command(state), ACTOR, below), WorkflowChanged)
    refused = facts(state, execution_refusal="the fix owner is retired")
    rejected = authorize_absent_retry(state, command(state), ACTOR, refused)
    assert "fix owner is retired" in rejected.reason
    assert not retry_authorization_available(state, state.gates[-1], refused)
    decision = spend(state, refused)
    assert decision.disposition is RetryDisposition.VOID and "fix owner is retired" in decision.reason


@pytest.mark.parametrize("reads, age, voided", [
    (MAX_GRANT_UNAVAILABLE_READS - 1, 0, False), (MAX_GRANT_UNAVAILABLE_READS, 0, True),
    (0, MAX_GRANT_AGE_SECONDS - 1, False), (0, MAX_GRANT_AGE_SECONDS, True),
])
def test_open_grant_survives_bounded_unavailable_reads(reads, age, voided):
    assert (grant_unavailable_void(reads, NOW - timedelta(seconds=age), NOW) is not None) is voided


@pytest.mark.parametrize("args", [(-1, NOW, NOW), (1.0, NOW, NOW), (0, NOW.replace(tzinfo=None), NOW)])
def test_grant_ceiling_rejects_malformed_inputs(args):
    with pytest.raises(ValueError):
        grant_unavailable_void(*args)
