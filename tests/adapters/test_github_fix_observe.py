"""The fix role's observation (slice 5b, #425): parsers and the bounded world
build, one request per connection page, against GitHub GraphQL answer shapes."""

from __future__ import annotations

import json
import re

import pytest

from issue_agent.adapters import github as gh
from issue_agent.adapters.github import (
    FixObservationIncomplete,
    GhUnavailable,
    GitHubGateway,
    parse_fix_issue,
    parse_fix_pr,
    parse_fix_refs,
    BlockerLinkFacts,
    parse_blocker_link,
    parse_blockers,
    parse_merged_harness_prs,
)
from issue_agent.domain.world import BlockerRef, Branch, Push


def labels(*names: str) -> dict:
    return {"totalCount": len(names), "nodes": [{"name": n} for n in names]}


def issue_node(number=5, **kw) -> dict:
    node = {
        "id": f"I{number}",
        "number": number,
        "title": "Null token",
        "state": "OPEN",
        "author": {"login": "alice"},
        "labels": labels("ai-ready"),
        "comments": {"totalCount": 0, "pageInfo": {"hasPreviousPage": False}, "nodes": []},
        "timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": []},
    }
    node.update(kw)
    return node


def pr_node(number=100, head="h1", **kw) -> dict:
    node = {
        "id": f"P{number}",
        "number": number,
        "state": "OPEN",
        "headRefName": "fix/5-null-token",
        "headRefOid": head,
        "baseRefName": "dev",
        "baseRefOid": "b1",
        "isDraft": False,
        "title": "Fix",
        "author": {"login": "fix-bot"},
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "labels": labels(),
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "committedDate": "2026-10-01T00:00:00Z",
                        "author": {"user": {"login": "fix-bot"}},
                    }
                }
            ]
        },
    }
    node.update(kw)
    return node


def ref(name: str, *prs: dict, committed: str = "2026-10-01T00:00:00Z", ahead: int | None = 1) -> dict:
    """A `fix/` ref node, named as a `refs/heads/` listing returns it: its newest PR
    (if any), head commit date, and commits ahead of base."""
    return {
        "name": f"fix/{name}",
        "associatedPullRequests": {"totalCount": len(prs), "nodes": list(prs[-1:])},
        "target": {"committedDate": committed},
        "compare": None if ahead is None else {"behindBy": ahead},
    }


def assoc(state: str, closed: str | None = None) -> dict:
    return {"state": state, "closedAt": closed}


def xref(number, state="MERGED", repo="o/r", head="fix/5-x") -> dict:
    return {"source": {"number": number, "state": state, "headRefName": head, "repository": {"nameWithOwner": repo}}}


# ── parsers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "node",
    [
        None,
        [],
        issue_node(number=0),
        issue_node(number="5"),
        issue_node(number=True),
        issue_node(state="DONE"),
        issue_node(labels={"totalCount": 101, "nodes": [{"name": "ai-ready"}]}),  # labels past one page
        issue_node(labels=None),
    ],
)
def test_a_malformed_or_partial_issue_node_is_incomplete_not_empty(node):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_issue(node, [])


def labeled(*events: tuple[str, str], earlier: bool = False) -> dict:
    return {
        "pageInfo": {"hasPreviousPage": earlier},
        "nodes": [{"createdAt": t, "label": {"name": n}} for n, t in events],
    }


def test_a_labelled_item_carries_when_needs_human_was_last_applied():
    node = issue_node(
        labels=labels("ai-ready", "needs-human"),
        labeled=labeled(
            ("needs-human", "2026-10-01T00:00:00Z"),
            ("ai-ready", "2026-10-01T01:00:00Z"),
            ("needs-human", "2026-10-01T02:00:00Z"),
        ),
    )
    assert parse_fix_issue(node, []).needs_human_labeled_at == "2026-10-01T02:00:00Z"
    pr = parse_fix_pr(
        pr_node(labels=labels("needs-human"), labeled=labeled(("needs-human", "2026-10-02T00:00:00Z"))), [], [], []
    )
    assert pr.needs_human_labeled_at == "2026-10-02T00:00:00Z"
    # An unlabelled item does not need (or read) the events.
    assert parse_fix_issue(issue_node(), []).needs_human_labeled_at == ""


def test_a_ready_issue_carries_when_ai_ready_was_last_applied():
    node = issue_node(labeled=labeled(("ai-ready", "2026-10-01T00:00:00Z"), ("ai-ready", "2026-10-03T00:00:00Z")))
    assert parse_fix_issue(node, []).ai_ready_labeled_at == "2026-10-03T00:00:00Z"
    unready = issue_node(labels=labels(), labeled=labeled(("ai-ready", "2026-10-01T00:00:00Z")))
    assert parse_fix_issue(unready, []).ai_ready_labeled_at == ""


