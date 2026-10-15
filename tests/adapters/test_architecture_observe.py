"""Offline GitHub responses exercise the production history and pass-issue reads."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway
from issue_agent.domain.architecture import architecture_owed
from issue_agent.domain.policy import architecture_actions
from issue_agent.domain.state import Dispatch, Harness, Local, derive, issue_key
from issue_agent.domain.actions import AgentKind, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.routing import Tier
from tests.adapters.test_github_fix_observe import ServedGitHub, issue_node, labels, labeled

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
HEAD, LEDGER = "b" * 40, "a" * 40
H = Harness("fix-bot", "review-bot")


def ledger_page():
    return {"ledgerFile": {"__typename": "Blob"}, "ledgerTip": {"name": "dev", "target": {
        "oid": HEAD, "history": {"nodes": [{"oid": LEDGER, "committedDate": NOW.isoformat()}]}}}}


class Server(ServedGitHub):
    def __init__(self, ledger=None, count=5, passes=(), issues=(), search=None):
        super().__init__(issues=issues)
        self.ledger = ledger_page() if ledger is None else ledger
        self.count, self.passes, self.search = count, passes, search
        self.count_queries = []

    def __call__(self, argv, token):
        if argv[:2] == ["gh", "api"] and any(a.startswith("repos/") for a in argv):
            path = next(a for a in argv if a.startswith("repos/"))
            if "/branches/" in path and "/rules/" not in path:
                return json.dumps({"protected": False})
            if "/rules/" in path:
                return json.dumps([[]])
            return json.dumps({"workflow_runs": [], "workflows": [], "total_count": 0})
        pairs = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")
        query = pairs.get("query", "")
        if "ledgerFile:" in query:
            return json.dumps({"data": {"repository": self.ledger}})
        if "search(query:$q,type:ISSUE,first:1)" in query:
            self.count_queries.append(pairs["q"])
            if isinstance(self.count, Exception):
                raise self.count
            return json.dumps({"data": {"search": {"issueCount": self.count}}})
        if "search(" in query:
            result = ({"nodes": list(self.passes), "issueCount": len(self.passes)} if "Architecture pass:" in pairs["q"]
                      else {"nodes": [], "issueCount": 0})
            if "Architecture pass:" in pairs["q"] and self.search is not None:
                result = self.search
            return json.dumps({"data": {"search": result}})
        return super().__call__(argv, token)


def world(server):
    return GitHubGateway("o/r", runner=server).build_world("dev", filed_by=H.fix_bot)


@pytest.mark.parametrize("count,owed", [(4, False), (5, True), (10**12, True)])
def test_count_scopes_merges_to_base_and_the_last_ledger_commit(count, owed):
    server = Server(count=count)
    assert architecture_owed(world(server).architecture.history, NOW) is owed
    assert server.count_queries == [f'repo:o/r is:pr is:merged base:"dev" merged:>{NOW.isoformat()}']


def test_only_an_explicit_null_file_means_absent():
    page = ledger_page()
    page["ledgerFile"] = None
    server = Server(page)
    assert architecture_owed(world(server).architecture.history, NOW)
    assert not server.count_queries
    del page["ledgerFile"]
    assert world(Server(page)).architecture is None


@pytest.mark.parametrize("count", [None, True, -1, "5", {}, 2**1000, GhUnavailable("server", "unavailable")])
def test_bad_or_unavailable_count_never_becomes_five(count):
    observed = world(Server(count=count))
    assert not architecture_owed(observed.architecture.history, NOW)


def test_unavailable_count_still_allows_the_independent_age_rule():
    page = ledger_page()
    page["ledgerTip"]["target"]["history"]["nodes"][0]["committedDate"] = (NOW - timedelta(days=2)).isoformat()
    assert architecture_owed(world(Server(page, count=GhUnavailable("server", "down"))).architecture.history, NOW)


@pytest.mark.parametrize("field,bad", [
    ("ledgerFile", {}), ("ledgerFile", {"__typename": "Tree"}), ("ledgerTip", None),
    ("ledgerTip", {"name": "other"}), ("ledgerTip", {"name": "dev", "target": None}),
])
def test_malformed_history_defers_without_losing_ordinary_issue_work(field, bad):
    page = ledger_page()
    page[field] = bad
    observed = world(Server(page, issues=(issue_node(),)))
    assert observed.architecture is None
    assert [i.number for i in observed.issues] == [5]


@pytest.mark.parametrize("search", [
    {}, {"nodes": [], "issueCount": True}, {"nodes": [], "issueCount": 1},
    {"nodes": [{}] * 101, "issueCount": 101}, {"nodes": [None], "issueCount": 1},
])
def test_incomplete_pass_inventory_never_looks_empty(search):
    assert world(Server(search=search)).architecture is None


def test_unlabelled_pass_is_observed_and_readied_then_full_observation_resolves_at_high():
    node = issue_node(7, title="Architecture pass: 2026-10-16", body=H.marker, author={"login": "FIX-BOT "}, labels=labels())
    server = Server(passes=(node,))
    observed = world(server)
    assert architecture_actions(observed, H) == (Relabel(Target.ISSUE, 7, None, Label.AI_READY),)
    node = {**node, "labels": labels("ai-ready"), "labeled": labeled(("ai-ready", NOW.isoformat()))}
    observed = world(Server(passes=(node,), issues=(node,)))
    assert architecture_actions(observed, H) == ()
    assert derive(observed, Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


def test_unrecognised_search_hit_does_not_hide_five_new_merges():
    from dataclasses import replace
    from issue_agent.domain.actions import FileIssue
    node = issue_node(7, title="Architecture pass: 2026-10-16", body="no harness marker", author={"login": "fix-bot"})
    observed = replace(world(Server(passes=(node,), count=5)), now=NOW.isoformat())
    (action,) = architecture_actions(observed, H)
    assert isinstance(action, FileIssue) and action.title == "Architecture pass: 2026-10-16"


def test_tech_debt_label_survives_github_parsing():
    node = issue_node(labels=labels("tech-debt"))
    assert Label.TECH_DEBT in world(Server(issues=(node,))).issues[0].labels


@pytest.mark.parametrize("changes", [
    {"title": {}}, {"title": "Architecture pass:" + "x" * 100_000},
    {"body": ["bad"]}, {"body": H.marker + "x" * 100_000},
])
def test_malformed_pass_inventory_never_claims_no_pass_exists(changes):
    node = issue_node(7, title="Architecture pass: 2026-10-16", body=H.marker, author={"login": "fix-bot"}, labels=labels())
    assert world(Server(passes=({**node, **changes},))).architecture is None


@pytest.mark.parametrize("field", ["ledgerFile", "ledgerTip"])
def test_graphql_errors_on_optional_history_do_not_lose_normal_work(field):
    class ErrorServer(Server):
        def __call__(self, argv, token):
            result = super().__call__(argv, token)
            if any(a.startswith("query=") and "ledgerFile:" in a for a in argv):
                raw = json.loads(result)
                raw["data"]["repository"][field] = None
                raw["errors"] = [{"path": ["repository", field], "message": "unreadable"}]
                return json.dumps(raw)
            return result
    observed = world(ErrorServer(issues=(issue_node(),)))
    assert observed.architecture is None and len(observed.issues) == 1
