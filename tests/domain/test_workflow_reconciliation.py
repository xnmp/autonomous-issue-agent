from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.workflow import (
    ActiveReconciliation, ReconciliationOnly, ReconciliationPolicy,
    RetiredSettledCommand, RetiredUncertainCommand,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)
from issue_agent.domain.workflow_reconciliation import reconciliation_due, unavailable_reconciliation

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