@pytest.mark.parametrize("events", [None, {"nodes": "x"}, labeled(earlier=True), labeled(("bug", "2026-10-01T00:00:00Z"))])
def test_an_unknown_ready_time_is_blank_and_never_fails_the_observation(events):
    # Display only: unlike needs-human's, a missing ai-ready event cannot mis-release anything.
    node = issue_node(labeled=events) if events is not None else issue_node()
    assert parse_fix_issue(node, []).ai_ready_labeled_at == ""


@pytest.mark.parametrize("events", [None, {"nodes": "x"}, labeled(("ai-ready", "2026-10-01T00:00:00Z"), earlier=True)])
def test_an_unreadable_or_truncated_label_history_is_incomplete_for_a_labelled_item(events):
    node = issue_node(labels=labels("ai-ready", "needs-human"), labeled=events)
    with pytest.raises(FixObservationIncomplete):
        parse_fix_issue(node, [])


def test_a_pr_node_records_its_head_push_and_files():
    view = parse_fix_pr(pr_node(), [], [], [{"path": ".github/workflows/ci.yml"}, {"path": "src/a.py"}])
    assert (view.number, view.head_ref_oid, view.merge_state) == (100, "h1", "CLEAN")
    assert view.pushes == (Push("fix-bot", "2026-10-01T00:00:00Z"),)
    assert view.changed_files == frozenset({".github/workflows/ci.yml", "src/a.py"})


def test_an_unattributed_head_commit_is_a_push_by_no_login():
    node = pr_node(
        commits={
            "nodes": [{"commit": {"oid": "h1", "committedDate": "2026-10-01T00:00:00Z", "author": {"user": None}}}]
        }
    )
    assert parse_fix_pr(node, [], [], []).pushes == (Push("", "2026-10-01T00:00:00Z"),)


@pytest.mark.parametrize(
    "node",
    [
        pr_node(number=-1),
        pr_node(headRefOid=None),
        pr_node(headRefName=7),
        pr_node(commits={"nodes": [{"commit": {"oid": "other"}}]}),  # head commit disagrees with head
        pr_node(commits=None),
    ],
)
def test_a_malformed_pr_node_is_incomplete(node):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_pr(node, [], [], [])


@pytest.mark.parametrize(
    ("node", "owed"),
    [
        (ref("5-x"), True),  # never had a PR
        (ref("5-x", ahead=0), False),  # nothing ahead of base (P1-9)
        (ref("5-x", assoc("OPEN")), False),
        (ref("5-x", assoc("MERGED", "2026-09-01T00:00:00Z")), False),
        (ref("5-x", assoc("CLOSED", "2026-09-01T00:00:00Z")), True),  # pushed after a human closed it (P1-8)
        (ref("5-x", assoc("CLOSED", "2026-10-02T00:00:00Z")), False),  # closed after its last push
        (ref("5-x", assoc("CLOSED", None)), False),
        (ref("5-x", assoc("CLOSED", "2026-09-01T00:00:00Z"), committed=""), False),  # untimed push proves nothing
    ],
)
def test_a_branch_is_owed_a_pr_only_when_ahead_of_base_and_without_a_live_or_newer_pr(node, owed):
    expected = (Branch("fix/5-x", node["target"]["committedDate"]),) if owed else ()
    assert parse_fix_refs([node]) == (expected, {})


@pytest.mark.parametrize(
    "bad",
    [
        {**ref("5-x"), "associatedPullRequests": {"totalCount": "0", "nodes": []}},
        {**ref("5-x"), "associatedPullRequests": {"totalCount": 1, "nodes": []}},  # a hidden or deleted PR
        {**ref("5-x"), "associatedPullRequests": {"totalCount": 1, "nodes": [{"state": "DRAFT"}]}},
        ref("5-x", ahead=None),  # no comparison (missing base)
        {**ref("5-x"), "compare": {"behindBy": "2"}},
        {**ref("5-x"), "compare": []},
    ],
)
def test_an_unjudgeable_branch_makes_only_its_issue_unknown(bad):
    owed, unknown = parse_fix_refs([bad, ref("6-y")])
    assert owed == (Branch("fix/6-y", "2026-10-01T00:00:00Z"),) and set(unknown) == {5}


def test_a_name_that_only_contains_the_prefix_is_not_a_harness_branch():
    # `query:"fix/"` matches anywhere in the name.
    assert parse_fix_refs([{**ref("5-x"), "name": "hotfix/5-x"}, {**ref("6-y"), "name": "feat/fix/6-y"}]) == ((), {})


def test_harness_refs_are_listed_under_refs_heads():
    # 2026-10-13: under a `refs/heads/fix/` prefix GitHub fails `compare`, so
    # every fix-role read failed. Live-verified shape; GitHub cannot be faked here.
    assert 'refPrefix:"refs/heads/",query:"fix/"' in gh._FIX_REFS


