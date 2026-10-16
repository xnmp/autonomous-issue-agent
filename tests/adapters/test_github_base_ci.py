"""Gap 22: the fix role's base-CI read, against GitHub's REST and GraphQL
answer shapes. The base branch's completed push runs and the fixer's base-CI
issues (an issue search) compose `WorldState.base`'s runs and issues; any
unusable read leaves them absent and the rest of the world intact."""

from __future__ import annotations

import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway
from issue_agent.adapters.github_ci import active_workflows, base_issues, base_reports, base_runs


def _page(nodes) -> dict:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def run_row(name="macOS Smoke", run=1, conclusion="failure", *, branch="dev", event="push", status="completed",
            at="2026-10-14T00:00:00Z", **extra) -> dict:
    return {"id": run, "name": name, "html_url": f"https://github.com/o/r/actions/runs/{run}", "head_sha": f"sha{run}",
            "head_branch": branch, "event": event, "status": status, "conclusion": conclusion, "created_at": at,
            "updated_at": at, **extra}


def found(number, title, state="OPEN", author="fix-bot", closed=None) -> dict:
    return {"number": number, "title": title, "state": state, "closedAt": closed, "author": {"login": author}}


def job(name, workflow, *, event="push", run="FAILURE", status="COMPLETED", conclusion="FAILURE") -> dict:
    """One check run on the base tip, with its workflow run's event and conclusion (its suite's)."""
    return {"__typename": "CheckRun", "id": f"CR{name}", "databaseId": 1, "name": name, "status": status,
            "conclusion": conclusion, "startedAt": "2026-10-12T00:00:00Z",
            "detailsUrl": "https://github.com/o/r/actions/runs/1/job/1",
            "checkSuite": {"conclusion": run, "app": {"databaseId": 15368},
                           "workflowRun": {"event": event, "workflow": {"name": workflow}}}}


def tip(*nodes) -> dict:
    contexts = {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}
    return {"name": "dev", "target": {"oid": "b9", "statusCheckRollup": {"id": "SCR1", "contexts": contexts}}}


PROTECTED = {"protected": True, "protection": {"required_status_checks": {"contexts": ["smoke"]}}}


def search(*nodes, count=None) -> dict:
    return {"issueCount": len(nodes) if count is None else count, "nodes": list(nodes)}


_NO_RUNS = {"total_count": 1_000_000, "workflow_runs": []}


class GitHub:
    """Answers the world read, the run listing and the issue search; records every call."""

    def __init__(self, runs=_NO_RUNS, issues=None, base_tip=None, branch=PROTECTED, workflows=...):
        self.runs, self.issues = runs, issues or search()
        self.base_tip, self.branch, self.workflows = base_tip, branch, workflows
        self.calls: list[list[str]] = []

    def __call__(self, argv, _token):
        self.calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            if any(a.startswith("query=") and "search(" in a for a in argv):
                return json.dumps({"data": {"search": self.issues}})
            return json.dumps({"data": {"repository": {"issues": _page([]), "pullRequests": _page([]),
                                                       "refs": _page([]), "baseTip": self.base_tip}}})
        path = next(a for a in argv[2:] if a.startswith("repos/"))
        if "/actions/runs?" in path:
            return json.dumps(self.runs) if self.runs is not ... else ""
        if path == "repos/o/r/branches/dev":
            return json.dumps(self.branch) if self.branch is not ... else ""
        if path.startswith("repos/o/r/rules/branches/dev"):
            return json.dumps([[]])
        if path.startswith("repos/o/r/actions/workflows?"):
            return json.dumps(self.workflows) if self.workflows is not ... else ""
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
    base = observe(github).base
    assert base is not None and base.runs is not None and [r.run_id for r in base.runs] == [3, 7]
    assert [(i.number, i.closed_at) for i in base.issues] == [(40, ""), (41, "2026-10-13T00:00:00Z")]
    listing = github.rest("/actions/runs?")[0][2]
    assert "branch=dev" in listing and "event=push" in listing
    query = next(a for c in github.calls for a in c if a.startswith("q="))
    assert query == 'q=repo:o/r is:issue author:fix-bot in:title "failing on dev" sort:created-desc'


def test_without_a_filer_base_ci_is_not_read_at_all():
    github = GitHub(runs={"total_count": 1, "workflow_runs": [run_row()]})
    assert GitHubGateway("o/r", runner=github).build_world("dev").base.runs is None
    assert not github.rest("/actions/runs?") and not github.rest("search(")


def test_rows_of_another_branch_or_event_are_not_the_bases_and_a_running_one_has_no_verdict():
    github = GitHub(runs={"total_count": 3, "workflow_runs": [
        run_row(run=9, branch="fix/5-x"), run_row(run=8, event="schedule"), run_row(run=7, conclusion=None,
                                                                                 status="in_progress"),
        run_row(run=1, conclusion="success")]})
    assert [(r.run_id, r.conclusion) for r in observe(github).base.runs] == [(7, ""), (1, "success")]


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
    assert world.base.runs is None and world.issues == () and world.prs == ()


