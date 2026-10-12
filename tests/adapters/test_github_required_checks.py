"""Complete GitHub policy and report reads are required before CI actions."""

import json
import re
from pathlib import Path

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway, Identity, _subprocess_runner
from issue_agent.domain.checks import Observed, RequiredCheck, Unavailable
from issue_agent.domain.ci import Clear, reduce_ci


@pytest.mark.parametrize("query_name", ["_METADATA", "_REPORTS"])
def test_selected_graphql_fields_exist_in_live_github_schema(query_name):
    from issue_agent.adapters import github_ci
    schema = json.loads((Path(__file__).parents[1] / "fixtures/github-ci-schema.json").read_text())["types"]
    # These queries have no aliases or named fragments; arguments don't change field ownership.
    query = re.sub(r"\([^()]*\)", "", getattr(github_ci, query_name))
    tokens = iter(re.findall(r"\.\.\.|[A-Za-z_][A-Za-z_0-9]*|[{}]", query))
    assert next(tokens) == "query" and next(tokens) == "{"
    def selections(owner):
        target = owner
        for field in tokens:
            if field == "}":
                return
            if field == "...":
                assert next(tokens) == "on"
                target = next(tokens)
                assert next(tokens) == "{"
                selections(target)
            elif field == "{":
                selections(target)
            elif field != "__typename":
                assert field in schema[owner], f"{owner}.{field} is absent from GitHub's schema"
                target = schema[owner][field]
    selections("Query")


def report(name="unit", **changes):
    return {"__typename": "CheckRun", "id": name, "databaseId": 1, "name": name, "status": "COMPLETED",
        "conclusion": "SUCCESS", "startedAt": None, "detailsUrl": None,
        "isRequired": True, "checkSuite": {"app": {"databaseId": 1}, "workflowRun": None}, **changes}


def metadata(head="head", merge=None):
    return {"data": {"repository": {"pullRequest": {"id": "pr-id", "headRefOid": head,
        "baseRefName": "dev", "potentialMergeCommit": merge,
        "commits": {"nodes": [{"commit": {"id": "commit-id", "oid": head, "statusCheckRollup": None}}]}}}}}