@pytest.mark.parametrize("bad", [[None], [{"name": 5}]])
def test_a_malformed_ref_list_is_incomplete(bad):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_refs(bad)


def test_an_issue_whose_branch_cannot_be_judged_waits_out_of_the_world():
    runner = GraphQL(
        {
            "issues": page([issue_node(5), issue_node(6)]),
            "pullRequests": page([]),
            "refs": page([ref("5-x", ahead=None), ref("6-y")]),
        }
    )
    world = GitHubGateway("o/r", runner=runner).build_world("dev")
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
    items = [
        xref(10, repo="O/R"),
        xref(11, state="CLOSED"),
        xref(12, repo="fork/r"),
        xref(13, head="feat/5-x"),
        {"source": None},
        None,
    ]
    assert [(p.number, p.state) for p in parse_merged_harness_prs(items, "o/r")] == [(10, "MERGED")]


# ── the bounded world build ──────────────────────────────────────────────────


def page(nodes, more=False, cursor=None) -> dict:
    return {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}


_ASKED = re.compile(r"\b(issues|pullRequests|refs)\(|\b(baseTip):")


def asked(query: str) -> set[str]:
    """The top-level connections a repository query asks for."""
    return {a or b for a, b in _ASKED.findall(query)}


class GraphQL:
    """Answers each repository read from the queued pages of the connections
    it asks for, and each node read from the node queue."""

    def __init__(self, *repositories):
        self.pages: dict[str, list] = {}
        for repository in repositories:
            for key, value in repository.items():
                self.pages.setdefault(key, []).append(value)
        self.nodes: list = []
        self.queries: list[str] = []

    def __call__(self, argv, _token):
        assert argv[:3] == ["gh", "api", "graphql"], argv
        query = next(a for a in argv if a.startswith("query="))
        self.queries.append(query)
        if query.startswith("query=query($id"):
            return json.dumps({"data": {"node": self.nodes.pop(0)}})
        return json.dumps({"data": {"repository": {k: self.pages[k].pop(0) for k in asked(query) if self.pages.get(k)}}})


def test_the_world_carries_issues_merged_prs_and_unpublished_branches():
    merged = xref(9, head="fix/4-old")
    runner = GraphQL(
        {
            "issues": page([issue_node(5), issue_node(4, timelineItems={"pageInfo": {"hasPreviousPage": False}, "nodes": [merged]})]),
            "pullRequests": page([pr_node(headRefName="feature/x")]),  # not a harness PR: never fetched further
            "refs": page([ref("5-null-token")]),
        }
    )
    world = GitHubGateway("o/r", runner=runner).build_world("dev")
    assert [i.number for i in world.issues] == [5, 4]
    assert [(p.number, p.state) for p in world.prs] == [(9, "MERGED")]
    assert world.branches == (Branch("fix/5-null-token", "2026-10-01T00:00:00Z"),)


def test_an_issue_with_unreadable_comments_is_skipped_not_misread():
    broken = issue_node(6, comments={"totalCount": 3, "pageInfo": {"hasPreviousPage": False}, "nodes": []})
    runner = GraphQL({"issues": page([issue_node(5), broken]), "pullRequests": page([]), "refs": page([])})
    assert [i.number for i in GitHubGateway("o/r", runner=runner).build_world("dev").issues] == [5]


def test_a_top_level_connection_past_its_page_bound_skips_the_tick():
    endless = [{"issues": page([], more=True, cursor=f"c{i}")} for i in range(gh._FIX_MAX_PAGES["issues"] + 5)]
    runner = GraphQL(*endless)
    with pytest.raises(FixObservationIncomplete, match="issues exceed"):
        GitHubGateway("o/r", runner=runner).build_world("dev")
    assert len(runner.queries) == gh._FIX_MAX_PAGES["issues"]  # bounded: no request past the ceiling


@pytest.mark.parametrize(
    "answer",
    [
        {"errors": [{"message": "denied"}]},
        {"data": None},
        {"data": {"repository": None}},
        {"data": {"repository": {"issues": page([]), "pullRequests": page([])}}},  # refs absent
    ],
)
def test_an_errored_or_partial_answer_skips_the_tick(answer):
    gateway = GitHubGateway("o/r", runner=lambda argv, token: json.dumps(answer))
    with pytest.raises(FixObservationIncomplete):
        gateway.build_world("dev")


def test_an_unreadable_open_pr_still_shadows_its_issue_with_no_head():
    runner = GraphQL(
        {
            "issues": page([issue_node(5)]),
            "pullRequests": page([pr_node(labels={"totalCount": 2, "nodes": []})]),
            "refs": page([]),
        }
    )
    world = GitHubGateway("o/r", runner=runner).build_world("dev")
    assert [(p.number, p.head_ref_oid, p.content_known) for p in world.prs] == [(100, "", False)]


