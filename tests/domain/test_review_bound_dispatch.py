"""Active review family dispatch (ADR 0015): the workflow's owed set decides, never sessions/last_sha."""

from dataclasses import replace

import pytest

from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.identity import IdentityMode
from issue_agent.domain.labels import Label
from issue_agent.domain.review import BoundReviewState, ReviewConfig, decide_bound_review
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowFence, WorkflowKey
from tests.domain.builders import pr, world
from tests.fakes.review_workflow import held

CFG = ReviewConfig(review_bot="review-bot")
ADOPT = ReviewConfig(review_bot="review-bot", adopt_unlabeled=True)


def binding(number: int):
    base = held().binding
    return replace(base, fence=WorkflowFence(WorkflowKey("o/r", PullRequestRef(number)), 0))


def owed(number: int, kind: AgentKind = AgentKind.REVIEW):
    return {number: (kind, binding(number))}


def test_an_owed_pr_dispatches_its_kind_with_the_captured_binding():
    p = pr(9, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[p]), CFG, BoundReviewState(owed=owed(9, AgentKind.RE_REVIEW)))
    assert actions == [RunAgent(AgentKind.RE_REVIEW, 9, workflow=binding(9))]


def test_a_pr_the_workflow_does_not_owe_is_never_dispatched_whatever_the_legacy_state():
    p = pr(9, "ai-pr-open", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), CFG, BoundReviewState()) == []


@pytest.mark.parametrize("state", [
    BoundReviewState(owed=owed(9), control_errors={9: "store unreadable"}),
    BoundReviewState(owed=owed(9), unproductive_rounds={9: 10**9}),
])
def test_unreadable_control_or_a_tripped_breaker_never_dispatches(state):
    p = pr(9, "ai-pr-open", head_ref_oid="abc")
    actions = decide_bound_review(world(prs=[p]), CFG, state)
    assert not [a for a in actions if isinstance(a, RunAgent)]


def test_the_unproductive_breaker_escalates_once_and_stays_quiet_after():
    p = pr(9, "ai-pr-open", head_ref_oid="abc")
    state = BoundReviewState(owed=owed(9), unproductive_rounds={9: CFG.max_unproductive_rounds})
    actions = decide_bound_review(world(prs=[p]), CFG, state)
    assert Relabel(Target.PR, 9, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions
    escalated = pr(9, "needs-human-review", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[escalated]), CFG, state) == []


def test_adoption_claims_and_dispatches_only_an_owed_pr():
    p = pr(200, head_ref_oid="abc", author="human")
    assert decide_bound_review(world(prs=[p]), ADOPT, BoundReviewState()) == []
    actions = decide_bound_review(world(prs=[p]), ADOPT, BoundReviewState(owed=owed(200)))
    claim = Relabel(Target.PR, 200, None, Label.AI_PR_OPEN, "abc")
    assert actions == [claim, RunAgent(AgentKind.REVIEW, 200, claim, workflow=binding(200))]


def test_solo_identity_never_reviews():
    solo = replace(CFG, identity_mode=IdentityMode.SOLO)
    p = pr(9, "ai-pr-open", head_ref_oid="abc")
    assert decide_bound_review(world(prs=[p]), solo, BoundReviewState(owed=owed(9))) == []


def test_a_review_dispatch_cannot_carry_another_prs_binding():
    with pytest.raises(ValueError, match="pull request identity"):
        RunAgent(AgentKind.REVIEW, 9, workflow=binding(10))
