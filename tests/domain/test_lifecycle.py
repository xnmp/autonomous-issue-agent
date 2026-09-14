"""Lifecycle value objects over the GitHub label control plane."""

from tests.domain.builders import issue, pr

from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import (
    IssueLifecycle,
    IssuePhase,
    PrLifecycle,
    PrPhase,
    issue_lifecycle,
    pr_escalation,
    pr_lifecycle,
    retry_breaker,
)
from issue_agent.domain.actions import PostComment, Relabel, Target


def test_issue_lifecycle_identifies_ready_open_issue():
    lifecycle = issue_lifecycle(issue(1, "ai-ready"))

    assert lifecycle.phase is IssuePhase.READY
    assert lifecycle.is_open is True
    assert lifecycle.is_ready is True
    assert lifecycle.is_blocked is False


def test_issue_lifecycle_closed_ready_issue_is_not_dispatch_ready():
    lifecycle = issue_lifecycle(issue(1, "ai-ready", state="closed"))

    assert lifecycle.phase is IssuePhase.READY
    assert lifecycle.is_open is False
    assert lifecycle.is_ready is False


def test_issue_lifecycle_phase_precedence_prefers_furthest_state():
    lifecycle = IssueLifecycle(
        labels=frozenset({Label.AI_READY, Label.AI_WORKING, Label.AI_PR_OPEN, Label.AI_DONE})
    )

    assert lifecycle.phase is IssuePhase.DONE
    assert lifecycle.is_ready is False
    assert lifecycle.is_working is True
    assert lifecycle.is_pr_open is True


def test_issue_lifecycle_tracks_blocked_and_untracked():
    assert issue_lifecycle(issue(2, "ai-blocked")).is_blocked is True
    assert issue_lifecycle(issue(3)).phase is IssuePhase.UNTRACKED


def test_pr_lifecycle_identifies_open_reviewable_pr():
    lifecycle = pr_lifecycle(pr(10, "ai-pr-open"))

    assert lifecycle.phase is PrPhase.REVIEWABLE
    assert lifecycle.is_open is True
    assert lifecycle.is_reviewable is True
    assert lifecycle.is_blocked is False


def test_pr_lifecycle_cleanup_is_state_and_label_based():
    merged = pr_lifecycle(pr(10, "ai-pr-open", state="MERGED"))
    closed = pr_lifecycle(pr(11, "ai-pr-open", state="CLOSED"))
    unlabeled_merged = pr_lifecycle(pr(12, state="MERGED"))

    assert merged.phase is PrPhase.DONE
    assert closed.phase is PrPhase.CLOSED
    assert merged.needs_cleanup is True
    assert closed.needs_cleanup is True
    assert unlabeled_merged.needs_cleanup is False


def test_pr_lifecycle_blocked_includes_human_escalation():
    blocked = PrLifecycle(labels=frozenset({Label.AI_BLOCKED}))
    escalated = PrLifecycle(labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))

    assert blocked.phase is PrPhase.BLOCKED
    assert escalated.phase is PrPhase.BLOCKED
    assert blocked.is_blocked is True
    assert escalated.is_blocked is True


def test_pr_lifecycle_working_and_no_review_are_explicit():
    assert pr_lifecycle(pr(20, "ai-working")).phase is PrPhase.WORKING
    assert pr_lifecycle(pr(21, "no-ai-review")).phase is PrPhase.NO_REVIEW
    assert pr_lifecycle(pr(22)).phase is PrPhase.UNTRACKED


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
