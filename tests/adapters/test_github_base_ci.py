"""Gap 22: the fix role's base-CI read, against GitHub's REST and GraphQL
answer shapes. The base branch's completed push runs and the fixer's base-CI
issues (an issue search) compose `WorldState.base_ci`; any unusable read
leaves it absent and the rest of the world intact."""

from __future__ import annotations

import json

import pytest

from issue_agent.adapters.github import FixObservationIncomplete, GhUnavailable, GitHubGateway
from issue_agent.adapters.github_ci import base_issues, base_runs


def _page(nodes) -> dict:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def run_row(name="macOS Smoke", run=1, conclusion="failure", *, branch="dev", event="push", status="completed",
            at="2026-10-14T00:00:00Z") -> dict:
    return {"id": run, "name": name, "html_url": f"https://github.com/o/r/actions/runs/{run}", "head_sha": f"sha{run}",
            "head_branch": branch, "event": event, "status": status, "conclusion": conclusion, "created_at": at,
            "updated_at": at}


def found(number, title, state="OPEN", author="fix-bot", closed=None, closer_login=None, by_pr=False) -> dict:
    events = [{"actor": {"login": closer_login}, "closer": {"__typename": "PullRequest"} if by_pr else None}]
    return {"number": number, "title": title, "state": state, "closedAt": closed, "author": {"login": author},
            "timelineItems": {"nodes": events if closer_login else []}}


def search(*nodes, count=None) -> dict:
    return {"issueCount": len(nodes) if count is None else count, "nodes": list(nodes)}


_NO_RUNS = {"total_count": 1_000_000, "workflow_runs": []}


class GitHub:
    """Answers the world read, the run listing and the issue search; records every call."""

    def __init__(self, runs=_NO_RUNS, issues=None):
        self.runs, self.issues = runs, issues or search()
        self.calls: list[list[str]] = []

    def __call__(self, argv, _token):
        self.calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            if any(a.startswith("query=") and "search(" in a for a in argv):
                return json.dumps({"data": {"search": self.issues}})
            return json.dumps({"data": {"repository": {"issues": _page([]), "pullRequests": _page([]),
                                                       "refs": _page([]), "baseTip": None}}})
        path = next(a for a in argv[2:] if a.startswith("repos/"))
        if "/actions/runs?" in path:
            return json.dumps(self.runs) if self.runs is not ... else ""
        raise AssertionError(argv)

    def rest(self, fragment: str) -> list[list[str]]:
        return [c for c in self.calls if any(fragment in a for a in c)]


def observe(github: GitHub, filed_by="fix-bot"):
    return GitHubGateway("o/r", runner=github).build_world("dev", filed_by=filed_by)


def test_the_read_composes_the_bases_completed_push_runs_and_the_fixers_issues():
    github = GitHub(
        runs={"total_count": 9_999, "workflow_runs": [run_row("macOS Smoke", 3), run_row("Lint", 7, "success")]},
        issues=search(found(40, "CI: Docs failing on dev"), found(41, "CI: Lint failing on dev", "CLOSED",
                                                                    closed="2026-10-13T00:00:00Z"),
                      found(42, "CI: macOS Smoke failing on dev", author="someone-else")),
    )
    base = observe(github).base_ci
    assert base is not None and [r.run_id for r in base.runs] == [3, 7]
    assert [(i.number, i.closed_at) for i in base.issues] == [(40, ""), (41, "2026-10-13T00:00:00Z")]
    listing = github.rest("/actions/runs?")[0][2]
    assert "branch=dev" in listing and "event=push" in listing and "status=completed" in listing
    query = next(a for c in github.calls for a in c if a.startswith("q="))
    assert query == 'q=repo:o/r is:issue author:fix-bot in:title "failing on dev"'


def test_without_a_filer_base_ci_is_not_read_at_all():
    github = GitHub(runs={"total_count": 1, "workflow_runs": [run_row()]})
    assert GitHubGateway("o/r", runner=github).build_world("dev").base_ci is None
    assert not github.rest("/actions/runs?") and not github.rest("search(")