@pytest.mark.parametrize("issues", [
    None, search(found(40, "CI: x failing on dev"), count=101),  # past the one page read
    search({"number": "40", "title": "t", "state": "OPEN"}), search(found(40, "t", "LOCKED")),
    search(found(40, "t", "CLOSED", closed=None)),
])
def test_an_unusable_issue_search_leaves_base_ci_unread(issues):
    github = GitHub(issues=issues)
    github.issues = issues
    assert observe(github).base.runs is None


def test_past_one_page_the_search_keeps_the_newest_hundred():
    nodes = [found(n, f"CI: x failing on dev since sha{n}", "CLOSED", closed="2026-10-13T00:00:00Z")
             for n in range(500, 400, -1)]
    assert len(base_issues(search(*nodes, count=5000), "fix-bot")) == 100


def test_a_workflow_is_required_when_its_check_run_on_the_tip_carries_a_required_context():
    github = GitHub(base_tip=tip(job("smoke", "Smoke"), job("lint", "Lint")))
    base = observe(github).base
    assert base.runs is not None and base.required == frozenset({"Smoke"})


@pytest.mark.parametrize("github", [
    GitHub(base_tip=tip(job("smoke", "Smoke")), branch=...),  # protection unreadable
    GitHub(base_tip=tip(job("smoke", "Smoke")), branch={"protected": "yes"}),
    GitHub(base_tip=None),  # the tip unread
])
def test_unread_required_workflows_ready_nothing_but_the_rest_of_base_ci_stands(github):
    base = observe(github).base
    assert base.runs is not None and base.required is None


def test_only_the_fixers_issues_count_whatever_the_login_case():
    assert [i.number for i in base_issues(search(found(1, "t", author="Fix-Bot"), found(2, "t", author="x")),
                                          "fix-bot")] == [1]


def _unanswered(github, read):
    def runner(argv, token):
        if any(read in a for a in argv):
            raise GhUnavailable("rate-limit", "API rate limit exceeded", argv=argv)
        return github(argv, token)
    return GitHubGateway("o/r", runner=runner).build_world("dev", filed_by="fix-bot")


def test_github_not_answering_the_base_ci_read_leaves_it_unread_and_the_tick_goes_on():
    world = _unanswered(GitHub(), "/actions/runs?")
    assert world.base.runs is None and world.issues == ()


def test_github_not_answering_the_workflow_listing_costs_the_base_ci_read_nothing_else():
    base = _unanswered(GitHub(runs={"total_count": 1, "workflow_runs": [run_row()]}), "/actions/workflows?").base
    assert base.workflows is None and base.runs is not None and len(base.runs) == 1


# ── #442: the tip's evidence is what base-CI issues are filed from ─────────


def test_an_actions_job_is_base_evidence_only_from_a_push_run_running_or_ended_red_or_green():
    reports = base_reports([
        job("red", "CI"), job("green", "CI", run="SUCCESS", conclusion="SUCCESS"),
        job("running", "CI", run=None, status="IN_PROGRESS", conclusion=None),
        job("timed out", "CI", run="TIMED_OUT"),
        job("a pr's twin", "CI", event="pull_request", run="SUCCESS", conclusion="SUCCESS"),
        job("scheduled", "CI", event="schedule"),
        job("cancelled", "CI", run="CANCELLED"),  # an aggregate job failed by its cancelled needs
        job("startup", "CI", run="STARTUP_FAILURE"),
        {**job("no event", "CI"), "checkSuite": {"conclusion": "FAILURE", "app": None, "workflowRun": {}}},
    ])
    assert [c.name for c in reports] == ["red", "green", "running", "timed out"]


def test_a_commit_status_or_another_apps_check_is_still_the_bases_evidence():
    other = {**job("vercel", "CI"), "checkSuite": {"conclusion": "FAILURE", "app": {"databaseId": 8}, "workflowRun": None}}
    status = {"__typename": "StatusContext", "id": "SC1", "createdAt": "2026-10-12T00:00:00Z", "context": "ci/x",
              "state": "FAILURE", "targetUrl": None}
    assert [(c.name, c.kind) for c in base_reports([other, status])] == [("vercel", "check"), ("ci/x", "status")]


def test_a_run_queued_running_or_being_re_run_is_listed_without_a_verdict():
    rows = [run_row(run=3, conclusion="failure", status="in_progress", run_attempt=2),  # the earlier attempt's
            run_row(run=2, conclusion=None, status="queued", run_attempt=1),
            run_row(run=1, conclusion="failure", run_attempt=3)]
    assert [(r.run_id, r.conclusion) for r in base_runs({"workflow_runs": rows}, "dev")] == [
        (3, ""), (2, ""), (1, "failure")]


# ── #449: the repository's workflows ─────────────────────────────────────────


