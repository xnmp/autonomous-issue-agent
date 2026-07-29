"""Domain contracts for the hard-end escalation route (#62)."""

from issue_agent.domain.effort import EscalationSignals


def test_recurrence_or_two_completed_failures_requires_the_ceiling():
    assert EscalationSignals(recurrence=True).requires_ceiling() is True
    assert EscalationSignals(resolve_retries=1, ci_retries=1).requires_ceiling() is True
    assert EscalationSignals(resolve_retries=1).requires_ceiling() is False
