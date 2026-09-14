"""A damaged target's file must not buy work or starve another target."""

from issue_agent.adapters.store import Store
from issue_agent.app.roles import _fix_state, _review_state
from issue_agent.domain.actions import RunAgent
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewConfig, decide_review
from issue_agent.domain.world import IssueView, PRView, WorldState
import pytest


def test_corrupt_resolve_counter_holds_only_affected_issue(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-1.resolve-retries").write_text("oops")
    world = WorldState(issues=tuple(
        IssueView(n, labels=frozenset({Label.AI_READY}), updated_at=f"r{n}")
        for n in (1, 2)
    ))
    state = _fix_state(store, world)
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
    assert "issue-1.resolve-retries" in state.control_errors[1]


def test_corrupt_review_counter_holds_only_affected_pr(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue--1.infra-deaths").write_text("oops")
    world = WorldState(prs=tuple(
        PRView(n, labels=frozenset({Label.AI_PR_OPEN}), head_ref_oid=f"h{n}")
        for n in (1, 2)
    ))
    state = _review_state(store, world)
    actions = decide_review(world, ReviewConfig(review_bot="review-bot"), state)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
    assert "issue--1.infra-deaths" in state.control_errors[1]


@pytest.mark.parametrize("raw", [
    '{"target":"ISSUE","number":1,"reservation_micros":1e309,"scope":"issue","cap_micros":100}',
    '{"target":"ISSUE","number":1,"reservation_micros":true,"scope":"issue","cap_micros":100}',
])
def test_structured_control_corruption_does_not_starve_peer(tmp_path, raw):
    (tmp_path / "issue-1.budget-block").write_text(raw)
    world = WorldState(issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY}), updated_at="r")
                                  for n in (1, 2)))
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"),
                         _fix_state(Store(str(tmp_path)), world), fixer_busy=False)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
