"""The fix role's one-query observation (slice 5b): parsers and the bounded
world build, against GitHub GraphQL answer shapes."""

from __future__ import annotations

import json
import re

import pytest

from issue_agent.adapters.github import (
    FixObservationIncomplete,
    GitHubGateway,
    parse_fix_issue,
    parse_fix_pr,
    parse_fix_refs,
    parse_merged_harness_prs,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.world import Branch, Push


def labels(*names: str) -> dict:
    return {"totalCount": len(names), "nodes": [{"name": n} for n in names]}


def issue_node(number=5, **kw) -> dict:
    node = {"id": f"I{number}", "number": number, "title": "Null token", "state": "OPEN",
            "author": {"login": "alice"}, "labels": labels("ai-ready"),
            "comments": {"totalCount": 0, "pageInfo": {"hasPreviousPage": False}, "nodes": []},
            "timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": []}}
    node.update(kw)
    return node


def pr_node(number=100, head="h1", **kw) -> dict:
    node = {"id": f"P{number}", "number": number, "state": "OPEN", "headRefName": "fix/5-null-token",
            "headRefOid": head, "baseRefName": "dev", "baseRefOid": "b1", "isDraft": False, "title": "Fix",
            "author": {"login": "fix-bot"}, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "labels": labels(),
            "commits": {"nodes": [{"commit": {"oid": head, "committedDate": "2026-10-01T00:00:00Z",
                                              "author": {"user": {"login": "fix-bot"}}}}]}}
    node.update(kw)
    return node


def ref(name: str, *prs: dict, committed: str = "2026-10-01T00:00:00Z", ahead: int | None = 1) -> dict:
    """A `fix/` ref node: its newest PR (if any), head commit date, and commits ahead of base."""
    return {"name": name, "associatedPullRequests": {"totalCount": len(prs), "nodes": list(prs[-1:])},
            "target": {"committedDate": committed}, "compare": None if ahead is None else {"behindBy": ahead}}


def assoc(state: str, closed: str | None = None) -> dict:
    return {"state": state, "closedAt": closed}


def xref(number, state="MERGED", repo="o/r", head="fix/5-x") -> dict:
    return {"source": {"number": number, "state": state, "headRefName": head, "repository": {"nameWithOwner": repo}}}


# ── parsers ──────────────────────────────────────────────────────────────────

def test_an_issue_node_parses_with_its_comments_and_labels():
    comments = [{"author": {"login": "bob"}, "body": "any update?", "createdAt": "2026-10-01T00:00:00Z"}]
    view = parse_fix_issue(issue_node(), comments)
    assert (view.number, view.state, view.author) == (5, "open", "alice")
    assert view.labels == frozenset({Label.AI_READY})
    assert [c.body for c in view.comments] == ["any update?"]


@pytest.mark.parametrize("node", [
    None, [], issue_node(number=0), issue_node(number="5"), issue_node(number=True), issue_node(state="DONE"),
    issue_node(labels={"totalCount": 101, "nodes": [{"name": "ai-ready"}]}),  # labels past one page
    issue_node(labels=None),
])
def test_a_malformed_or_partial_issue_node_is_incomplete_not_empty(node):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_issue(node, [])


def test_a_pr_node_records_its_head_push_and_files():
    view = parse_fix_pr(pr_node(), [], [], [{"path": ".github/workflows/ci.yml"}, {"path": "src/a.py"}])
    assert (view.number, view.head_ref_oid, view.merge_state) == (100, "h1", "CLEAN")
    assert view.pushes == (Push("fix-bot", "2026-10-01T00:00:00Z"),)
    assert view.changed_files == frozenset({".github/workflows/ci.yml", "src/a.py"})


def test_an_unattributed_head_commit_is_a_push_by_no_login():
    node = pr_node(commits={"nodes": [{"commit": {"oid": "h1", "committedDate": "2026-10-01T00:00:00Z",
                                                  "author": {"user": None}}}]})
    assert parse_fix_pr(node, [], [], []).pushes == (Push("", "2026-10-01T00:00:00Z"),)


