from dataclasses import replace

import pytest

from issue_agent.domain.labels import Label
from issue_agent.domain.review_linkage import (
    LinkedIssueUnavailable, NoLinkedIssue, ObservedLinkedIssue, linked_issue_holds,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow_identity import MAX_DETAIL_CHARS


def test_explicit_absence_and_observed_clear_issue_allow_review():
    assert not linked_issue_holds(NoLinkedIssue())
    assert not linked_issue_holds(ObservedLinkedIssue(IssueView(7)))


def test_unavailable_linkage_is_never_absence():
    with pytest.raises(ValueError, match="unavailable is not absence"):
        linked_issue_holds(LinkedIssueUnavailable("issue read failed"))  # type: ignore[arg-type]


@pytest.mark.parametrize("label", [Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_HUMAN_REVIEW,
                                   Label.NEEDS_BUDGET_REVIEW])
def test_every_remote_hold_is_named_with_its_issue(label):
    holds = linked_issue_holds(ObservedLinkedIssue(IssueView(7, labels=frozenset({label}))))
    assert any("#7" in hold and label.value in hold for hold in holds)


def test_closed_dependencies_and_sql_holds_are_all_preserved():
    holds = linked_issue_holds(ObservedLinkedIssue(
        IssueView(7, state="CLOSED", blocked_by_open=True), ("unresolved workflow gates",),
    ))
    for reason in ("closed", "open dependency", "unresolved workflow gates"):
        assert any("#7" in hold and reason in hold for hold in holds)


@pytest.mark.parametrize("field,value", [
    ("number", True), ("number", 0), ("number", 2**63), ("state", "unknown"), ("state", None),
    ("labels", {Label.AI_BLOCKED}), ("labels", frozenset({"ai-blocked"})), ("blocked_by_open", "false"),
])
def test_malformed_issue_facts_cannot_become_trusted_observations(field, value):
    with pytest.raises(ValueError):
        ObservedLinkedIssue(replace(IssueView(7), **{field: value}))


@pytest.mark.parametrize("holds", [[], ("",), ("x" * (MAX_DETAIL_CHARS + 1),), ("hold",) * 33])
def test_workflow_hold_evidence_must_be_bounded_and_immutable(holds):
    with pytest.raises(ValueError):
        ObservedLinkedIssue(IssueView(7), holds)