# ── #425: no single request is large ─────────────────────────────────────────


class ServedGitHub:
    """Serves whole connections the way GitHub pages them: each repository
    read gets `first:N` items after its `$after` cursor (an index here).
    `fail` maps a (connection, page number) to the answer that page gets."""

    def __init__(self, issues=(), prs=(), refs=(), tip=None, fail=None):
        self.items = {"issues": list(issues), "pullRequests": list(prs), "refs": list(refs)}
        self.tip, self.fail = tip, fail or {}
        self.queries: list[tuple[str, dict]] = []

    def __call__(self, argv, _token):
        assert argv[:3] == ["gh", "api", "graphql"], argv
        pairs = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")
        query = pairs.pop("query")
        self.queries.append((query, pairs))
        (name,) = asked(query)
        number = len(self.asking(name))
        if (name, number) in self.fail:
            failure = self.fail[(name, number)]
            if isinstance(failure, BaseException):
                raise failure
            return json.dumps(failure)
        if name == "baseTip":
            return json.dumps({"data": {"repository": {"baseTip": self.tip}}})
        start = int(pairs.get("after", "0"))
        size = int(re.search(name + r"\([^)]*first:(\d+)", query).group(1))
        chunk, end = self.items[name][start : start + size], start + size
        more = end < len(self.items[name])
        return json.dumps({"data": {"repository": {name: page(chunk, more, str(end) if more else None)}}})

    def asking(self, name: str) -> list[str]:
        return [q for q, _ in self.queries if asked(q) == {name}]


def test_425_each_request_carries_one_connection_and_a_small_page():
    served = ServedGitHub(issues=[issue_node(n) for n in range(1, 4)], refs=[ref(f"{n}-x") for n in range(1, 4)])
    GitHubGateway("o/r", runner=served).build_world("dev")
    assert sorted(name for q, _ in served.queries for name in asked(q)) == ["baseTip", "issues", "pullRequests", "refs"]
    for query, _ in served.queries:
        assert len(asked(query)) == 1, query
        assert all(int(n) <= 50 for n in re.findall(r"(?:issues|pullRequests|refs)\([^)]*first:(\d+)", query))


def test_425_a_huge_repository_is_read_in_full_across_many_small_requests():
    issues = [issue_node(n) for n in range(1, 1001)]
    refs = [ref(f"{n}-x") for n in range(1, 1001)]
    prs = [pr_node(n, headRefName=f"feature/{n}") for n in range(1, 1001)]  # human PRs: listed, never acted on
    served = ServedGitHub(issues=issues, prs=prs, refs=refs)
    world = GitHubGateway("o/r", runner=served).build_world("dev")
    assert [i.number for i in world.issues] == list(range(1, 1001))
    assert len(world.branches) == 1000 and world.prs == ()
    assert {name: len(served.asking(name)) for name in ("issues", "pullRequests", "refs")} == gh._FIX_MAX_PAGES


@pytest.mark.parametrize("name", ["issues", "pullRequests", "refs"])
def test_425_one_item_past_the_ceiling_skips_the_tick_after_a_bounded_number_of_requests(name):
    many = {"issues": [issue_node(n) for n in range(1, 1002)],
            "pullRequests": [pr_node(n, headRefName=f"feature/{n}") for n in range(1, 1002)],
            "refs": [ref(f"{n}-x") for n in range(1, 1002)]}
    served = ServedGitHub()
    served.items[name] = many[name]
    with pytest.raises(FixObservationIncomplete, match=f"{name} exceed"):
        GitHubGateway("o/r", runner=served).build_world("dev")
    assert len(served.asking(name)) == gh._FIX_MAX_PAGES[name]


