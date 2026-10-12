"""domain.effort — the escalation ladder (#16).

Behaviour under test: how much evidence exists, what a notch buys, and that the
ladder can only ever hand back a stance at least as strong as the one it was
given. Nothing here touches a runtime, a store, or GitHub — that wiring is
covered in tests/adapters/test_fix_workflow.py.
"""

import pytest

from issue_agent.domain.effort import (
    EFFORT_LADDER,
    TIER_LADDER,
    Effort,
    parse_effort,
)


# ── totality on junk ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["HIGH", " high ", "High"])
def test_parse_effort_normalises_case_and_whitespace(raw):
    assert parse_effort(raw) is Effort.HIGH


@pytest.mark.parametrize("raw", ["", "hihg", None, 7, [], "medium-high"])
def test_parse_effort_returns_the_default_on_anything_unrecognised(raw):
    assert parse_effort(raw) is None
    assert parse_effort(raw, Effort.LOW) is Effort.LOW


def test_parse_effort_passes_an_effort_through():
    assert parse_effort(Effort.MEDIUM) is Effort.MEDIUM


# ── the sensor behind `review_rounds` (#16 P1) ────────────────────────────────
# The store's per-review attempt counter re-keys to zero on every new review, so
# it could never answer "rounds on this PR are climbing". These assert the
# GitHub-derived counter that replaced it, from the FIXER's point of view.
def test_outstanding_change_requests_counts_everyone_but_the_fixer():
    from issue_agent.domain.review import count_outstanding_change_requests
    from issue_agent.domain.world import Review

    reviews = (
        Review("CHANGES_REQUESTED", "rev-bot", "t1"),
        Review("CHANGES_REQUESTED", "a-human", "t2"),
        Review("CHANGES_REQUESTED", "fix-bot", "t3"),  # our own — not evidence
    )
    assert count_outstanding_change_requests(reviews, "fix-bot") == 2


def test_an_approve_clears_the_outstanding_count():
    from issue_agent.domain.review import count_outstanding_change_requests
    from issue_agent.domain.world import Review

    reviews = (
        Review("CHANGES_REQUESTED", "rev-bot", "t1"),
        Review("CHANGES_REQUESTED", "rev-bot", "t2"),
        Review("APPROVED", "rev-bot", "t3"),
        Review("CHANGES_REQUESTED", "rev-bot", "t4"),
    )
    assert count_outstanding_change_requests(reviews, "fix-bot") == 1


def test_no_reviews_is_no_evidence():
    from issue_agent.domain.review import count_outstanding_change_requests

    assert count_outstanding_change_requests((), "fix-bot") == 0
