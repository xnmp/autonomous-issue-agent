"""Trusted external holds for review re-arms (ADR 0012 provenance, ADR 0015)."""

from tests.domain.builders import observed_pr

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.review_runtime import current_review_failures, review_external_holds
from issue_agent.app.review_activation import unproductive_rounds_reader
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


@pytest.mark.parametrize("key", ["issue", "pr", "recovered"])
def test_stopped_import_reader_preserves_legacy_attribution(tmp_path, key):
    """Legacy key attribution remains an input to stopped import only."""
    store = Store(str(tmp_path))
    pr = view(closing_issue=5, head_ref_name="fix/5-thing")
    deaths = {-PR: 3} if key == "recovered" else {}
    if key != "recovered":
        Path(store.issue_path(5 if key == "issue" else -PR, "infra-deaths")).write_text("3")
    read = unproductive_rounds_reader(store, lambda: deaths)(pr)
    assert read == 3
    holds = review_external_holds(unproductive_rounds_reader(store, lambda: deaths), 3)
    assert holds(observed(), pr) == ("unproductive review round ceiling",)


from tests.adapters.test_review_execution_control import rig


def test_rearm_and_dispatch_read_the_same_current_allowance(rig, tmp_path):
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


def test_current_execution_historical_repair_hold_is_visible_and_owes_no_paid_review(rig):
    from issue_agent.adapters.effects import GitHubEffectExecutor
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.app.review_runtime import ActiveReviewRuntime
    from issue_agent.domain.review import ReviewConfig
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from tests.adapters.test_review_execution_admission import install_historical_repair_hold
    from tests.adapters.test_review_execution_control import OWNER
    store,_,gateway,selection,_=rig
    install_historical_repair_hold(store,selection)
    review=SQLiteReviewWorkflowControl(store,WorkflowWriter('o/r','review',OWNER),gateway=gateway,
                                      ceiling=3,trigger_label=Label.AI_PR_OPEN,rule=CountingRule('review-bot[bot]'))
    effects=GitHubEffectExecutor(gateway,store,repository='o/r',identity=OWNER,log=lambda _:None)
    runtime=ActiveReviewRuntime(review,effects,gateway,review_bot='review-bot[bot]',log=lambda _:None)
    plan=runtime.plan(SimpleNamespace(prs=[gateway.view]),ReviewConfig('review-bot[bot]'))
    assert plan.held[9]=='historical_pr_repair_migration_blocked'
    assert plan.owed=={} and plan.errors=={}
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM review_execution_rounds').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==0