@pytest.mark.parametrize("node", [
    pr_node(number=-1), pr_node(headRefOid=None), pr_node(headRefName=7),
    pr_node(commits={"nodes": [{"commit": {"oid": "other"}}]}),  # head commit disagrees with head
    pr_node(commits=None),
])
def test_a_malformed_pr_node_is_incomplete(node):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_pr(node, [], [], [])


@pytest.mark.parametrize(("node", "owed"), [
    (ref("5-x"), True),                                                           # never had a PR
    (ref("5-x", ahead=0), False),                                                 # nothing ahead of base (P1-9)
    (ref("5-x", assoc("OPEN")), False),
    (ref("5-x", assoc("MERGED", "2026-09-01T00:00:00Z")), False),
    (ref("5-x", assoc("CLOSED", "2026-09-01T00:00:00Z")), True),                  # pushed after a human closed it (P1-8)
    (ref("5-x", assoc("CLOSED", "2026-10-02T00:00:00Z")), False),                 # closed after its last push
    (ref("5-x", assoc("CLOSED", None)), False),
    (ref("5-x", assoc("CLOSED", "2026-09-01T00:00:00Z"), committed=""), False),   # untimed push proves nothing
])
def test_a_branch_is_owed_a_pr_only_when_ahead_of_base_and_without_a_live_or_newer_pr(node, owed):
    expected = (Branch("fix/5-x", node["target"]["committedDate"]),) if owed else ()
    assert parse_fix_refs([node]) == (expected, {})


@pytest.mark.parametrize("bad", [
    {**ref("5-x"), "associatedPullRequests": {"totalCount": "0", "nodes": []}},
    {**ref("5-x"), "associatedPullRequests": {"totalCount": 1, "nodes": []}},      # a hidden or deleted PR
    {**ref("5-x"), "associatedPullRequests": {"totalCount": 1, "nodes": [{"state": "DRAFT"}]}},
    ref("5-x", ahead=None),                                                         # no comparison (missing base)
    {**ref("5-x"), "compare": {"behindBy": "2"}},
    {**ref("5-x"), "compare": []},
])
def test_an_unjudgeable_branch_makes_only_its_issue_unknown(bad):
    owed, unknown = parse_fix_refs([bad, ref("6-y")])
    assert owed == (Branch("fix/6-y", "2026-10-01T00:00:00Z"),) and set(unknown) == {5}


@pytest.mark.parametrize("bad", [[None], [{"name": 5}]])
def test_a_malformed_ref_list_is_incomplete(bad):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_refs(bad)


def test_an_issue_whose_branch_cannot_be_judged_waits_out_of_the_world():
    runner = GraphQL({"issues": page([issue_node(5), issue_node(6)]), "pullRequests": page([]),
                      "refs": page([ref("5-x", ahead=None), ref("6-y")])})
    world = GitHubGateway("o/r", runner=runner).build_fix_world("dev")
    assert [i.number for i in world.issues] == [6]
    assert world.branches == (Branch("fix/6-y", "2026-10-01T00:00:00Z"),)


def test_the_pr_lookup_for_a_branch_considers_open_prs_only():
    # P1-8 depends on it: a closed PR found here would make act skip the open forever.
    calls = []

    def runner(argv, _token):
        calls.append(argv)
        return json.dumps([{"number": 101, "headRefName": "fix/5-x"}, {"number": 7, "headRefName": "fix/5-xy"}])

    assert GitHubGateway("o/r", runner=runner).pull_request_for_branch("fix/5-x") == 101
    (argv,) = calls
    assert argv[argv.index("--state") + 1] == "open" and argv[argv.index("--head") + 1] == "fix/5-x"


def test_merged_harness_prs_come_only_from_this_repository_and_fix_branches():
    items = [xref(10, repo="O/R"), xref(11, state="CLOSED"), xref(12, repo="fork/r"), xref(13, head="feat/5-x"),
             {"source": None}, None]
    assert [(p.number, p.state) for p in parse_merged_harness_prs(items, "o/r")] == [(10, "MERGED")]


# ── the bounded world build ──────────────────────────────────────────────────

def page(nodes, more=False, cursor=None) -> dict:
    return {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}


