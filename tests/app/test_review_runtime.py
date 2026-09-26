"""Trusted external holds for review re-arms (ADR 0012 provenance, ADR 0015)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.review_runtime import review_external_holds, unproductive_rounds_reader
from issue_agent.app.roles import _review_unproductive_rounds
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState
from issue_agent.domain.world import PRView
from tests.fakes.review_workflow import HEAD, PR_KEY

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
PR = PR_KEY.target.number


def observed(*labels: Label) -> ObservedPullRequest:
    return ObservedPullRequest(PR_KEY, HEAD, PullRequestState.OPEN, frozenset(labels), CompleteReviews(()), NOW)


def view(**fields) -> PRView:
    return PRView(PR, head_ref_oid=HEAD, **fields)


def test_harness_hold_labels_and_a_tripped_breaker_are_holds_but_lifecycle_labels_are_not():
    holds = review_external_holds(lambda _pr: 0, 5)
    assert holds(observed(Label.NEEDS_HUMAN_REVIEW, Label.AI_PR_OPEN), view()) == ()
    assert holds(observed(Label.AI_PARKED, Label.AI_BLOCKED), view()) == ("ai-blocked", "ai-parked")
    assert holds(observed(Label.NEEDS_BUDGET_REVIEW), view()) == ("needs-budget-review",)
    tripped = review_external_holds(lambda _pr: 5, 5)
    assert tripped(observed(), view()) == ("unproductive review round ceiling",)
    assert review_external_holds(lambda _pr: 10**12, 5)(observed(), view()) == ("unproductive review round ceiling",)


def test_an_unreadable_counter_or_a_missing_view_is_never_permission_to_clear():
    def unreadable(_pr):
        raise OSError("state dir unreadable")
    with pytest.raises(OSError):
        review_external_holds(unreadable, 5)(observed(), view())
    with pytest.raises(ValueError):
        review_external_holds(lambda _pr: 0, 5)(observed(), None)
    with pytest.raises(ValueError):
        review_external_holds(lambda _pr: 0, 5)(observed(), PRView(PR + 1))


@pytest.mark.parametrize("key", ["issue", "pr", "recovered"])
def test_the_rearm_hold_and_the_dispatch_decision_count_the_same_unproductive_rounds(tmp_path, key):
    """A breaker tripped under the legacy issue key must hold a re-arm exactly as it holds dispatch."""
    store = Store(str(tmp_path))
    pr = view(closing_issue=5, head_ref_name="fix/5-thing")
    deaths = {-PR: 3} if key == "recovered" else {}
    for _ in range(3 if key != "recovered" else 0):
        store.bump_infra_deaths(5 if key == "issue" else -PR)
    decided = _review_unproductive_rounds(store, SimpleNamespace(prs=[pr]), deaths, {})[PR]
    read = unproductive_rounds_reader(store, lambda: deaths)(pr)
    assert decided == read == 3
    holds = review_external_holds(unproductive_rounds_reader(store, lambda: deaths), 3)
    assert holds(observed(), pr) == ("unproductive review round ceiling",)