@pytest.mark.parametrize(
    "bad_page",
    [
        {"nodes": [], "pageInfo": None},  # no page info: whether more follow is unknown
        {"nodes": []},
        {"nodes": [], "pageInfo": {}},
        {"nodes": [], "pageInfo": {"hasNextPage": "false"}},
        {"nodes": [], "pageInfo": {"hasNextPage": None, "endCursor": None}},
        {"nodes": [], "pageInfo": []},
        {"nodes": None, "pageInfo": {"hasNextPage": False}},
        {"nodes": [], "pageInfo": {"hasNextPage": True}},  # more, but no cursor to fetch them
        {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": ""}},
        {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": 7}},
    ],
)
@pytest.mark.parametrize("name", ["issues", "pullRequests", "refs"])
def test_425_malformed_page_info_on_a_later_page_fails_the_read_never_truncates_it(name, bad_page):
    count = range(1, gh._FIX_PAGE_SIZE[name] + 2)  # one item onto a second page
    served = ServedGitHub(issues=[issue_node(n) for n in count], refs=[ref(f"{n}-x") for n in count],
                          prs=[pr_node(n, headRefName=f"feature/{n}") for n in count],
                          fail={(name, 2): {"data": {"repository": {name: bad_page}}}})
    with pytest.raises(FixObservationIncomplete):
        GitHubGateway("o/r", runner=served).build_world("dev")


@pytest.mark.parametrize(
    "failure",
    [
        GhUnavailable("server", "HTTP 502: Bad Gateway"),
        {"errors": [{"message": "Something went wrong while executing your query"}]},  # GitHub's timeout answer
        {"data": {"repository": None}},
    ],
)
def test_425_a_failed_later_page_skips_the_tick_rather_than_observing_the_first_page_alone(failure):
    served = ServedGitHub(issues=[issue_node(n) for n in range(1, gh._FIX_PAGE_SIZE["issues"] + 2)],
                          fail={("issues", 2): failure})
    with pytest.raises((FixObservationIncomplete, GhUnavailable)):
        GitHubGateway("o/r", runner=served).build_world("dev")
    assert len(served.queries) == 2  # no retry: the next tick is the retry


@pytest.mark.parametrize("name", ["pullRequests", "refs"])
def test_425_a_failed_connection_after_a_good_one_still_skips_the_tick(name):
    served = ServedGitHub(issues=[issue_node(5)], fail={(name, 1): GhUnavailable("server", "HTTP 504: Gateway Timeout")})
    with pytest.raises(GhUnavailable):
        GitHubGateway("o/r", runner=served).build_world("dev")


@pytest.mark.parametrize(
    "failure",
    [
        GhUnavailable("server", "HTTP 502"),
        {"errors": [{"message": "Something went wrong while executing your query"}]},  # GitHub's timeout answer
        {"data": None},
        {"data": {"repository": None}},
    ],
)
def test_425_a_failed_base_tip_request_skips_the_tick_rather_than_charging_base_reds_to_prs(failure):
    served = ServedGitHub(issues=[issue_node(5)], fail={("baseTip", 1): failure})
    with pytest.raises((FixObservationIncomplete, GhUnavailable)):
        GitHubGateway("o/r", runner=served).build_world("dev")


@pytest.mark.parametrize("info", [None, {}, {"hasPreviousPage": "no"}, {"hasPreviousPage": None}])
def test_425_cross_references_without_page_info_make_the_issue_unknown_not_unmerged(info):
    xrefs = {"nodes": [], **({} if info is None else {"pageInfo": info})}
    served = ServedGitHub(issues=[issue_node(5, timelineItems=xrefs), issue_node(6)])
    world = GitHubGateway("o/r", runner=served).build_world("dev")
    assert [i.number for i in world.issues] == [6]


@pytest.mark.parametrize("connection", ["comments", "reviews", "files"])
def test_425_a_pr_nested_connection_without_page_info_makes_its_content_unknown(connection):
    empty = {"totalCount": 0, "nodes": []}
    pr = pr_node(comments={**empty, "pageInfo": {"hasPreviousPage": False}},
                 reviews={**empty, "pageInfo": {"hasPreviousPage": False}},
                 files={**empty, "pageInfo": {"hasNextPage": False}})
    pr[connection] = empty  # GitHub's shape without its page info
    served = ServedGitHub(prs=[pr])
    world = GitHubGateway("o/r", runner=served).build_world("dev")
    assert [(p.number, p.head_ref_oid, p.content_known) for p in world.prs] == [(100, "", False)]


# ── paged reads: the query text GitHub validates (P1-5) ──────────────────────


def _back(nodes, cursor=None) -> dict:
    return {
        "totalCount": None,
        "pageInfo": {"hasPreviousPage": cursor is not None, "startCursor": cursor},
        "nodes": nodes,
    }


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
        harness = pr_node(
            comments=_back([], "pc"), reviews=_back([], "pr"), files={"totalCount": None, **_fwd([], "pf")}
        )
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
            return {
                name: _back(
                    [
                        {
                            "author": {"login": "review-bot"},
                            "state": "APPROVED",
                            "submittedAt": "2026-10-01T00:00:00Z",
                            "body": "",
                            "commit": {"oid": "h1"},
                        }
                    ]
                )
            }
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
        if "baseTip:" in query:
            return json.dumps({"data": {"repository": {"baseTip": None}}})
        if "refs(" in query and "after:$after" in query:
            return json.dumps({"data": {"repository": {"refs": _fwd([ref("5-x")])}}})
        return json.dumps({"data": {"repository": self.first()}})


def _declared(query: str) -> set[str]:
    header = query[query.index("(") + 1 : query.index(")")]
    return set(re.findall(r"\$(\w+):", header))


def _used(query: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", query[query.index(")") + 1 :]))


def test_every_paged_query_declares_exactly_the_variables_it_uses_and_is_sent_them():
    runner = PagedGitHub()
    world = GitHubGateway("o/r", runner=runner).build_world("dev")
    assert len(runner.calls) == 10  # issues, PRs, refs ×2, base tip, 5 nested pages
    for query, variables in runner.calls:
        assert _declared(query) == _used(query) == set(variables), query


def test_paged_connections_are_read_in_full():
    world = GitHubGateway("o/r", runner=PagedGitHub()).build_world("dev")
    (issue,) = world.issues
    harness = next(p for p in world.prs if p.state == "OPEN")
    assert [c.body for c in issue.comments] == ["older"]
    assert harness.content_known and harness.changed_files == frozenset({"src/a.py"})
    assert [r.state for r in harness.reviews] == ["APPROVED"] and len(harness.comments) == 1
    assert world.branches == (Branch("fix/5-x", "2026-10-01T00:00:00Z"),)


def test_a_merged_pr_on_a_later_cross_reference_page_still_retires_its_issue():
    world = GitHubGateway("o/r", runner=PagedGitHub(timeline_pages=3)).build_world("dev")
    assert [(p.number, p.state) for p in world.prs if p.state == "MERGED"] == [(9, "MERGED")]


def test_cross_references_past_their_bound_skip_the_issue_rather_than_read_as_unmerged():
    world = GitHubGateway("o/r", runner=PagedGitHub(timeline_pages=50)).build_world("dev")
    assert world.issues == () and not any(p.state == "MERGED" for p in world.prs)


# ── #313: the base tip's checks ─────────────────────────────────────────────


def run_node(name, conclusion="FAILURE", status="COMPLETED", app=15368, n=1) -> dict:
    return {
        "__typename": "CheckRun",
        "id": f"CR{name}{n}",
        "databaseId": n,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": "2026-10-12T00:00:00Z",
        "detailsUrl": f"https://github.com/o/r/actions/runs/{n}/job/1",
        "checkSuite": {"app": {"databaseId": app}},
    }


def status_node(context, state="FAILURE") -> dict:
    return {
        "__typename": "StatusContext",
        "id": f"SC{context}",
        "createdAt": "2026-10-12T00:00:00Z",
        "context": context,
        "state": state,
        "targetUrl": None,
    }


def base_tip(nodes, name="dev", more=None, total=None) -> dict:
    contexts = {
        "totalCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": more is not None, "endCursor": more},
        "nodes": nodes,
    }
    return {"name": name, "target": {"oid": "b9", "statusCheckRollup": {"id": "SCR1", "contexts": contexts}}}


def world_with(tip, *more):
    first = {"issues": page([]), "pullRequests": page([]), "refs": page([]), "baseTip": tip}
    runner = GraphQL(first)
    runner.nodes += list(more)
    return GitHubGateway("o/r", runner=runner).build_world("dev"), runner


def test_313_the_base_tips_checks_are_read_with_the_world():
    world, runner = world_with(base_tip([run_node("frontend"), run_node("rust", "SUCCESS", n=2), status_node("ci/x")]))
    assert world.base is not None and (world.base.ref, world.base.oid) == ("dev", "b9")
    assert [(c.name, c.bucket, c.kind, c.app_id) for c in world.base.reports] == [
        ("frontend", "fail", "check", 15368),
        ("rust", "pass", "check", 15368),
        ("ci/x", "fail", "status", None),
    ]
    assert any("baseTip:ref(qualifiedName:$base)" in q for q in runner.queries)


def test_313_a_base_tip_with_no_checks_is_an_empty_read_not_an_unread_one():
    world, _ = world_with({"name": "dev", "target": {"oid": "b9", "statusCheckRollup": None}})
    assert world.base is not None and world.base.reports == ()


@pytest.mark.parametrize(
    "tip",
    [
        None,  # the base ref does not exist
        "x",
        {"name": "main", "target": {"oid": "b9", "statusCheckRollup": None}},  # not the configured base
        {"name": "dev", "target": None},
        {"name": "dev", "target": {"statusCheckRollup": None}},  # no commit id
        base_tip([run_node("frontend", status="EXPLODED")]),  # unknown status
        base_tip([{"__typename": "CheckRun", "name": "x"}]),  # partial node
        base_tip([None]),
        base_tip([run_node("a")], total=2),  # count disagrees with the nodes
        {"name": "dev", "target": {"oid": "b9", "statusCheckRollup": {"id": "SCR1", "contexts": None}}},
    ],
)
def test_313_an_unreadable_base_tip_is_absent_and_the_tick_goes_on(tip):
    world, _ = world_with(tip)
    assert world.base is None


def test_313_a_base_tip_past_one_page_is_read_in_full():
    more = {"contexts": {"totalCount": 101, "pageInfo": {"hasNextPage": False, "endCursor": None},
                         "nodes": [run_node("last", n=101)]}}
    world, runner = world_with(base_tip([run_node(f"j{i}", n=i + 1) for i in range(100)], more="c1", total=101), more)
    assert world.base is not None and len(world.base.reports) == 101
    assert any("on StatusCheckRollup{contexts(first:100,after:$cursor)" in q for q in runner.queries)


def test_313_a_huge_base_check_list_past_its_bound_is_absent():
    big = [run_node(f"j{i}", n=i + 1) for i in range(100)]
    pages = [{"contexts": {"totalCount": 5000, "pageInfo": {"hasNextPage": True, "endCursor": f"c{k}"},
                           "nodes": [run_node(f"p{k}-{i}", n=1000 * k + i) for i in range(100)]}} for k in range(2, 12)]
    world, _ = world_with(base_tip(big, more="c1", total=5000), *pages)
    assert world.base is None


# ── "blocked by" (GitHub issue dependencies) ─────────────────────────────────


def blocker(number, state="OPEN", repo="o/r") -> dict:
    """One `blockedBy` node as GitHub's GraphQL returns it (schema checked live
    on xnmp/tauri-explorer, 2026-10-14: Issue.blockedBy is an IssueConnection)."""
    return {"number": number, "state": state, "repository": {"nameWithOwner": repo}}


def blocked_by(*nodes, total=None) -> dict:
    return {"totalCount": len(nodes) if total is None else total, "nodes": list(nodes)}


def test_the_issue_query_reads_blockers_in_the_same_round_trip():
    assert "blockedBy(first:100){totalCount nodes{number state repository{nameWithOwner}}}" in gh._FIX_ISSUE


def test_only_open_blockers_remain_and_closed_ones_are_resolved_whatever_the_reason():
    raw = blocked_by(blocker(8), blocker(3, "CLOSED"), blocker(9, "CLOSED"), blocker(2))
    assert parse_blockers(raw, "o/r") == ((BlockerRef(2), BlockerRef(8)), "")


def test_every_closed_blocker_means_no_blockers():
    assert parse_blockers(blocked_by(blocker(3, "CLOSED")), "o/r") == ((), "")
    assert parse_blockers(blocked_by(), "o/r") == ((), "")


def test_a_cross_repository_blocker_keeps_its_repository_and_the_own_repository_is_matched_case_blind():
    raw = blocked_by(blocker(4, repo="other/lib"), blocker(7, repo="O/R"))
    assert parse_blockers(raw, "o/r") == ((BlockerRef(7), BlockerRef(4, "other/lib")), "")


@pytest.mark.parametrize("raw", [None, [], "x", {"nodes": None}, {"totalCount": 1}])
def test_an_absent_or_malformed_connection_is_no_blockers_with_a_reason(raw):
    refs, problem = parse_blockers(raw, "o/r")
    assert refs == () and problem


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"number": 4, "state": "MERGED", "repository": {"nameWithOwner": "o/r"}},
        {"number": "4", "state": "OPEN", "repository": {"nameWithOwner": "o/r"}},
        {"number": True, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}},
        {"number": 0, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}},
        {"number": 4, "state": "OPEN", "repository": None},
        {"number": 4, "state": "OPEN", "repository": {"nameWithOwner": "not a repo"}},
    ],
)
def test_a_malformed_blocker_node_is_dropped_never_a_block_and_valid_peers_stay(bad):
    refs, problem = parse_blockers(blocked_by(bad, blocker(6)), "o/r")
    assert refs == (BlockerRef(6),) and "1 malformed" in problem


