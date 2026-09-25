from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.workflow import (
    ActiveReconciliation, CommandAbsenceEvidence, ReconciliationOnly, ReconciliationPolicy,
    RemediationOutcome, RetiredSettledCommand, RetiredUncertainCommand, RetryAuthorizationRequired,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)
from issue_agent.domain.workflow_reconciliation import (
    absent_reconciliation, absent_reexecution, reconciliation_due, reexecution_exhausted,
    unavailable_reconciliation, unknown_outcome_history,
)

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
BINDING = WorkflowBinding(WorkflowFence(WorkflowKey("o/r", IssueRef(7)), 1), FamilyAuthority(WorkflowFamily.RESOLVE, 1), "episode")


def failure(prior=None, *, now=NOW, policy=ReconciliationPolicy()):
    return unavailable_reconciliation(BINDING, CommandId("command"), prior, now=now, evidence_id="observed-failure", error="remote unavailable", policy=policy)


def test_outage_backoff_is_bounded_and_retains_original_uncertainty():
    progress = failure()
    assert isinstance(progress.state, ActiveReconciliation)
    assert progress.state.next_read_at == NOW + timedelta(seconds=30)
    assert not reconciliation_due(progress.state, NOW)
    for index in range(1, 30):
        history = progress.state.history if isinstance(progress.state, ReconciliationOnly) else progress.state
        now = history.next_read_at
        progress = failure(progress.state, now=now)
        current = progress.state.history if isinstance(progress.state, ReconciliationOnly) else progress.state
        assert current.unresolved_since == NOW
        assert (current.next_read_at - now).total_seconds() <= 1800
    assert isinstance(progress.state, ReconciliationOnly)
    assert progress.gate is not None
    assert progress.gate.cause.command_id == "command"
    assert progress.gate.cause.unresolved_since == NOW


@pytest.mark.parametrize("cause", ["age", "reads"])
def test_expiry_opens_uncertainty_gate_not_absence_or_retry_permission(cause):
    policy = ReconciliationPolicy(maximum_age_seconds=60, maximum_reads=2)
    progress = failure(policy=policy)
    now = NOW + timedelta(seconds=60 if cause == "age" else 30)
    if cause == "age":
        policy = ReconciliationPolicy(maximum_age_seconds=60, maximum_reads=20)
    expired = failure(progress.state, now=now, policy=policy)
    assert isinstance(expired.state, ReconciliationOnly)
    assert expired.gate.cause.last_error == "remote unavailable"


def test_policy_relaxation_cannot_restore_expired_execution_permission():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    later = failure(expired.state, now=NOW + timedelta(seconds=30), policy=ReconciliationPolicy(maximum_reads=10000))
    assert isinstance(later.state, ReconciliationOnly)
    assert later.state.gate_id == expired.state.gate_id


def test_retirement_survives_repeated_unavailable_reads():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    retired = RetiredUncertainCommand(expired.state, "retirement")
    later = failure(retired, now=NOW + timedelta(seconds=30))
    assert isinstance(later.state, RetiredUncertainCommand)
    assert later.state.retirement_evidence_id == "retirement"
    assert later.state.uncertainty.history.unresolved_since == NOW


def test_retired_settled_obligation_never_regains_execution_or_periodic_polling():
    retired = RetiredSettledCommand("retirement", "observed-outcome")
    assert not reconciliation_due(retired, NOW)
    assert failure(retired).state == retired


def test_reversed_clock_does_not_rewrite_original_uncertainty_time():
    with pytest.raises(ValueError):
        failure(failure().state, now=NOW - timedelta(seconds=1))


def absent(prior, *, evidence=None, now=NOW):
    return absent_reconciliation(
        BINDING, CommandId("command"), prior,
        evidence or CommandAbsenceEvidence("absence", CommandId("command"), BINDING, NOW, "remote-revision"),
        now=now,
    )


def test_verified_absence_replaces_unknown_cause_but_does_not_authorize_execution():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    verified = absent(expired.state)
    assert isinstance(verified.state, ReconciliationOnly)
    assert verified.state.history.unresolved_since == expired.state.history.unresolved_since
    assert verified.state.history.failed_reads == expired.state.history.failed_reads
    assert verified.gate.gate_id == expired.gate.gate_id
    assert verified.gate.cause == RetryAuthorizationRequired(CommandId("command"), "absence")
    assert verified.gate.remediation.outcome is RemediationOutcome.VERIFIED_ABSENT
    assert verified.gate.remediation.evidence_ids == ("absence",)
    assert absent(verified.state) == verified


def test_successful_absence_read_advances_polling_without_restoring_execution():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    due_at = expired.state.history.next_read_at
    assert reconciliation_due(expired.state, due_at)
    evidence = CommandAbsenceEvidence("absence", CommandId("command"), BINDING, due_at, "revision")
    verified = absent(expired.state, evidence=evidence, now=due_at)
    assert not reconciliation_due(verified.state, due_at)
    assert not reconciliation_due(verified.state, due_at + timedelta(seconds=1799))
    assert reconciliation_due(verified.state, due_at + timedelta(seconds=1800))
    assert isinstance(verified.state, ReconciliationOnly)
    assert verified.state.history.unresolved_since == NOW
    assert verified.state.history.failed_reads == expired.state.history.failed_reads
    later = failure(verified.state, now=due_at + timedelta(seconds=1800))
    assert isinstance(later.state, ReconciliationOnly)
    assert later.state.gate_id == verified.state.gate_id


