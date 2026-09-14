"""Reachable partial label transitions cannot override terminal state or holds."""

import pytest

from issue_agent.domain.actions import EnableAutoMerge, RunAgent, UpdateBranch
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewConfig, ReviewState, decide_review
from issue_agent.domain.world import Check, IssueView, PRView, Review, WorldState


@pytest.mark.parametrize("hold", [
    Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_BUDGET_REVIEW,
    Label.AI_BLOCKED, Label.AI_PARKED, Label.AI_DONE,
])
def test_ready_issue_with_hold_does_not_buy_resolve(hold):
    issue = IssueView(1, labels=frozenset({Label.AI_READY, hold}), updated_at="r1")
    actions = decide_fix(WorldState(issues=(issue,)), FixConfig(fix_bot="fix-bot"),
                         FixState(), fixer_busy=False)
    assert not any(isinstance(action, RunAgent) for action in actions)


@pytest.mark.parametrize("hold", [
    Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_BUDGET_REVIEW,
    Label.AI_BLOCKED, Label.AI_PARKED, Label.NO_AI_REVIEW,
])
def test_review_trigger_with_hold_does_not_buy_review(hold):
    pr = PRView(2, labels=frozenset({Label.AI_PR_OPEN, hold}), head_ref_oid="head")
    actions = decide_review(WorldState(prs=(pr,)),
                            ReviewConfig(review_bot="review-bot"), ReviewState())
    assert not any(isinstance(action, RunAgent) for action in actions)


@pytest.mark.parametrize("state", ["closed", "unknown", ""])
def test_non_open_issue_cannot_buy_resolve(state):
    issue = IssueView(1, labels=frozenset({Label.AI_READY}), state=state)
    actions = decide_fix(WorldState(issues=(issue,)), FixConfig(fix_bot="fix-bot"),
                         FixState(), fixer_busy=False)
    assert not any(isinstance(action, RunAgent) for action in actions)


@pytest.mark.parametrize("hold", [None, Label.AI_PARKED, Label.NEEDS_BUDGET_REVIEW, Label.NEEDS_DESIGN])
def test_merge_and_branch_update_share_explicit_hold_policy(hold):
    labels = frozenset({Label.AI_PR_OPEN} | ({hold} if hold else set()))
    pr = PRView(2, author="fix-bot", head_ref_name="fix/1-x", head_ref_oid="head",
                merge_state="CLEAN", labels=labels,
                reviews=(Review("APPROVED", "review-bot", commit_id="head"),),
                checks=(Check("build", bucket="pass"),))
    actions = decide_fix(WorldState(prs=(pr,)), FixConfig(fix_bot="fix-bot", arm_on_open=True),
                         FixState(), fixer_busy=False)
    assert any(isinstance(a, EnableAutoMerge) for a in actions) is (hold is None)
    if hold is not None:
        assert not any(isinstance(a, UpdateBranch) for a in actions)


def test_invalid_linked_issue_state_does_not_disable_its_hold():
    issue = IssueView(1, state="unknown", labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW}))
    pr = PRView(2, author="fix-bot", head_ref_name="fix/1-x", closing_issue=1,
                labels=frozenset({Label.AI_PR_OPEN}),
                reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(WorldState(issues=(issue,), prs=(pr,)), FixConfig(fix_bot="fix-bot"),
                         FixState(), fixer_busy=False)
    assert not any(isinstance(a, RunAgent) for a in actions)