def test_blockers_github_counts_but_does_not_list_are_unseen_not_blocking():
    refs, problem = parse_blockers(blocked_by(blocker(3, "CLOSED"), total=250), "o/r")
    assert refs == () and "249 blocker(s) unseen" in problem


def test_a_huge_blocker_list_parses_every_open_node():
    raw = blocked_by(*(blocker(n) for n in range(1, 5001)))
    refs, problem = parse_blockers(raw, "o/r")
    assert len(refs) == 5000 and refs[0] == BlockerRef(1) and problem == ""


def test_the_world_carries_open_blockers_and_logs_an_unreadable_connection(capsys):
    runner = GraphQL(
        {
            "issues": page([
                issue_node(5, blockedBy=blocked_by(blocker(4), blocker(3, "CLOSED"), blocker(9, repo="x/y"))),
                issue_node(6, blockedBy="garbage"),
                issue_node(7, blockedBy=blocked_by(None, blocker(8), total=2)),
            ]),
            "pullRequests": page([]),
            "refs": page([]),
        }
    )
    world = GitHubGateway("o/r", runner=runner).build_world("dev")
    assert {i.number: i.blocked_by for i in world.issues} == {
        5: (BlockerRef(4), BlockerRef(9, "x/y")), 6: (), 7: (BlockerRef(8),)}
    err = capsys.readouterr().err
    assert "issue #6: blockedBy is absent or malformed; unreadable blockers never block" in err
    assert "issue #5" not in err