class GraphQL:
    """Answers each `gh api graphql` call from a queue of repository pages."""

    def __init__(self, *repositories):
        self.answers = [json.dumps({"data": {"repository": r}}) for r in repositories]
        self.queries: list[str] = []

    def __call__(self, argv, _token):
        assert argv[:3] == ["gh", "api", "graphql"], argv
        self.queries.append(next(a for a in argv if a.startswith("query=")))
        return self.answers.pop(0)


def test_the_world_carries_issues_merged_prs_and_unpublished_branches():
    merged = xref(9, head="fix/4-old")
    runner = GraphQL({
        "issues": page([issue_node(5), issue_node(4, timelineItems={"pageInfo": {}, "nodes": [merged]})]),
        "pullRequests": page([pr_node(headRefName="feature/x")]),  # not a harness PR: never fetched further
        "refs": page([ref("5-null-token")]),
    })
    world = GitHubGateway("o/r", runner=runner).build_fix_world("dev")
    assert [i.number for i in world.issues] == [5, 4]
    assert [(p.number, p.state) for p in world.prs] == [(9, "MERGED")]
    assert world.branches == (Branch("fix/5-null-token", "2026-10-01T00:00:00Z"),)
    assert len(runner.queries) == 1  # one query for the whole observation


def test_an_issue_with_unreadable_comments_is_skipped_not_misread():
    broken = issue_node(6, comments={"totalCount": 3, "pageInfo": {"hasPreviousPage": False}, "nodes": []})
    runner = GraphQL({"issues": page([issue_node(5), broken]), "pullRequests": page([]), "refs": page([])})
    assert [i.number for i in GitHubGateway("o/r", runner=runner).build_fix_world("dev").issues] == [5]


def test_a_top_level_connection_past_its_page_bound_skips_the_tick():
    first = {"issues": page([], more=True, cursor="c0"), "pullRequests": page([]), "refs": page([])}
    more = [{"issues": page([], more=True, cursor=f"c{i}")} for i in range(1, 12)]
    with pytest.raises(FixObservationIncomplete, match="issues exceed"):
        GitHubGateway("o/r", runner=GraphQL(first, *more)).build_fix_world("dev")


@pytest.mark.parametrize("answer", [
    {"errors": [{"message": "denied"}]}, {"data": None}, {"data": {"repository": None}},
    {"data": {"repository": {"issues": page([]), "pullRequests": page([])}}},  # refs absent
])
def test_an_errored_or_partial_answer_skips_the_tick(answer):
    gateway = GitHubGateway("o/r", runner=lambda argv, token: json.dumps(answer))
    with pytest.raises(FixObservationIncomplete):
        gateway.build_fix_world("dev")


def test_an_unreadable_open_pr_still_shadows_its_issue_with_no_head():
    runner = GraphQL({"issues": page([issue_node(5)]),
                      "pullRequests": page([pr_node(labels={"totalCount": 2, "nodes": []})]),
                      "refs": page([])})
    world = GitHubGateway("o/r", runner=runner).build_fix_world("dev")
    assert [(p.number, p.head_ref_oid, p.content_known) for p in world.prs] == [(100, "", False)]


# ── paged reads: the query text GitHub validates (P1-5) ──────────────────────

def _back(nodes, cursor=None) -> dict:
    return {"totalCount": None, "pageInfo": {"hasPreviousPage": cursor is not None, "startCursor": cursor},
            "nodes": nodes}


def _fwd(nodes, cursor=None) -> dict:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


