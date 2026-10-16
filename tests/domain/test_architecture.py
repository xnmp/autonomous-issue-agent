"""Architecture passes are ordinary harness-filed issues (ADR 0017)."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.actions import AgentKind, FileIssue, Relabel, Target
from issue_agent.domain.architecture import NoPass, PassEnded, PassInFlight, architecture_owed
from issue_agent.domain.identity import Account
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import architecture_actions
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import Dispatch, Harness, Local, derive, issue_key
from issue_agent.domain.world import IssueView, Pickup, WorldState

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")
PASS = IssueView(7, "Architecture pass (first)", author=Account("User", "FIX-BOT"))


@pytest.mark.parametrize("observed,owed", [
    (NoPass(), True), (PassEnded(7, 4), False), (PassEnded(7, 5), True),
    (PassEnded(7, 10**12), True), (PassInFlight(PASS), False),
    (None, False), ({}, False), ("absent", False),
])
def test_trigger_counts_from_the_last_pass(observed, owed):
    assert architecture_owed(observed) is owed


@pytest.mark.parametrize("number,count", [
    (None, 5), (True, 5), (0, 5), (-1, 5), ("7", 5), (2**1000, 5),
    (7, None), (7, True), (7, -1), (7, "5"), (7, {}), (7, 2**1000),
])
def test_malformed_pass_facts_cannot_become_trusted(number, count):
    with pytest.raises(ValueError):
        PassEnded(number, count)


def world(*issues, observed=NoPass()):
    return WorldState(issues=issues, now=NOW.isoformat(), architecture=observed)


def test_policy_files_one_pass_then_readies_it_without_refiling():
    (action,) = architecture_actions(world(), H)
    assert isinstance(action, FileIssue)
    assert action.title == "Architecture pass (first)"
    assert H.marker in action.body
    assert architecture_actions(world(PASS, observed=PassInFlight(PASS)), H) == (
        Relabel(Target.ISSUE, 7, None, Label.AI_READY),)
    ready = replace(PASS, labels=frozenset({Label.AI_READY}))
    assert architecture_actions(world(ready, observed=PassInFlight(ready)), H) == ()
    assert derive(world(ready), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


def test_issue_body_supplies_the_complete_bounded_pass_contract():
    (action,) = architecture_actions(world(), H)
    assert isinstance(action, FileIssue)
    for instruction in (
        "docs/architecture-pressure.md", "Covered through: <full commit sha on base>",
        "from that SHA (exclusive) to the current base tip (inclusive)", "from the beginning",
        "PRs' review threads", "residual sections", "seam", "evidence links", "trend",
        "candidate intervention", "reconsideration trigger", "expiry", "drop expired entries",
        "keep the ledger small", "defer", "at most **3**", "`tech-debt`", "Never mark",
        "`ai-ready`", "link each from the ledger", "Closes #<issue>",
    ):
        assert instruction in action.body
    assert "plugin/AGENT.md" not in action.body
    assert len(action.body) < 3000
    assert architecture_actions(world(observed=PassEnded(7, 5)), H)[0].body == action.body


def test_needs_human_pass_is_neither_readied_nor_replaced():
    parked = replace(PASS, labels=frozenset({Label.NEEDS_HUMAN}))
    assert architecture_actions(world(parked, observed=PassInFlight(parked)), H) == ()


@pytest.mark.parametrize("observed,title", [
    (NoPass(), "Architecture pass (first)"), (PassEnded(7, 5), "Architecture pass (after #7)"),
])
def test_same_title_and_key_across_ticks_and_midnight(observed, title):
    before = replace(world(observed=observed), now="2026-10-15T23:59:59Z")
    after = replace(before, now="2026-10-16T00:00:01Z")
    (action,) = architecture_actions(before, H)
    assert action.title == title and action.key == f"architecture:{title}"
    assert architecture_actions(before, H) == architecture_actions(after, H) == (action,)


@pytest.mark.parametrize("body", ["", None, {}, "x" * 100_000])
def test_pass_recognition_does_not_need_the_issue_body(body):
    ready = replace(PASS, body=body, labels=frozenset({Label.AI_READY}))
    assert derive(world(ready), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


@pytest.mark.parametrize("changes", [
    {"author": "alice"}, {"author": "review-bot"}, {"author": None},
    {"title": None}, {"title": {}}, {"title": "Architecture pass" + "x" * 100_000},
])
def test_untrusted_or_malformed_issues_never_get_architecture_privileges(changes):
    issue = replace(PASS, labels=frozenset({Label.AI_READY}), **changes)
    assert derive(world(issue), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.STEWARD)}


def test_old_date_titles_keep_the_architecture_origin():
    ready = replace(PASS, title="Architecture pass: 2026-10-15", labels=frozenset({Label.AI_READY}))
    assert derive(world(ready), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


def test_tech_debt_requires_explicit_readiness_even_with_open_pickup():
    debt = IssueView(9, labels=frozenset({Label.TECH_DEBT}))
    observed = WorldState(issues=(debt,), pickup=Pickup.OPEN)
    assert derive(observed, Local(now=NOW), H) == {}
    assert derive(replace(observed, issues=(replace(debt, labels=debt.labels | {Label.AI_READY}),)), Local(now=NOW), H) == {
        issue_key(9): Dispatch(AgentKind.STEWARD)}