def page(nodes, *, more=False, cursor=None, total=None, head="head", revision="head", merge=None):
    return {"data": {"repository": {"pullRequest": {"id": "pr-id", "headRefOid": head, "baseRefName": "dev", "potentialMergeCommit": merge}},
        "node": {"oid": revision, "statusCheckRollup": {"contexts": {"nodes": nodes,
            "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}}


def gateway(*, branch=None, rules=None, pages=None, meta=None):
    branch = {"protected": True, "protection": {"required_status_checks": {
        "contexts": ["unit"], "checks": [{"context": "unit", "app_id": 1}]}}} if branch is None else branch
    def run(argv, token):
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"number": 3, "headRefOid": "head", "baseRefName": "dev"})
        if any("/rules/branches/" in v for v in argv):
            return json.dumps([[]] if rules is None else rules)
        if any("/branches/" in v for v in argv):
            return json.dumps(branch)
        if "graphql" in argv:
            return json.dumps((metadata() if meta is None else meta) if "--paginate" not in argv else
                              ([page([report()])] if pages is None else pages))
        raise AssertionError(argv)
    return GitHubGateway("o/r", Identity(), runner=run)


def test_branch_summary_and_active_rules_union_app_bound_contexts():
    rules = [[{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": "lint", "integration_id": 2}]}}]]
    ci = gateway(rules=rules, pages=[page([report(), report("lint", checkSuite={"app": {"databaseId": 2}, "workflowRun": None})])]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert ci.policy == frozenset({RequiredCheck("unit", 1), RequiredCheck("lint", 2)})
    assert isinstance(reduce_ci(ci), Clear)


def test_unprotected_summary_with_absent_protection_is_an_observed_empty_policy():
    ci = gateway(branch={"protected": False}, pages=[page([report(isRequired=False)])]).get_pr(3).ci
    assert isinstance(ci, Observed) and not ci.policy


@pytest.mark.parametrize("pages", [[], None, [{}], [page([None])], [page([report(isRequired=None)])],
    [page([report()], more=True, cursor="next", total=2)], [page([report()], total=2)],
    [page([report()], head="changed")], [page([report()], revision="other")],
    [page([report(status="UNRECOGNIZED")])], [{"errors": [{"message": "denied"}]}]])
def test_partial_absent_malformed_and_changed_revision_reads_are_unavailable(pages):
    # None explicitly means a null transport response in this case.
    gw = gateway(pages=pages if pages is not None else [None])
    assert isinstance(gw.get_pr(3).ci, Unavailable)


@pytest.mark.parametrize("branch,rules", [({"protected": True}, [[]]),
    ({"protected": False, "protection": None}, []),
    ({"protected": False}, [[None]]),
    ({"protected": False}, [[{"type": "required_status_checks", "parameters": {"required_status_checks": None}}]])])
def test_incomplete_or_unsupported_policy_is_unavailable(branch, rules):
    assert isinstance(gateway(branch=branch, rules=rules).get_pr(3).ci, Unavailable)


def test_large_paginated_rollup_preserves_every_obligation():
    nodes = [report(f"matrix-{i}", isRequired=False) for i in range(15000)]
    pages = [page(nodes[i:i+100], more=i+100 < len(nodes), cursor=f"cursor-{i}", total=len(nodes))
             for i in range(0, len(nodes), 100)]
    ci = gateway(branch={"protected": False}, pages=pages).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.reports) == 15000


@pytest.mark.parametrize("head_red", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_declared_policy_reads_test_merge_checks_and_empty_policy_reads_head(head_red, declared):
    from issue_agent.domain.ci import Failed
    merge_commit = {"id": "merge-id", "oid": "merge-sha", "statusCheckRollup": {"contexts": {"totalCount": 1}}}
    meta = metadata(merge=merge_commit)
    def run(argv, token):
        if "--paginate" in argv and "graphql" in argv:
            merge = "commit=merge-id" in argv
            red = not head_red if merge else head_red
            return json.dumps([page([report(conclusion="FAILURE" if red else "SUCCESS")],
                revision="merge-sha" if merge else "head", merge=merge_commit)])
        return original(argv, token)
    gw = gateway(meta=meta, branch=None if declared else {"protected": False})
    original = gw._run
    gw._run = run
    pr = gw.get_pr(3)
    ci = pr.ci
    assert isinstance(ci, Observed) and ci.tested_revision == ("merge-sha" if declared else "head")
    red = not head_red if declared else head_red
    assert isinstance(reduce_ci(ci), Failed if red else Clear)


@pytest.mark.parametrize("merge", [None, {"id": "merge-id", "oid": "merge-sha", "statusCheckRollup": None},
    {"id": "merge-id", "oid": "merge-sha", "statusCheckRollup": {"contexts": {"totalCount": 0}}}])
def test_declared_policy_falls_back_to_head_without_test_merge_checks(merge):
    ci = gateway(meta=metadata(merge=merge)).get_pr(3).ci
    assert isinstance(ci, Observed) and ci.tested_revision == "head"
    assert isinstance(reduce_ci(ci), Clear)


def test_head_rollup_disappearing_between_metadata_and_reports_is_unavailable():
    meta = metadata()
    meta["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {"state": "FAILURE"}
    reports = page([])
    reports["data"]["node"]["statusCheckRollup"] = None
    assert isinstance(gateway(branch={"protected": False}, meta=meta, pages=[reports]).get_pr(3).ci, Unavailable)


@pytest.mark.parametrize("rule_type", ["required_deployments", "merge_queue", "pull_request", "workflows", "future_rule"])
def test_non_check_rule_leaves_green_ci_clear(rule_type):
    rules = [[{"type": rule_type, "parameters": {
        "required_deployment_environments": ["production"]}}]]
    assert isinstance(reduce_ci(gateway(rules=rules).get_pr(3).ci), Clear)


def test_consistently_null_head_rollup_is_an_observed_empty_report_set():
    reports = page([])
    reports["data"]["node"]["statusCheckRollup"] = None
    ci = gateway(branch={"protected": False}, pages=[reports]).get_pr(3).ci
    assert isinstance(ci, Observed) and isinstance(reduce_ci(ci), Clear)


def test_absent_head_rollup_metadata_is_unavailable_even_when_reports_are_green():
    meta = metadata()
    del meta["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    assert isinstance(gateway(meta=meta).get_pr(3).ci, Unavailable)


@pytest.mark.parametrize("message,expected", [
    ("Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)", "[[]]"),
    ("Resource not accessible by integration (HTTP 403)", ""),
    ("Not Found (HTTP 404)", ""),
    ("Upgrade to GitHub Pro or make this repository public (HTTP 404)", ""),
    ("API rate limit exceeded. Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)", ""),
    ("Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 404)", ""),
])
def test_only_the_known_rulesets_capability_403_means_no_rulesets(monkeypatch, message, expected):
    from types import SimpleNamespace
    monkeypatch.setattr("issue_agent.adapters.github.subprocess.run", lambda *a, **k:
        SimpleNamespace(returncode=1, stdout="", stderr=message))
    if "rate limit" in message:
        with pytest.raises(GhUnavailable):
            _subprocess_runner(["gh", "api", "--paginate", "--slurp", "repos/o/r/rules/branches/dev"], None)
        return
    assert _subprocess_runner(["gh", "api", "--paginate", "--slurp", "repos/o/r/rules/branches/dev"], None) == expected


@pytest.mark.parametrize("ordered", [True, False])
def test_empty_policy_successful_rerun_supersedes_old_failure_across_pages(ordered):
    old = report(id="old", databaseId=10, conclusion="FAILURE")
    current = report(id="new", databaseId=11, startedAt=None)
    nodes = [old, current] if ordered else [current, old]
    ci = gateway(branch={"protected": False}, pages=[page(nodes[:1], more=True, cursor="next", total=2), page(nodes[1:], total=2)]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert len(ci.reports) == 1 and isinstance(reduce_ci(ci), Clear)


@pytest.mark.parametrize("ordered", [False, True])
def test_required_sibling_jobs_with_same_name_in_one_workflow_both_gate(ordered):
    from issue_agent.domain.ci import Failed
    suite = {"app": {"databaseId": 1}, "workflowRun": {"event": "pull_request", "workflow": {"id": "CI"}}}
    nodes = [report(id="job-a", databaseId=10, conclusion="FAILURE", checkSuite=suite),
        report(id="job-b", databaseId=11, checkSuite=suite)]
    if ordered:
        nodes.reverse()
    ci = gateway(pages=[page(nodes[:1], more=True, cursor="next", total=2), page(nodes[1:], total=2)]).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.reports) == 2
    assert isinstance(reduce_ci(ci), Failed)


def test_latest_reports_preserve_app_and_status_check_collisions():
    status = {"__typename": "StatusContext", "id": "status", "context": "unit", "state": "FAILURE",
        "targetUrl": None, "isRequired": True, "createdAt": "2026-10-12T00:00:00Z"}
    other = report(id="other-app", checkSuite={"app": {"databaseId": 2}, "workflowRun": None}, isRequired=False, conclusion="FAILURE")
    ci = gateway(pages=[page([report(), other, status])]).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.reports) == 3
    from issue_agent.domain.ci import Failed
    assert isinstance(reduce_ci(ci), Failed)


@pytest.mark.parametrize("rules", [[[{"type": ""}]], [[{"type": "pull_request"}], []]])
def test_malformed_rules_and_partial_ruleset_pages_are_unavailable(rules):
    assert isinstance(gateway(rules=rules).get_pr(3).ci, Unavailable)


def test_large_ruleset_pagination_unites_all_pages():
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": f"matrix-{i}", "integration_id": 1}]}} for i in range(1000)]
    ci = gateway(rules=[rules[i:i+100] for i in range(0, 1000, 100)]).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.policy) == 1001


@pytest.mark.parametrize("stdout,expected", [
    (json.dumps([{"message": "Upgrade to GitHub Pro or make this repository public to enable this feature.", "status": "403"}]), "[[]]"),
    (json.dumps([[{"type": "required_status_checks"}], {"message": "Upgrade to GitHub Pro or make this repository public", "status": "403"}]), ""),
    ("malformed", ""),
    (json.dumps({"message": "API rate limit exceeded. Upgrade to GitHub Pro or make this repository public to enable this feature.", "status": "403"}), ""),
])
def test_ruleset_capability_error_body_and_partial_success_are_classified(monkeypatch, stdout, expected):
    from types import SimpleNamespace
    monkeypatch.setattr("issue_agent.adapters.github.subprocess.run", lambda *a, **k:
        SimpleNamespace(returncode=1, stdout=stdout,
            stderr="Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"))
    assert _subprocess_runner(["gh", "api", "--paginate", "--slurp", "repos/o/r/rules/branches/dev"], None) == expected


@pytest.mark.parametrize("started", [None, "2026-10-12T00:00:00Z"])
def test_started_time_preserves_unknown_and_actual_deadline_evidence(started):
    ci = gateway(pages=[page([report(status="IN_PROGRESS", conclusion=None, startedAt=started)])]).get_pr(3).ci
    assert isinstance(ci, Observed) and ci.reports[0].started_at == (started or "")


def test_distinct_workflows_and_events_with_the_same_job_name_still_gate():
    def suite(workflow, event):
        return {"app": {"databaseId": 1}, "workflowRun": {"event": event, "workflow": {"id": workflow, "name": workflow}}}
    nodes = [report(id="failed-pr", databaseId=10, conclusion="FAILURE", checkSuite=suite("CI", "pull_request")),
        report(id="green-push", databaseId=11, checkSuite=suite("CI", "push")),
        report(id="green-other", databaseId=12, checkSuite=suite("Other", "pull_request"))]
    ci = gateway(pages=[page(nodes)]).get_pr(3).ci
    from issue_agent.domain.ci import Failed
    assert isinstance(ci, Observed) and len(ci.reports) == 3
    assert isinstance(reduce_ci(ci), Failed)


def test_required_workflows_with_identical_display_names_remain_distinct():
    def suite(identity):
        return {"app": {"databaseId": 1}, "workflowRun": {
            "event": "pull_request", "workflow": {"id": identity, "name": "CI"}}}
    nodes = [report(id="failed-workflow", databaseId=10, conclusion="FAILURE", checkSuite=suite("workflow-A")),
        report(id="green-workflow", databaseId=11, checkSuite=suite("workflow-B"))]
    from issue_agent.domain.ci import Failed
    assert isinstance(reduce_ci(gateway(pages=[page(nodes)]).get_pr(3).ci), Failed)


@pytest.mark.parametrize("cursor", [False, 0, 12, {}, ""])
def test_malformed_final_pagination_cursor_is_unavailable(cursor):
    assert isinstance(gateway(pages=[page([report()], cursor=cursor)]).get_pr(3).ci, Unavailable)


def test_required_status_infers_only_unambiguous_app_provenance():
    status = {"__typename": "StatusContext", "id": "status", "context": "unit", "state": "SUCCESS",
        "targetUrl": None, "isRequired": True, "createdAt": "2026-10-12T00:00:00Z"}
    ci = gateway(pages=[page([status])]).get_pr(3).ci
    assert isinstance(ci, Observed) and isinstance(reduce_ci(ci), Clear)
    assert ci.reports[0].app_id == 1
    branch = {"protected": True, "protection": {"required_status_checks": {"contexts": ["unit"],
        "checks": [{"context": "unit", "app_id": 1}, {"context": "unit", "app_id": 2}]}}}
    assert isinstance(gateway(branch=branch, pages=[page([status])]).get_pr(3).ci, Unavailable)


@pytest.mark.parametrize("changes", [{"startedAt": False}, {"detailsUrl": False}, {"databaseId": True},
    {"databaseId": None}, {"name": None}, {"isRequired": 1}])
def test_malformed_report_scalar_types_are_unavailable(changes):
    assert isinstance(gateway(pages=[page([report(**changes)])]).get_pr(3).ci, Unavailable)
