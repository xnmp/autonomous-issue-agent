"""Offline GitHub responses exercise pass discovery and strict merge boundaries."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway
from issue_agent.domain.actions import AgentKind, FileIssue, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import architecture_actions
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import Dispatch, Harness, Local, derive, issue_key
from tests.adapters.test_github_fix_observe import ServedGitHub, issue_node, labels, labeled

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")


def merge(at=NOW, *, issue=7, base="dev", repo="o/r"):
    return {"number": issue + 100, "state": "MERGED", "mergedAt": at.isoformat(),
            "headRefName": f"fix/{issue}-pass", "baseRefName": base, "repository": {"nameWithOwner": repo}}


def pass_issue(number=7, *, pr=None, closed=None, **changes):
    return issue_node(number, title="Architecture pass (first)", author={"__typename": "User", "login": "FIX-BOT"}, labels=labels(),
                      state="CLOSED" if closed else "OPEN", closedAt=closed,
                      timelineItems={"pageInfo": {"hasPreviousPage": False},
                                     "nodes": [{"source": pr}] if pr else []}, **changes)


def work_merges(count):
    return tuple(merge(NOW + timedelta(seconds=i + 1), issue=20 + i) for i in range(count))


class Server(ServedGitHub):
    def __init__(self, *, passes=(), merges=(), count=..., issues=(), search=None):
        super().__init__(issues=issues)
        self.passes, self.merges, self.count, self.search = passes, merges, count, search
        self.pass_queries, self.count_queries, self.graphql_queries = [], [], []

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
        self.graphql_queries.append(query)
        if "search(query:$q,type:ISSUE,first:1)" in query:
            self.count_queries.append(pairs["q"])
            if isinstance(self.count, Exception):
                raise self.count
            boundary = datetime.fromisoformat(pairs["q"].split("merged:>")[1])
            count = sum(p["baseRefName"] == "dev" and p["repository"]["nameWithOwner"] == "o/r"
                        and datetime.fromisoformat(p["mergedAt"]) > boundary for p in self.merges)
            return json.dumps({"data": {"search": {"issueCount": count if self.count is ... else self.count}}})
        if "search(" in query:
            result = {"nodes": [], "issueCount": 0}
            if '"Architecture pass"' in pairs["q"]:
                self.pass_queries.append(pairs["q"])
                result = self.search if self.search is not None else {
                    "nodes": sorted(self.passes, key=lambda n: n["number"], reverse=True)[:20],
                    "issueCount": len(self.passes)}
            return json.dumps({"data": {"search": result}})
        return super().__call__(argv, token)


def world(server):
    return GitHubGateway("o/r", runner=server).build_world("dev", filed_by=H.fix_bot)


def filing(observed):
    return tuple(a for a in architecture_actions(observed, H) if isinstance(a, FileIssue))


def test_no_pass_ever_files_first_without_a_count_or_ledger_read():
    server = Server()
    (action,) = filing(world(server))
    assert action.title == "Architecture pass (first)"
    assert server.pass_queries == ['repo:o/r is:issue author:fix-bot in:title "Architecture pass" sort:created-desc']
    assert not server.count_queries
    assert all("ledgerFile" not in q and "history(" not in q for q in server.graphql_queries)


@pytest.mark.parametrize("count,owed", [(0, False), (4, False), (5, True)])
@pytest.mark.parametrize("state", ["open", "closed"])
def test_merged_pass_ends_at_merge_even_if_open_or_closed_later(count, owed, state):
    pr = merge()
    closed = (NOW + timedelta(hours=1)).isoformat() if state == "closed" else None
    node = pass_issue(pr=pr, closed=closed)
    # The pass PR, another simultaneous merge, older work, and a different base do not count.
    merges = (pr, merge(issue=8), merge(NOW - timedelta(days=2)),
              merge(NOW + timedelta(minutes=1), base="other")) + work_merges(count)
    server = Server(passes=(node,), merges=merges, issues=(node,) if state == "open" else ())
    observed = world(server)
    assert bool(filing(observed)) is owed
    assert server.count_queries == [f'repo:o/r is:pr is:merged base:"dev" merged:>{NOW.isoformat()}']
    assert len(server.pass_queries) + len(server.count_queries) == 2
    assert not derive(observed, Local(now=NOW), H)  # An unclosed merged pass stays done.
    if owed:
        assert filing(observed)[0].title == "Architecture pass (after #7)"
    else:
        assert architecture_actions(replace(observed, now=(NOW + timedelta(days=3)).isoformat()), H) == ()


@pytest.mark.parametrize("count,owed", [(4, False), (5, True)])
def test_closed_unmerged_pass_counts_from_close_time(count, owed):
    node = pass_issue(closed=NOW.isoformat())
    server = Server(passes=(node,), merges=(merge(issue=8),) + work_merges(count))
    assert bool(filing(world(server))) is owed
    assert server.count_queries == [f'repo:o/r is:pr is:merged base:"dev" merged:>{NOW.isoformat()}']


def test_open_pass_is_in_flight_and_unlabelled_pass_is_readied_without_its_body():
    node = pass_issue()
    server = Server(passes=(node,), merges=work_merges(5))
    assert architecture_actions(world(server), H) == (Relabel(Target.ISSUE, 7, None, Label.AI_READY),)
    assert not server.count_queries
    node = {**node, "labels": labels("ai-ready"), "labeled": labeled(("ai-ready", NOW.isoformat()))}
    observed = world(Server(passes=(node,), issues=(node,)))
    assert architecture_actions(observed, H) == ()
    assert derive(observed, Local(now=NOW), H) == {issue_key(7): Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)}


def test_only_the_newest_bot_authored_pass_anchors_the_count():
    passes = tuple(pass_issue(n) for n in range(1, 8))
    latest = pass_issue(8, closed=NOW.isoformat())
    stranger = {**pass_issue(9), "author": {"__typename": "User", "login": "alice"}}
    server = Server(passes=passes + (latest, stranger), merges=work_merges(5))
    assert filing(world(server))[0].title == "Architecture pass (after #8)"


def test_newest_means_creation_order_from_the_search():
    newest = pass_issue(7, closed=NOW.isoformat())
    search = {"nodes": [newest, pass_issue(8)], "issueCount": 2}
    assert filing(world(Server(search=search, merges=work_merges(5))))[0].title == "Architecture pass (after #7)"


def test_older_pass_details_and_label_history_do_not_control_the_trigger():
    newest = {**pass_issue(8, closed=NOW.isoformat()), "labels": labels("needs-human")}
    older = {**pass_issue(7), "state": None, "labels": None, "timelineItems": None}
    assert filing(world(Server(passes=(older, newest), merges=work_merges(5))))[0].title == "Architecture pass (after #8)"


@pytest.mark.parametrize("changes", [{"author": {"__typename": "User", "login": "alice"}}, {"title": "Discuss Architecture pass"}])
def test_non_bot_or_non_prefix_search_hits_are_ignored(changes):
    node = {**pass_issue(), **changes}
    assert filing(world(Server(passes=(node,))))[0].title == "Architecture pass (first)"


@pytest.mark.parametrize("noise_count", [1, 6, 19])
def test_newer_phrase_matches_do_not_hide_the_newest_genuine_pass(noise_count):
    noise = tuple({**pass_issue(10 + n), "title": "Discuss Architecture pass"} for n in range(noise_count))
    server = Server(passes=(pass_issue(6), pass_issue(7, closed=NOW.isoformat())) + noise,
                    merges=work_merges(5))
    assert filing(world(server))[0].title == "Architecture pass (after #7)"
    assert len(server.pass_queries) == 1


@pytest.mark.parametrize("total", [20, 21, 1000])
def test_twenty_non_genuine_hits_are_unknown_not_owed_and_logged_once(total, capsys):
    noise = [{**pass_issue(n), "title": "Discuss Architecture pass"} for n in range(1, 21)]
    server = Server(search={"nodes": noise, "issueCount": total}, issues=(issue_node(),))
    observed = world(server)
    assert observed.architecture is None
    assert architecture_actions(observed, H) == ()
    assert [i.number for i in observed.issues] == [5]
    assert not server.count_queries and len(server.pass_queries) == 1
    assert capsys.readouterr().err.count("architecture unread this poll") == 1


def test_a_merge_for_another_issue_or_repo_does_not_end_an_open_pass():
    for pr in (merge(issue=8), merge(repo="fork/r")):
        observed = world(Server(passes=(pass_issue(pr=pr),), merges=work_merges(5)))
        assert architecture_actions(observed, H) == (Relabel(Target.ISSUE, 7, None, Label.AI_READY),)


@pytest.mark.parametrize("count", [None, True, -1, "5", {}, 2**1000, GhUnavailable("server", "unavailable")])
def test_bad_or_unavailable_count_never_becomes_five(count):
    observed = world(Server(passes=(pass_issue(closed=NOW.isoformat()),), count=count, issues=(issue_node(),)))
    assert architecture_actions(observed, H) == ()
    assert [i.number for i in observed.issues] == [5]


def test_large_valid_count_is_owed():
    assert filing(world(Server(passes=(pass_issue(closed=NOW.isoformat()),), count=10**12)))


@pytest.mark.parametrize("search", [
    {}, {"nodes": [], "issueCount": True}, {"nodes": [], "issueCount": 1},
    {"nodes": [], "issueCount": 2**1000}, {"nodes": [{}] * 100_000, "issueCount": 100_000},
    {"nodes": [None], "issueCount": 1},
    {"nodes": [{**pass_issue(n), "title": "Discuss Architecture pass"} for n in range(1, 6)], "issueCount": 6},
])
def test_incomplete_or_huge_inventory_never_looks_empty(search):
    observed = world(Server(search=search, issues=(issue_node(),)))
    assert architecture_actions(observed, H) == () and len(observed.issues) == 1


@pytest.mark.parametrize("changes", [
    {"title": {}}, {"title": "Architecture pass" + "x" * 100_000},
    {"number": True}, {"number": 0}, {"number": 2**1000}, {"state": "UNKNOWN"}, {"author": None},
    {"author": {"login": "x" * 100_000}},
    {"timelineItems": {"pageInfo": {"hasPreviousPage": True}, "nodes": []}},
    {"timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": [None] * 100_000}},
])
def test_malformed_pass_inventory_defers(changes):
    node = {**pass_issue(), **changes}
    observed = world(Server(search={"nodes": [node], "issueCount": 1}))
    assert architecture_actions(observed, H) == ()


@pytest.mark.parametrize("stamp", [None, "", {}, "x" * 100_000, "2026-10-16", "invalid"])
@pytest.mark.parametrize("merged", [False, True])
def test_unknown_or_malformed_end_never_falls_back_to_an_older_boundary(stamp, merged):
    node = pass_issue(closed=NOW.isoformat(), pr={**merge(), "mergedAt": stamp} if merged else None)
    if not merged:
        node["closedAt"] = stamp
    observed = world(Server(passes=(node,), merges=work_merges(5)))
    assert architecture_actions(observed, H) == ()


@pytest.mark.parametrize("field", ["number", "headRefName", "repository", "state"])
@pytest.mark.parametrize("missing", [False, True])
def test_unreadable_pr_reference_never_falls_back_to_close_time(field, missing):
    pr = merge()
    if missing:
        del pr[field]
    else:
        pr[field] = None
    node = pass_issue(closed=NOW.isoformat(), pr=pr)
    observed = world(Server(passes=(node,), merges=work_merges(5), issues=(issue_node(),)))
    assert architecture_actions(observed, H) == () and len(observed.issues) == 1


def test_non_pr_cross_references_do_not_hide_a_closed_pass():
    node = pass_issue(closed=NOW.isoformat(), pr={})
    node["timelineItems"]["nodes"] = [{"source": {}}]
    assert filing(world(Server(passes=(node,), merges=work_merges(5))))


def test_empty_pr_head_is_unknown_completion():
    node = pass_issue(closed=NOW.isoformat(), pr={**merge(), "headRefName": ""})
    assert architecture_actions(world(Server(passes=(node,), merges=work_merges(5))), H) == ()


def test_unreadable_pass_pr_is_not_resolved_again_and_other_work_survives():
    node = {**pass_issue(pr={**merge(), "number": None}), "labels": labels("ai-ready"),
            "labeled": labeled(("ai-ready", NOW.isoformat()))}
    observed = world(Server(passes=(node,), issues=(node, issue_node())))
    assert architecture_actions(observed, H) == ()
    assert derive(observed, Local(now=NOW), H) == {issue_key(5): Dispatch(AgentKind.STEWARD)}


def test_optional_graphql_error_does_not_lose_ordinary_work():
    class ErrorServer(Server):
        def __call__(self, argv, token):
            result = super().__call__(argv, token)
            if any(a.startswith("query=") and "first:20)" in a for a in argv):
                return json.dumps({"data": {"search": None}, "errors": [{"message": "unreadable"}]})
            return result
    observed = world(ErrorServer(issues=(issue_node(),)))
    assert architecture_actions(observed, H) == () and len(observed.issues) == 1


def test_fix_issue_reads_keep_bodies_out_of_the_tick_payload():
    server = Server(issues=(issue_node(),))
    world(server)
    assert any("id number title state author{__typename login}" in query for query in server.graphql_queries)
    assert all("number title body state" not in query for query in server.graphql_queries)


def test_tech_debt_label_survives_github_parsing():
    node = issue_node(labels=labels("tech-debt"))
    assert Label.TECH_DEBT in world(Server(issues=(node,))).issues[0].labels
