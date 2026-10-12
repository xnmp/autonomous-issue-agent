"""Trusted external holds for review re-arms (ADR 0012 provenance, ADR 0015)."""

from tests.domain.builders import observed_pr

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.review_runtime import current_review_failures, review_external_holds
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
    return observed_pr(PR, head_ref_oid=HEAD, **fields)


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
        review_external_holds(lambda _pr: 0, 5)(observed(), observed_pr(PR + 1))


from tests.adapters.test_review_execution_control import rig


def test_rearm_and_dispatch_read_the_same_current_allowance(rig, tmp_path):
    from tests.adapters.test_review_execution_renewal_transactions import exhaust_with_three_settled_returns

    exhaust_with_three_settled_returns(rig)
    control, _, _, _, _ = rig
    store = Store(str(tmp_path / "retired-files"))
    Path(store.issue_path(-9, "infra-deaths")).parent.mkdir(parents=True, exist_ok=True)
    Path(store.issue_path(-9, "infra-deaths")).write_text("20")
    pr = observed_pr(9, head_ref_oid=HEAD)
    errors = {}
    counts = _review_unproductive_rounds(control, "o/r", SimpleNamespace(prs=[pr]), errors)
    assert counts == {9: 3} and errors == {}
    holds = review_external_holds(lambda pr: current_review_failures(control, "o/r", pr.number), 3)
    assert holds(observed(), view()) == ("unproductive review round ceiling",)