def test_retired_absence_settles_current_uncertainty_without_reviving_execution():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    retired = RetiredUncertainCommand(expired.state, "original-retirement")
    verified = absent(retired)
    assert verified.state == RetiredSettledCommand("original-retirement", "absence")
    assert verified.gate is None
    assert not reconciliation_due(verified.state, NOW + timedelta(days=1))
    assert failure(verified.state).state == verified.state


@pytest.mark.parametrize("change", [
    {"command_id": CommandId("another-command")},
    {"binding": replace(BINDING, episode_id="another-episode")},
    {"binding": replace(BINDING, fence=replace(BINDING.fence, generation=2))},
    {"observed_at": NOW + timedelta(seconds=1)},
    {"observed_at": NOW - timedelta(seconds=301)},
])
def test_absence_proof_cannot_cross_command_episode_generation_or_freshness_boundary(change):
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    evidence = CommandAbsenceEvidence("absence", CommandId("command"), BINDING, NOW, "remote-revision")
    with pytest.raises(ValueError):
        absent(expired.state, evidence=replace(evidence, **change))


def test_recent_absence_before_uncertainty_started_cannot_resolve_it():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    evidence = CommandAbsenceEvidence("absence", CommandId("command"), BINDING, NOW - timedelta(seconds=1), "revision")
    with pytest.raises(ValueError, match="predates"):
        absent(expired.state, evidence=evidence)


@pytest.mark.parametrize("prior", [None, ActiveReconciliation(NOW, NOW, 0), RetiredSettledCommand("retirement", "proof")])
def test_absence_reconciliation_does_not_invent_expiry_or_revive_settled_commands(prior):
    with pytest.raises(ValueError):
        absent(prior)


def test_absence_reconciliation_rejects_naive_clock():
    expired = failure(policy=ReconciliationPolicy(maximum_reads=1))
    with pytest.raises(ValueError, match="timezone"):
        absent(expired.state, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("age, exhausted", [
    (timedelta(0), False), (timedelta(seconds=3599), False),
    (timedelta(seconds=3600), True), (timedelta(days=365), True),
])
def test_absent_re_execution_is_bounded_by_the_unresolved_age(age, exhausted):
    history = ActiveReconciliation(NOW, NOW + timedelta(seconds=30), 0)
    assert reexecution_exhausted(history, NOW + age) is exhausted


def test_re_execution_bound_follows_a_tighter_policy():
    history = ActiveReconciliation(NOW, NOW, 0)
    policy = ReconciliationPolicy(maximum_age_seconds=60)
    assert reexecution_exhausted(history, NOW + timedelta(seconds=60), policy)


@pytest.mark.parametrize("history", [None, "active", ReconciliationOnly(ActiveReconciliation(NOW, NOW, 1), "gate")])
def test_re_execution_bound_rejects_anything_but_pre_expiry_history(history):
    with pytest.raises(ValueError):
        reexecution_exhausted(history, NOW)


def test_repeated_unknowns_back_off_exponentially_per_re_execution_and_keep_their_age():
    history = unknown_outcome_history(None, NOW)
    assert history == ActiveReconciliation(NOW, NOW + timedelta(seconds=30), 0)
    delays = []
    for _ in range(12):
        history = absent_reexecution(history)
        now = history.next_read_at
        history = unknown_outcome_history(history, now)
        delays.append((history.next_read_at - now).total_seconds())
        assert history.unresolved_since == NOW
    assert delays[:5] == [60, 120, 240, 480, 960]
    assert max(delays) == ReconciliationPolicy().maximum_delay_seconds
    assert history.reexecutions == 12


def test_an_unknown_never_pulls_a_later_scheduled_read_earlier():
    later = ActiveReconciliation(NOW, NOW + timedelta(hours=1), 3, 1)
    assert unknown_outcome_history(later, NOW).next_read_at == later.next_read_at


@pytest.mark.parametrize("prior, now", [
    ("history", NOW), (ReconciliationOnly(ActiveReconciliation(NOW, NOW, 1), "gate"), NOW),
    (None, datetime(2026, 9, 14)),
])
def test_unknown_scheduling_rejects_malformed_inputs(prior, now):
    with pytest.raises(ValueError):
        unknown_outcome_history(prior, now)


@pytest.mark.parametrize("reexecutions, exhausted", [(0, False), (3, False), (4, True), (10**6, True)])
def test_re_executions_are_capped_by_count_before_the_age_ceiling(reexecutions, exhausted):
    history = ActiveReconciliation(NOW, NOW, 0, reexecutions)
    assert reexecution_exhausted(history, NOW + timedelta(seconds=1)) is exhausted


def test_failed_reads_and_the_ceiling_read_keep_the_re_execution_count():
    history = ActiveReconciliation(NOW, NOW, 0, 2)
    progress = failure(history, now=NOW + timedelta(seconds=5))
    assert progress.state.reexecutions == 2 and progress.gate is None
    capped = unavailable_reconciliation(BINDING, CommandId("command"), history, now=NOW + timedelta(seconds=5),
                                        evidence_id="observed-absence", error="ceiling", reexecution_ceiling=True)
    assert isinstance(capped.state, ReconciliationOnly) and capped.state.history.reexecutions == 2
    assert capped.gate is not None


@pytest.mark.parametrize("value", [-1, True, "2", 2.0])
def test_malformed_re_execution_counts_are_rejected(value):
    with pytest.raises(ValueError):
        ActiveReconciliation(NOW, NOW, 0, value)
