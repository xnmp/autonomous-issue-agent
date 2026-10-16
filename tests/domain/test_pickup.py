"""ISSUE_PICKUP: which open issues derive picks up (`picked_up`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.identity import configured_account
from issue_agent.domain.labels import Label
from issue_agent.domain.state import Dispatch, Harness, Local, Wait, WaitReason, derive, harness_gap, issue_key, picked_up
from issue_agent.domain.world import IssueView, Pickup, PRView, WorldState

H = Harness("fix-bot", "review-bot")
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def _derive(pickup: Pickup, *issues: IssueView, prs: tuple[PRView, ...] = ()):
    return derive(WorldState(issues=issues, prs=prs, pickup=pickup), Local(now=NOW), H)


def _open(number: int = 3, *labels: Label, **kw) -> IssueView:
    return IssueView(number=number, labels=frozenset(labels), author=configured_account(kw.pop("author", "alice")), **kw)


def test_an_unlabelled_open_issue_is_picked_up_only_when_every_open_issue_is():
    unlabelled = _open(3)
    assert _derive(Pickup.OPEN, unlabelled) == {issue_key(3): Dispatch(AgentKind.STEWARD)}
    assert _derive(Pickup.READY, unlabelled) == {}


def test_a_needs_human_issue_waits_for_its_human_in_either_mode():
    parked = _open(3, Label.NEEDS_HUMAN)
    assert _derive(Pickup.OPEN, parked) == {issue_key(3): Wait(WaitReason.NEEDS_HUMAN)}
    assert _derive(Pickup.READY, parked) == {}


def test_a_closed_issue_is_never_picked_up():
    assert _derive(Pickup.OPEN, _open(3, state="closed"), _open(4, Label.AI_READY, state="closed")) == {}


def test_the_harness_gap_reports_never_queue_themselves():
    gap = _open(3, author="fix-bot", title="Harness gap: the reload watch missed a knob")
    assert harness_gap(gap, H)
    assert _derive(Pickup.OPEN, gap) == {}
    # A human marking it ready is the deliberate route in.
    marked = _open(3, Label.AI_READY, author="fix-bot", title=gap.title)
    assert _derive(Pickup.OPEN, marked) == _derive(Pickup.READY, marked) == {issue_key(3): Dispatch(AgentKind.STEWARD)}


@pytest.mark.parametrize("issue", [
    _open(3, author="FIX-BOT ", title="CI: tests failing on dev"),  # a base-CI issue stays pickable (#402)
    _open(3, author="alice", title="Harness gap: filed by a human"),  # only FIX_BOT's are the harness's
    _open(3, author="fix-bot", title="harness gap: lower case is not the filer's title"),
    _open(3, author="", title="Harness gap: no author read"),
])
def test_only_fix_bots_harness_gap_titles_are_excluded(issue):
    assert picked_up(issue, H, Pickup.OPEN)


def test_an_issue_with_a_harness_pr_is_the_prs_item_not_a_second_one():
    pr = PRView(number=9, head_ref_name="fix/3-thing", state="MERGED", content_known=False)
    assert _derive(Pickup.OPEN, _open(3), prs=(pr,)) == {}


def test_many_open_issues_are_each_an_item():
    issues = tuple(_open(n) for n in range(1, 2001))
    assert len(_derive(Pickup.OPEN, *issues)) == 2000


@pytest.mark.parametrize("number", [0, -1])
def test_a_malformed_issue_number_is_never_picked_up(number):
    assert not picked_up(_open(number), H, Pickup.OPEN)
