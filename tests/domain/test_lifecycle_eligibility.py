"""Reachable partial label transitions cannot override terminal state or holds."""


import pytest

from issue_agent.domain.eligibility import IssueHoldGuard
from issue_agent.domain.lifecycle import hold_guard
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView


# -- ADR 0015 semantic hold fence ---------------------------------------------

NHR, READY = Label.NEEDS_HUMAN_REVIEW, Label.AI_READY
OWN = {"removed": (NHR,), "added": (READY,)}


def _held(**changes) -> IssueView:
    fields = {"labels": frozenset({NHR}), "updated_at": "r1", "title": "t", "body": "b"}
    return IssueView(1, **{**fields, **changes})


def test_closed_issues_cannot_mint_a_hold_guard():
    with pytest.raises(ValueError):
        hold_guard(_held(state="closed"))


@pytest.mark.parametrize("state_reason, labels", [
    ("x" * 257, frozenset()), ("bad\x00reason", frozenset()), (None, frozenset()),
    ("", {NHR}), ("", frozenset({"needs-human-review"})), ("", frozenset({Label.P2})),
])
def test_malformed_hold_guards_are_rejected(state_reason, labels):
    with pytest.raises(ValueError):
        IssueHoldGuard(state_reason, labels)