def test_an_error_inside_blocked_by_alone_never_skips_the_tick(capsys):
    # A blocker GitHub will not show this token: it nulls the connection and
    # reports an error on that path, with the rest of the data intact.
    issues = {"issues": page([issue_node(5, blockedBy=None)])}
    error = {"message": "Resource not accessible", "path": ["repository", "issues", "nodes", 0, "blockedBy"]}
    rest = GraphQL({"pullRequests": page([]), "refs": page([])})

    def runner(argv, token):
        query = next(a for a in argv if a.startswith("query="))
        return json.dumps({"data": {"repository": issues}, "errors": [error]}) if "issues(" in query else rest(argv, token)

    world = GitHubGateway("o/r", runner=runner).build_world("dev")
    assert [(i.number, i.blocked_by) for i in world.issues] == [(5, ())]
    assert "issue #5: blockedBy is absent or malformed" in capsys.readouterr().err


def test_any_other_graphql_error_still_fails_the_read():
    repository = {"issues": page([issue_node(5)]), "pullRequests": page([]), "refs": page([])}
    for errors in ([{"message": "x", "path": ["repository", "issues"]}], [{"message": "x"}], "boom"):
        answers = [json.dumps({"data": {"repository": repository}, "errors": errors})]
        with pytest.raises(FixObservationIncomplete):
            GitHubGateway("o/r", runner=lambda argv, _t: answers.pop(0)).build_world("dev")


