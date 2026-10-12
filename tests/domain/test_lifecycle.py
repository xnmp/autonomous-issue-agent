"""Lifecycle value objects over the GitHub label control plane."""

from tests.domain.builders import issue, pr

from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import (
    pr_escalation,
    retry_breaker,
)
from issue_agent.domain.actions import PostComment, Relabel, Target


def test_retry_breaker_trips_at_cap_and_unbounded_cap_never_trips():
    assert retry_breaker(2, 3).tripped is False
    assert retry_breaker(3, 3).tripped is True
    assert retry_breaker(4, 3).tripped is True
    assert retry_breaker(99, 0).tripped is False
    assert retry_breaker(99, -1).tripped is False


def test_pr_escalation_posts_reason_and_moves_pr_to_human_review():
    actions = pr_escalation(100, "CI fix failed 3× without going green.").actions()

    assert actions == [
        PostComment(
            Target.PR,
            100,
            "**Circuit breaker:** CI fix failed 3× without going green. Escalating to a human.",
        ),
        Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
    ]
