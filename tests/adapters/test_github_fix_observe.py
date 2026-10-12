"""The fix role's one-query observation (slice 5b): parsers and the bounded
world build, against GitHub GraphQL answer shapes."""

from __future__ import annotations

import json

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
            "timelineItems": {"nodes": []}}
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


def ref(name: str, prs: int = 0, committed: str = "2026-10-01T00:00:00Z") -> dict:
    return {"name": name, "associatedPullRequests": {"totalCount": prs}, "target": {"committedDate": committed}}


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


def test_only_harness_branches_that_never_had_a_pr_are_owed_publication():
    refs = [ref("5-null-token"), ref("6-merged", prs=1), ref("7-new", committed="")]
    assert parse_fix_refs(refs) == (Branch("fix/5-null-token", "2026-10-01T00:00:00Z"), Branch("fix/7-new", ""))


@pytest.mark.parametrize("bad", [
    [None], [{"name": 5}], [{"name": "5-x"}], [{"name": "5-x", "associatedPullRequests": {"totalCount": "0"}}],
])
def test_an_unreadable_branch_association_is_incomplete_not_unpublished(bad):
    with pytest.raises(FixObservationIncomplete):
        parse_fix_refs(bad)


def test_merged_harness_prs_come_only_from_this_repository_and_fix_branches():
    def xref(number, state="MERGED", repo="O/R", head="fix/5-x"):
        return {"source": {"number": number, "state": state, "headRefName": head,
                           "repository": {"nameWithOwner": repo}}}
    node = issue_node(timelineItems={"nodes": [
        xref(10), xref(11, state="CLOSED"), xref(12, repo="fork/r"), xref(13, head="feat/5-x"), {"source": None},
    ]})
    assert [p.number for p in parse_merged_harness_prs(node, "o/r")] == [10]
    assert parse_merged_harness_prs(node, "o/r")[0].state == "MERGED"


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
    merged = {"source": {"number": 9, "state": "MERGED", "headRefName": "fix/4-old",
                         "repository": {"nameWithOwner": "o/r"}}}
    runner = GraphQL({
        "issues": page([issue_node(5), issue_node(4, timelineItems={"nodes": [merged]})]),
        "pullRequests": page([pr_node(headRefName="feature/x")]),  # not a harness PR: never fetched further
        "refs": page([ref("5-null-token")]),
    })
    world = GitHubGateway("o/r", runner=runner).build_fix_world()
    assert [i.number for i in world.issues] == [5, 4]
    assert [(p.number, p.state) for p in world.prs] == [(9, "MERGED")]
    assert world.branches == (Branch("fix/5-null-token", "2026-10-01T00:00:00Z"),)
    assert len(runner.queries) == 1  # one query for the whole observation


def test_an_issue_with_unreadable_comments_is_skipped_not_misread():
    broken = issue_node(6, comments={"totalCount": 3, "pageInfo": {"hasPreviousPage": False}, "nodes": []})
    runner = GraphQL({"issues": page([issue_node(5), broken]), "pullRequests": page([]), "refs": page([])})
    assert [i.number for i in GitHubGateway("o/r", runner=runner).build_fix_world().issues] == [5]


def test_a_top_level_connection_past_its_page_bound_skips_the_tick():
    first = {"issues": page([], more=True, cursor="c0"), "pullRequests": page([]), "refs": page([])}
    more = [{"issues": page([], more=True, cursor=f"c{i}")} for i in range(1, 12)]
    with pytest.raises(FixObservationIncomplete, match="issues exceed"):
        GitHubGateway("o/r", runner=GraphQL(first, *more)).build_fix_world()


@pytest.mark.parametrize("answer", [
    {"errors": [{"message": "denied"}]}, {"data": None}, {"data": {"repository": None}},
    {"data": {"repository": {"issues": page([]), "pullRequests": page([])}}},  # refs absent
])
def test_an_errored_or_partial_answer_skips_the_tick(answer):
    gateway = GitHubGateway("o/r", runner=lambda argv, token: json.dumps(answer))
    with pytest.raises(FixObservationIncomplete):
        gateway.build_fix_world()


def test_an_unreadable_open_pr_still_shadows_its_issue_with_no_head():
    runner = GraphQL({"issues": page([issue_node(5)]),
                      "pullRequests": page([pr_node(labels={"totalCount": 2, "nodes": []})]),
                      "refs": page([])})
    world = GitHubGateway("o/r", runner=runner).build_fix_world()
    assert [(p.number, p.head_ref_oid, p.content_known) for p in world.prs] == [(100, "", False)]
