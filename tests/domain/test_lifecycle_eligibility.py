"""Reachable partial label transitions cannot override terminal state or holds."""

from tests.domain.builders import observed_pr

import pytest

from issue_agent.domain.actions import EnableAutoMerge, RunAgent, UpdateBranch
from issue_agent.domain.eligibility import IssueHoldGuard
from issue_agent.domain.lifecycle import hold_guard, hold_guard_applies
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.review import BoundReviewState, ReviewConfig, decide_bound_review
from tests.domain.test_review_bound_dispatch import owed
from issue_agent.domain.world import Check, Comment, IssueView, PRView, Review, WorldState


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
    pr = observed_pr(2, labels=frozenset({Label.AI_PR_OPEN, hold}), head_ref_oid="head")
    actions = decide_bound_review(WorldState(prs=(pr,)),
                                  ReviewConfig(review_bot="review-bot"), BoundReviewState(owed=owed(2)))
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
    pr = observed_pr(2, author="fix-bot", head_ref_name="fix/1-x", head_ref_oid="head",
                merge_state="CLEAN", labels=labels,
                reviews=(Review("APPROVED", "review-bot", commit_id="head"),),
                checks=(Check("build", bucket="pass"),))
    actions = decide_fix(WorldState(prs=(pr,)), FixConfig(fix_bot="fix-bot", ),
                         FixState(), fixer_busy=False)
    assert any(isinstance(a, EnableAutoMerge) for a in actions) is (hold is None)
    if hold is not None:
        assert not any(isinstance(a, UpdateBranch) for a in actions)


def test_invalid_linked_issue_state_does_not_disable_its_hold():
    issue = IssueView(1, state="unknown", labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW}))
    pr = observed_pr(2, author="fix-bot", head_ref_name="fix/1-x", closing_issue=1,
                labels=frozenset({Label.AI_PR_OPEN}),
                reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(WorldState(issues=(issue,), prs=(pr,)), FixConfig(fix_bot="fix-bot"),
                         FixState(), fixer_busy=False)
    assert not any(isinstance(a, RunAgent) for a in actions)


# -- ADR 0015 semantic hold fence ---------------------------------------------

NHR, READY = Label.NEEDS_HUMAN_REVIEW, Label.AI_READY
OWN = {"removed": (NHR,), "added": (READY,)}


def _held(**changes) -> IssueView:
    fields = {"labels": frozenset({NHR}), "updated_at": "r1", "title": "t", "body": "b"}
    return IssueView(1, **{**fields, **changes})


@pytest.mark.parametrize("current", [
    _held(updated_at="r9", comments=(Comment("alice", "any news?", "r9"),)),
    _held(updated_at="r9", body="edited", title="retitled"),
    _held(labels=frozenset({NHR, Label.P1})),
    _held(labels=frozenset({NHR, READY})),  # this command's own half-applied add
    _held(labels=frozenset({READY})),  # this command fully applied
    _held(labels=frozenset()),  # own removal landed, add did not
])
def test_harmless_activity_and_own_effects_keep_the_hold_fence(current):
    assert hold_guard_applies(hold_guard(_held()), current, linked_pr=None, **OWN)


@pytest.mark.parametrize("current, linked_pr", [
    (_held(labels=frozenset({NHR, Label.AI_PARKED})), None),
    (_held(labels=frozenset({NHR, Label.RECURRENCE})), None),
    (_held(labels=frozenset({NHR, Label.AI_DONE})), None),
    (_held(state="closed", state_reason="completed"), None),
    (_held(state_reason="reopened"), None),
    (_held(), 70),
])
def test_interventions_break_the_hold_fence(current, linked_pr):
    assert not hold_guard_applies(hold_guard(_held()), current, linked_pr=linked_pr, **OWN)


def test_a_removed_hold_that_the_command_did_not_declare_breaks_the_fence():
    guard = hold_guard(_held(labels=frozenset({NHR, Label.AI_PARKED})))
    assert not hold_guard_applies(guard, _held(labels=frozenset({NHR})), linked_pr=None, **OWN)


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


def test_an_untyped_guard_is_never_evaluated():
    with pytest.raises(ValueError):
        hold_guard_applies({"labels": []}, _held(), linked_pr=None)


def test_huge_label_sets_are_compared_exactly():
    many = frozenset(Label) - {Label.P1, Label.P2, Label.P3}
    guard = hold_guard(_held(labels=many))
    assert hold_guard_applies(guard, _held(labels=many), linked_pr=None)
    assert not hold_guard_applies(guard, _held(labels=many - {Label.AI_PARKED}), linked_pr=None)
