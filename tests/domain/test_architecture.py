"""Architecture passes are ordinary harness-filed issues (ADR 0017)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind, FileIssue, Relabel, Target
from issue_agent.domain.architecture import ArchitectureObservation, LedgerAbsent, LedgerPresent, architecture_owed
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import architecture_actions
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import Dispatch, Harness, Local, derive, issue_key
from issue_agent.domain.world import IssueView, Pickup, WorldState

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")
HEAD, LEDGER = "b" * 40, "a" * 40
PASS = IssueView(7, "Architecture pass: 2026-10-16", H.marker, author="FIX-BOT ")


@pytest.mark.parametrize("history,owed", [
    (LedgerAbsent(HEAD), True),
    (LedgerPresent(HEAD, LEDGER, NOW, 4), False),
    (LedgerPresent(HEAD, LEDGER, NOW, 5), True),
    (LedgerPresent(HEAD, LEDGER, NOW - timedelta(hours=24), 0), False),
    (LedgerPresent(HEAD, LEDGER, NOW - timedelta(hours=24, seconds=1), 0), True),
    (LedgerPresent(HEAD, HEAD, NOW - timedelta(days=2), 0), False),
    (LedgerPresent(HEAD, LEDGER, NOW + timedelta(seconds=1), 5), False),
    (None, False), ({}, False), ("absent", False),
])
def test_trigger_uses_only_observed_history(history, owed):
    assert architecture_owed(history, NOW) is owed
    assert not architecture_owed(history, NOW, in_flight=True)


@pytest.mark.parametrize("now", [None, "2026-10-16", datetime(2026, 10, 16)])
def test_unknown_clock_is_not_owed(now):
    assert not architecture_owed(LedgerAbsent(HEAD), now)


@pytest.mark.parametrize("changes", [
    {"base_head": None}, {"base_head": "x" * 100_000}, {"ledger_head": "bad"},
    {"updated_at": None}, {"updated_at": datetime(2026, 10, 16)},
    {"merged_prs": True}, {"merged_prs": -1}, {"merged_prs": "5"}, {"merged_prs": 2**1000},
])
def test_malformed_history_cannot_become_trusted(changes):
    with pytest.raises(ValueError):
        LedgerPresent(**({"base_head": HEAD, "ledger_head": LEDGER, "updated_at": NOW, "merged_prs": 5} | changes))


def world(*issues, history=LedgerAbsent(HEAD)):
    return WorldState(issues=issues, now=NOW.isoformat(), architecture=ArchitectureObservation(history, issues))


def test_policy_files_one_pass_then_readies_it_without_refiling():
    (action,) = architecture_actions(world(), H)
    assert isinstance(action, FileIssue)
    assert action.title == "Architecture pass: 2026-10-16"
    assert H.marker in action.body and "plugin/AGENT.md" in action.body
    assert architecture_actions(world(PASS), H) == (Relabel(Target.ISSUE, 7, None, Label.AI_READY),)
    ready = replace(PASS, labels=frozenset({Label.AI_READY}))
    assert architecture_actions(world(ready), H) == ()
    assert derive(world(ready), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


def test_needs_human_pass_is_neither_readied_nor_replaced():
    assert architecture_actions(world(replace(PASS, labels=frozenset({Label.NEEDS_HUMAN}))), H) == ()


@pytest.mark.parametrize("changes", [
    {"author": "alice"}, {"author": "review-bot"}, {"body": "no marker"},
    {"title": None}, {"title": {}}, {"title": "Architecture pass:" + "x" * 100_000},
    {"body": None}, {"body": {}}, {"body": H.marker + "x" * 100_000},
])
def test_untrusted_or_malformed_issues_never_get_architecture_privileges(changes):
    issue = replace(PASS, labels=frozenset({Label.AI_READY}), **changes)
    assert derive(world(issue), Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.STEWARD)}
    assert len([a for a in architecture_actions(world(issue), H) if isinstance(a, FileIssue)]) == 1


def test_tech_debt_requires_explicit_readiness_even_with_open_pickup():
    debt = IssueView(9, labels=frozenset({Label.TECH_DEBT}))
    observed = WorldState(issues=(debt,), pickup=Pickup.OPEN)
    assert derive(observed, Local(now=NOW), H) == {}
    assert derive(replace(observed, issues=(replace(debt, labels=debt.labels | {Label.AI_READY}),)), Local(now=NOW), H) == {
        issue_key(9): Dispatch(AgentKind.STEWARD)}