# ── writing a steward's "blocked by" link ────────────────────────────────────


def link_page(*, subject_blockers=(), blocker_state="OPEN", blocker_blockers=()) -> dict:
    return {"subject": {"id": "I5", "state": "OPEN", "blockedBy": blocked_by(*subject_blockers)},
            "blocker": {"id": "I4", "state": blocker_state, "blockedBy": blocked_by(*blocker_blockers)}}


def test_a_link_read_reports_both_ends_with_only_this_repositorys_open_blockers_of_the_blocker():
    page = link_page(subject_blockers=(blocker(4),),
                     blocker_blockers=(blocker(5), blocker(7, "CLOSED"), blocker(5, repo="x/y")))
    assert parse_blocker_link(page, "o/r", 4) == BlockerLinkFacts("I5", "I4", True, True, (5,))
    assert parse_blocker_link(link_page(blocker_state="CLOSED"), "o/r", 4).blocker_open is False


@pytest.mark.parametrize(
    "page",
    [None, {}, {"subject": None, "blocker": {}}, {**link_page(), "blocker": {"id": "I4", "state": "GONE"}},
     {**link_page(), "subject": {"id": "", "blockedBy": blocked_by()}},
     {**link_page(), "blocker": {"id": "I4", "state": "OPEN", "blockedBy": None}},
     {**link_page(), "blocker": {"id": "I4", "state": "OPEN", "blockedBy": blocked_by(None, blocker(9))}},
     {**link_page(), "blocker": {"id": "I4", "state": "OPEN", "blockedBy": blocked_by(total=3)}}],
)
def test_an_unreadable_link_read_raises_so_nothing_is_written_on_a_guess(page):
    with pytest.raises(FixObservationIncomplete):
        parse_blocker_link(page, "o/r", 4)


def test_gaps_in_the_issues_own_blockers_do_not_stop_the_link_read():
    page = {**link_page(), "subject": {"id": "I5", "blockedBy": blocked_by(None, blocker(4), total=9)}}
    assert parse_blocker_link(page, "o/r", 4).linked is True


def test_the_link_is_read_in_one_query_and_written_with_add_blocked_by():
    calls = []

    def runner(argv, _token):
        calls.append(argv)
        query = next(a for a in argv if a.startswith("query="))
        if query.startswith("query=mutation"):
            return json.dumps({"data": {"addBlockedBy": {"issue": {"number": 5}}}})
        return json.dumps({"data": {"repository": link_page()}})

    gateway = GitHubGateway("o/r", runner=runner)
    assert gateway.blocker_link(5, 4).linked is False
    gateway.add_blocked_by("I5", "I4")
    read, write = (next(a for a in argv if a.startswith("query=")) for argv in calls)
    assert "subject:issue(number:5)" in read and "blocker:issue(number:4)" in read
    assert "addBlockedBy(input:{issueId:$issue,blockingIssueId:$blocker})" in write
    assert "issue=I5" in calls[1] and "blocker=I4" in calls[1]
    with pytest.raises(ValueError):
        gateway.blocker_link(5, True)


def test_a_merged_harness_prs_merge_time_is_read_and_a_missing_or_malformed_one_is_blank():
    def at(merged_at):
        return {"source": {**xref(10)["source"], "mergedAt": merged_at}}

    times = [p.merged_at for p in parse_merged_harness_prs([at("2026-10-14T10:00:00Z"), xref(10), at(7)], "o/r")]
    assert times == ["2026-10-14T10:00:00Z", "", ""]