class PagedGitHub:
    """Answers the first repository read with one more page behind every
    connection, then each follow-up read from the query it is sent. Records
    every GraphQL query with the variables passed to it."""

    def __init__(self, timeline_pages: int = 1):
        self.calls: list[tuple[str, dict]] = []
        self.timeline_pages = timeline_pages

    def first(self) -> dict:
        issue = issue_node(5, comments=_back([], "ic"), timelineItems=_back([], "it0"))
        harness = pr_node(comments=_back([], "pc"), reviews=_back([], "pr"), files={"totalCount": None, **_fwd([], "pf")})
        return {"issues": page([issue]), "pullRequests": page([harness]), "refs": _fwd([], "r1")}

    def node(self, query: str, variables: dict) -> dict:
        name = re.search(r"on \w+\{(\w+)\(", query).group(1)
        if name == "timelineItems":
            step = int(variables["cursor"].removeprefix("it")) + 1
            more = f"it{step}" if step < self.timeline_pages else None
            nodes = [xref(9, head="fix/5-old")] if more is None else [xref(n, state="CLOSED") for n in range(100)]
            return {name: _back(nodes, more)}
        if name == "files":
            return {name: _fwd([{"path": "src/a.py"}])}
        if name == "reviews":
            return {name: _back([{"author": {"login": "review-bot"}, "state": "APPROVED",
                                   "submittedAt": "2026-10-01T00:00:00Z", "body": "", "commit": {"oid": "h1"}}])}
        return {name: _back([{"author": {"login": "alice"}, "body": "older", "createdAt": "2026-09-01T00:00:00Z"}])}

    def __call__(self, argv, _token):
        if argv[:3] != ["gh", "api", "graphql"]:
            return "[]"  # REST reads (required checks): nothing configured
        pairs = [argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f"]
        variables = {k: v for k, v in pairs}
        query = variables.pop("query")
        self.calls.append((query, variables))
        if query.startswith("query($id"):
            return json.dumps({"data": {"node": self.node(query, variables)}})
        if "refs(" in query and "after:$after" in query:
            return json.dumps({"data": {"repository": {"refs": _fwd([ref("5-x")])}}})
        return json.dumps({"data": {"repository": self.first()}})


def _declared(query: str) -> set[str]:
    header = query[query.index("(") + 1:query.index(")")]
    return set(re.findall(r"\$(\w+):", header))


def _used(query: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", query[query.index(")") + 1:]))


def test_every_paged_query_declares_exactly_the_variables_it_uses_and_is_sent_them():
    runner = PagedGitHub()
    world = GitHubGateway("o/r", runner=runner).build_fix_world("dev")
    assert len(runner.calls) == 7  # first read, refs page 2, 5 nested pages
    for query, variables in runner.calls:
        assert _declared(query) == _used(query) == set(variables), query


@pytest.mark.parametrize(("connection", "owners"), [
    ("timelineItems", ["Issue"]), ("files", ["PullRequest"]), ("reviews", ["PullRequest"]),
    ("comments", ["Issue", "PullRequest"]),
])
def test_a_nested_page_selects_its_connection_only_on_the_owners_type(connection, owners):
    runner = PagedGitHub()
    GitHubGateway("o/r", runner=runner).build_fix_world("dev")
    pages = [q for q, _ in runner.calls if q.startswith("query($id") and f"{{{connection}(" in q]
    head = re.compile(r"query\(\$id:ID!,\$cursor:String!\)\{node\(id:\$id\)\{\.\.\. on (\w+)\{" + connection + r"\(")
    assert sorted(m.group(1) for q in pages if (m := head.match(q))) == owners
    assert len(pages) == len(owners)


def test_paged_connections_are_read_in_full():
    world = GitHubGateway("o/r", runner=PagedGitHub()).build_fix_world("dev")
    (issue,) = world.issues
    harness = next(p for p in world.prs if p.state == "OPEN")
    assert [c.body for c in issue.comments] == ["older"]
    assert harness.content_known and harness.changed_files == frozenset({"src/a.py"})
    assert [r.state for r in harness.reviews] == ["APPROVED"] and len(harness.comments) == 1
    assert world.branches == (Branch("fix/5-x", "2026-10-01T00:00:00Z"),)


def test_a_merged_pr_on_a_later_cross_reference_page_still_retires_its_issue():
    world = GitHubGateway("o/r", runner=PagedGitHub(timeline_pages=3)).build_fix_world("dev")
    assert [(p.number, p.state) for p in world.prs if p.state == "MERGED"] == [(9, "MERGED")]


def test_cross_references_past_their_bound_skip_the_issue_rather_than_read_as_unmerged():
    world = GitHubGateway("o/r", runner=PagedGitHub(timeline_pages=50)).build_fix_world("dev")
    assert world.issues == () and not any(p.state == "MERGED" for p in world.prs)