def _workflows(*rows, count=None) -> dict:
    return {"total_count": len(rows) if count is None else count,
            "workflows": [{"id": i, "name": name, "path": f".github/workflows/w{i}.yml", "state": state}
                          for i, (name, state) in enumerate(rows)]}


def test_the_repositorys_active_workflows_are_read_with_the_base():
    github = GitHub(workflows=_workflows(("CI", "active"), ("Old", "disabled_manually"), ("Gone", "deleted")))
    assert observe(github).base.workflows == frozenset({"CI"})
    unasked = GitHub(workflows=_workflows(("CI", "active")))
    assert observe(unasked, filed_by="").base.workflows is None and not unasked.rest("/actions/workflows?")


@pytest.mark.parametrize("payload", [
    ..., None, [], {"workflows": []}, _workflows(("CI", "active"), count=101),  # past the one page read
    {"total_count": 1, "workflows": [{"name": None, "state": "active"}]}, {"total_count": 1, "workflows": ["CI"]},
])
def test_an_unusable_workflow_listing_closes_nothing_and_the_rest_of_the_base_stands(payload):
    base = observe(GitHub(workflows=payload, runs={"total_count": 1, "workflow_runs": [run_row()]})).base
    assert base.workflows is None and base.runs is not None and len(base.runs) == 1


def test_a_workflow_listing_of_any_size_parses_within_its_page():
    assert len(active_workflows(_workflows(*((f"w{i}", "active") for i in range(100))), _NO_RUNS)) == 100


def test_a_workflow_named_apart_from_its_runs_on_the_base_leaves_the_listing_unread():
    # Renamed on the base before the default branch: the listing's name may be the default branch's.
    listing = {"workflow_runs": [{**run_row("Smoke2"), "path": ".github/workflows/w0.yml"}]}
    assert active_workflows(_workflows(("Smoke", "active")), listing) is None
    assert active_workflows(_workflows(("Smoke2", "active")), listing) == frozenset({"Smoke2"})
    assert active_workflows(_workflows(("Smoke", "active")), {"workflow_runs": [run_row("Smoke2")]}) == {"Smoke"}


def test_the_commit_that_closed_an_issue_is_read_with_it_and_unreadable_is_none():
    def closed(closer):
        return {**found(1, "t", "CLOSED", closed="2026-10-15T11:55:46Z"),
                "timelineItems": {"nodes": [{"__typename": "ClosedEvent", "closer": closer}]}}
    nodes = [closed({"mergeCommit": {"oid": "d20c058"}}), closed({"oid": "abc"}), closed(None), closed({}),
             closed({"mergeCommit": None}), closed("x"), {**closed(None), "timelineItems": {"nodes": "x"}},
             {**closed(None), "timelineItems": None}, found(2, "t")]
    assert [i.closed_by for i in base_issues(search(*nodes), "fix-bot")] == ["d20c058", "abc", "", "", "", "", "", "",
                                                                             ""]


@pytest.mark.parametrize("path", [["x"], {"p": 1}])
def test_a_workflow_listing_with_an_unhashable_path_is_unread_and_the_tick_goes_on(path):
    github = GitHub(workflows={"total_count": 1, "workflows": [{"name": "CI", "path": path, "state": "active"}]},
                    runs={"total_count": 1, "workflow_runs": [run_row()]})
    base = observe(github).base
    assert base.workflows is None and base.runs is not None


def test_the_comment_the_fixer_closed_an_issue_with_is_read_and_any_other_is_not():
    def closed(*events):
        return {**found(1, "t", "CLOSED", closed="2026-10-15T11:55:46Z"), "timelineItems": {"nodes": list(events)}}

    def said(login, body="m"):
        return {"__typename": "IssueComment", "author": {"login": login}, "body": body}
    close, merged = {"__typename": "ClosedEvent", "closer": None}, {"__typename": "ClosedEvent", "closer": {"oid": "abc"}}
    nodes = [closed(said("Fix-Bot"), close),  # `gh issue close --comment` by the fixer
             closed(said("fix-bot"), merged),  # a commit closed it: the commit rules
             closed(said("someone"), close), closed(said("fix-bot"), {"__typename": "ReopenedEvent"}),
             closed({"__typename": "ReopenedEvent"}, close),  # reopened since its comment, closed by someone
             closed(said(None), close), closed(said("fix-bot", None), close), closed(close), found(2, "t")]
    assert [(i.closed_by, i.note) for i in base_issues(search(*nodes), "fix-bot")] == [
        ("", "m"), ("abc", ""), ("", ""), ("", ""), ("", ""), ("", ""), ("", ""), ("", ""), ("", "")]


def test_base_issue_filing_evidence_is_observed_for_closure_after_run_history_ages_out():
    body = "- **Smoke**: First failing run: https://x/runs/1 (head `sha1`)."
    (issue,) = base_issues(search({**found(40, "CI: base failing on dev (first)"), "body": body}), "fix-bot")
    assert issue.body == body
    with pytest.raises(ValueError):
        base_issues(search({**found(40, issue.title), "body": ["not", "text"]}), "fix-bot")
