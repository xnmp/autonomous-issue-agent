"""Which open PRs are the harness's (ADR 0018): those on a branch the act
layer recorded opening, whatever account opened them or wrote their fields.
An unrecorded PR at a commit a harness leg pushed is recorded (OpenPR on the
PR, at that head), whatever its issue's state. Everything else is not the
harness's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind, OpenPullRequest
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import actions
from issue_agent.domain.state import Dispatch, Harness, Local, OpenPR, derive, issue_key, pr_key
from issue_agent.domain.world import Branch, IssueView, PRView, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
H = Harness("fix-bot", "review-bot")
BRANCH = "fix/3-slug"
GREEN = Observed("h1", "h1", frozenset({RequiredCheck("test")}), (Check("test", state="COMPLETED", bucket="pass",
                                                                        required=True),))
TRIAGED = (LegEnd(AgentKind.STEWARD, T0 - timedelta(hours=2), False),
           LegEnd(AgentKind.TRIAGE, T0 - timedelta(hours=1), False))


def pr(head: str = "h1", branch: str = BRANCH, **kw) -> PRView:
    return PRView(number=7, head_ref_name=branch, head_ref_oid=head, ci=kw.pop("ci", GREEN), mergeable="MERGEABLE",
                  merge_state="CLEAN", **kw)


def world(*prs: PRView, branches: tuple[Branch, ...] = ()) -> WorldState:
    return WorldState(issues=(IssueView(3, labels=frozenset({Label.AI_READY})),), prs=prs, branches=branches)


def local(owned: frozenset[str] = frozenset(), **legs) -> Local:
    by = {issue_key(3): (*TRIAGED, *legs.get("issue", ())), pr_key(7): legs.get("pr", ())}
    return Local(now=NOW, legs=by, owned=owned)


def resolved(end_head: str) -> LegEnd:
    return LegEnd(AgentKind.RESOLVE, T0, False, end_head=end_head)


def test_a_recorded_pr_is_the_harness_item_whoever_authored_it():
    states = derive(world(pr(author="alice")), local(frozenset({BRANCH})), H)
    assert states == {pr_key(7): Dispatch(AgentKind.REVIEW, "h1")}


def test_an_unrecorded_harness_named_pr_is_not_an_item_and_keeps_its_issue_shadowed():
    """A human's `fix/<n>-` PR (the harness repository's #145, #128) is left alone."""
    assert derive(world(pr(author="xnmp")), local(), H) == {}


def test_a_merged_unrecorded_pr_still_means_its_issue_is_done():
    assert derive(world(pr(state="MERGED")), local(), H) == {}


def test_an_unrecorded_pr_whose_head_a_resolve_leg_pushed_is_recorded_at_that_head():
    """A PR a leg opened itself, or one from before records existed."""
    states = derive(world(pr("pushed")), local(issue=(resolved("pushed"),)), H)
    assert states == {pr_key(7): OpenPR(BRANCH, "pushed")}
    assert actions(states, H) == (OpenPullRequest(3, BRANCH, "pushed"),)


def test_an_unrecorded_pr_whose_head_a_pr_stage_leg_pushed_is_recorded():
    rebased = LegEnd(AgentKind.REBASE, T0, False, head="old", end_head="rebased")
    assert derive(world(pr("rebased")), local(pr=(rebased,)), H) == {pr_key(7): OpenPR(BRANCH, "rebased")}


@pytest.mark.parametrize(
    "issues",
    [(), (IssueView(3),), (IssueView(3, state="closed", labels=frozenset({Label.AI_READY})),),
     (IssueView(3, labels=frozenset({Label.AI_READY, Label.NEEDS_HUMAN})),)],
    ids=["unobserved", "not-ready", "closed", "held"],
)
def test_adoption_does_not_depend_on_the_issue_being_an_item(issues):
    """ballast #82's issue is `tech-debt`; a human may un-ready an issue
    while its PR is open. The PR is recorded and goes on as the harness's."""
    rebased = LegEnd(AgentKind.REBASE, T0, False, head="old", end_head="rebased")
    states = derive(WorldState(issues=issues, prs=(pr("rebased"),)), local(pr=(rebased,)), H)
    assert states == {pr_key(7): OpenPR(BRANCH, "rebased")}


def test_another_unrecorded_pr_of_the_issue_does_not_hold_adoption_back():
    human = PRView(number=8, head_ref_name="fix/3-human", head_ref_oid="theirs")
    states = derive(world(pr("pushed"), human), local(issue=(resolved("pushed"),)), H)
    assert states == {pr_key(7): OpenPR(BRANCH, "pushed")}


def test_a_recorded_pr_is_never_adopted_again():
    states = derive(world(pr("h1")), local(frozenset({BRANCH}), issue=(resolved("h1"),)), H)
    assert states == {pr_key(7): Dispatch(AgentKind.REVIEW, "h1")}


@pytest.mark.parametrize(
    "ends",
    [
        {"pr": (LegEnd(AgentKind.REVIEW, T0, False, head="h1", end_head="h1"),)},  # reviewed, pushed nothing
        {"pr": (LegEnd(AgentKind.REVIEW, T0, False, head="h1"),)},  # end head unread
        {"issue": (resolved("older"),)},  # a human pushed after the harness's push
        {"issue": (resolved(""),)},
    ],
    ids=["review-only", "unread", "pushed-over", "no-end-head"],
)
def test_a_pr_no_harness_leg_pushed_its_head_is_never_adopted(ends):
    assert derive(world(pr("h1")), local(**ends), H) == {}


def test_an_unrecorded_pr_with_an_unread_head_is_never_adopted():
    assert derive(world(pr("", content_known=False)), local(issue=(resolved(""),)), H) == {}


def test_a_pr_open_on_an_issue_item_or_a_record_on_an_issue_is_refused_by_policy():
    with pytest.raises(ValueError):
        actions({issue_key(3): OpenPR(BRANCH, "h1")}, H)
    with pytest.raises(ValueError):
        actions({pr_key(7): OpenPR(BRANCH)}, H)


def test_a_recorded_branch_whose_pr_never_opened_is_opened_again():
    """Crash point: the act layer recorded the branch and died before GitHub
    opened the PR. The pushed branch has no PR, so OpenPR is owed again."""
    states = derive(world(branches=(Branch(BRANCH, T0.isoformat()),)), local(frozenset({BRANCH})), H)
    assert states == {issue_key(3): OpenPR(BRANCH)}


def test_a_record_for_another_branch_does_not_own_this_pr():
    assert derive(world(pr()), local(frozenset({"fix/3-other"})), H) == {}


@pytest.mark.parametrize(
    "owned",
    [{"fix/3-slug"}, frozenset({""}), frozenset({7}), ("fix/3-slug",), None],
    ids=["set", "empty-name", "not-text", "tuple", "none"],
)
def test_malformed_records_are_refused(owned):
    with pytest.raises(ValueError, match="owned"):
        Local(now=NOW, owned=owned)  # type: ignore[arg-type]


def test_many_records_cost_nothing_extra_to_match():
    owned = frozenset(f"fix/{n}-slug" for n in range(1, 50_000))
    assert derive(world(pr()), local(owned), H) == {pr_key(7): Dispatch(AgentKind.REVIEW, "h1")}