def test_rows_of_another_branch_event_or_status_are_not_the_bases():
    github = GitHub(runs={"total_count": 3, "workflow_runs": [
        run_row(run=9, branch="fix/5-x"), run_row(run=8, event="schedule"), run_row(run=7, conclusion=None,
                                                                                 status="in_progress"),
        run_row(run=1, conclusion="success")]})
    assert [r.run_id for r in observe(github).base_ci.runs] == [1]


def test_a_completed_run_with_no_conclusion_is_no_verdict_not_a_malformed_page():
    assert base_runs({"workflow_runs": [run_row(conclusion=None)]}, "dev")[0].conclusion == "unknown"


@pytest.mark.parametrize("runs", [
    ...,  # gh answered nothing (a 404 or 403: Actions off, or no actions:read)
    None,
    [],
    {"workflow_runs": None},
    {"workflow_runs": ["x"]},
    {"workflow_runs": [{**run_row(), "id": "1"}]},
    {"workflow_runs": [{**run_row(), "created_at": "yesterday"}]},
    {"workflow_runs": [{**run_row(), "updated_at": None}]},
    {"workflow_runs": [{**run_row(), "conclusion": 3}]},
    {"workflow_runs": [{**run_row(), "html_url": ""}]},
])
def test_an_absent_or_malformed_run_listing_leaves_base_ci_unread_and_the_world_intact(runs):
    world = observe(GitHub(runs=runs))
    assert world.base_ci is None and world.issues == () and world.prs == ()


@pytest.mark.parametrize("issues", [
    None, search(found(40, "CI: x failing on dev"), count=101),  # past the one page read
    search({"number": "40", "title": "t", "state": "OPEN"}), search(found(40, "t", "LOCKED")),
    search(found(40, "t", "CLOSED", closed=None)),
])
def test_an_unusable_issue_search_leaves_base_ci_unread(issues):
    github = GitHub(issues=issues)
    github.issues = issues
    assert observe(github).base_ci is None


def test_an_issue_counts_as_harness_closed_only_when_the_fixer_itself_closed_it_last():
    at = "2026-10-13T00:00:00Z"
    issues = base_issues(search(found(1, "t", "CLOSED", closed=at, closer_login="Fix-Bot"),
                                found(2, "t", "CLOSED", closed=at, closer_login="alice"),
                                found(3, "t", "CLOSED", closed=at, closer_login="fix-bot", by_pr=True),
                                found(4, "t", "CLOSED", closed=at), found(5, "t", closer_login="fix-bot")), "fix-bot")
    assert [(i.number, i.harness_closed) for i in issues] == [(1, True), (2, False), (3, False), (4, False), (5, False)]


def test_only_the_fixers_issues_count_whatever_the_login_case():
    assert [i.number for i in base_issues(search(found(1, "t", author="Fix-Bot"), found(2, "t", author="x")),
                                          "fix-bot")] == [1]


def test_github_not_answering_the_base_ci_read_leaves_it_unread_and_the_tick_goes_on():
    github = GitHub()

    def runner(argv, token):
        if any("/actions/runs?" in a for a in argv):
            raise GhUnavailable("rate-limit", "API rate limit exceeded", argv=argv)
        return github(argv, token)

    world = GitHubGateway("o/r", runner=runner).build_world("dev", filed_by="fix-bot")
    assert world.base_ci is None and world.issues == ()


def test_create_issue_returns_the_number_gh_prints_and_refuses_no_link():
    calls = []

    def runner(argv, _token):
        calls.append(argv)
        return "https://github.com/o/r/issues/1234\n" if len(calls) == 1 else ""

    gateway = GitHubGateway("o/r", runner=runner)
    assert gateway.create_issue("CI: x failing on dev", "body") == 1234
    assert calls[0][:3] == ["gh", "issue", "create"] and "--label" not in calls[0]
    with pytest.raises(FixObservationIncomplete):
        gateway.create_issue("CI: x failing on dev", "body")
