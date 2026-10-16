"""Complete GitHub policy and report reads are required before CI actions."""

import json
import re
from pathlib import Path

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway, Identity, _subprocess_runner
from issue_agent.domain.checks import Observed, Unavailable
from issue_agent.domain.ci import Clear, Waiting, reduce_ci


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
    return {
        "__typename": "CheckRun",
        "id": name,
        "databaseId": 1,
        "name": name,
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "startedAt": None,
        "detailsUrl": None,
        "isRequired": True,
        "checkSuite": {"app": {"databaseId": 1}, "workflowRun": None},
        **changes,
    }


def metadata(head="head", merge=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": "pr-id",
                    "headRefOid": head,
                    "baseRefName": "dev",
                    "potentialMergeCommit": merge,
                    "commits": {"nodes": [{"commit": {"id": "commit-id", "oid": head, "statusCheckRollup": None}}]},
                }
            }
        }
    }


def page(nodes, *, more=False, cursor=None, total=None, head="head", revision="head", merge=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {"id": "pr-id", "headRefOid": head, "baseRefName": "dev", "potentialMergeCommit": merge}
            },
            "node": {
                "oid": revision,
                "statusCheckRollup": {
                    "contexts": {
                        "nodes": nodes,
                        "totalCount": len(nodes) if total is None else total,
                        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                    }
                },
            },
        }
    }


def gateway(*, branch=None, rules=None, pages=None, meta=None, jobs=None):
    branch = (
        {
            "protected": True,
            "protection": {
                "required_status_checks": {"contexts": ["unit"], "checks": [{"context": "unit", "app_id": 1}]}
            },
        }
        if branch is None
        else branch
    )

    def run(argv, token):
        if any("pullRequest(number:3){id number title body" in a for a in argv):
            node = {"id": "P3", "number": 3, "headRefOid": "head", "baseRefName": "dev",
                    "labels": {"totalCount": 0, "nodes": []}}
            node |= {name: {"nodes": [], "totalCount": 0, "pageInfo": {
                "hasNextPage" if name == "files" else "hasPreviousPage": False}}
                for name in ("comments", "reviews", "files")}
            return json.dumps({"data": {"repository": {"pullRequest": node}}})
        if jobs is not None and any("/actions/runs/77/jobs?filter=all" in v for v in argv):
            return json.dumps(jobs)
        if any("/rules/branches/" in v for v in argv):
            return json.dumps([[]] if rules is None else rules)
        if any("/branches/" in v for v in argv):
            return json.dumps(branch)
        if "graphql" in argv:
            return json.dumps(
                (metadata() if meta is None else meta)
                if "--paginate" not in argv
                else ([page([report()])] if pages is None else pages)
            )
        raise AssertionError(argv)

    return GitHubGateway("o/r", Identity(), runner=run)


def test_unprotected_summary_with_absent_protection_is_an_observed_empty_policy():
    ci = gateway(branch={"protected": False}, pages=[page([report(isRequired=False)])]).get_pr(3).ci
    assert isinstance(ci, Observed) and not ci.policy


def test_same_named_checks_from_different_apps_are_kept_as_distinct_reports():
    # One app's newer success must not shadow another app's failure of the same name.
    failing = report(
        id="b",
        databaseId=2,
        conclusion="FAILURE",
        isRequired=False,
        checkSuite={"app": {"databaseId": 2}, "workflowRun": None},
    )
    ci = gateway(branch={"protected": False}, pages=[page([report(isRequired=False), failing])]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert sorted(check.app_id for check in ci.reports) == [1, 2]


@pytest.mark.parametrize(
    "pages",
    [
        [],
        None,
        [{}],
        [page([None])],
        [page([report(isRequired=None)])],
        [page([report()], more=True, cursor="next", total=2)],
        [page([report()], total=2)],
        [page([report()], head="changed")],
        [page([report()], revision="other")],
        [page([report(status="UNRECOGNIZED")])],
        [{"errors": [{"message": "denied"}]}],
    ],
)
def test_partial_absent_malformed_and_changed_revision_reads_are_unavailable(pages):
    # None explicitly means a null transport response in this case.
    gw = gateway(pages=pages if pages is not None else [None])
    assert isinstance(gw.get_pr(3).ci, Unavailable)


@pytest.mark.parametrize(
    "branch,rules",
    [
        ({"protected": True}, [[]]),
        ({"protected": False, "protection": None}, []),
        ({"protected": False}, [[None]]),
        ({"protected": False}, [[{"type": "required_status_checks", "parameters": {"required_status_checks": None}}]]),
    ],
)
def test_incomplete_or_unsupported_policy_is_unavailable(branch, rules):
    assert isinstance(gateway(branch=branch, rules=rules).get_pr(3).ci, Unavailable)


def test_large_paginated_rollup_preserves_every_obligation():
    nodes = [report(f"matrix-{i}", isRequired=False) for i in range(15000)]
    pages = [
        page(nodes[i : i + 100], more=i + 100 < len(nodes), cursor=f"cursor-{i}", total=len(nodes))
        for i in range(0, len(nodes), 100)
    ]
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
            return json.dumps(
                [
                    page(
                        [report(conclusion="FAILURE" if red else "SUCCESS")],
                        revision="merge-sha" if merge else "head",
                        merge=merge_commit,
                    )
                ]
            )
        return original(argv, token)

    gw = gateway(meta=meta, branch=None if declared else {"protected": False})
    original = gw._run
    gw._run = run
    pr = gw.get_pr(3)
    ci = pr.ci
    assert isinstance(ci, Observed) and ci.tested_revision == ("merge-sha" if declared else "head")
    red = not head_red if declared else head_red
    assert isinstance(reduce_ci(ci), Failed if red else Clear)


@pytest.mark.parametrize(
    "merge",
    [
        None,
        {"id": "merge-id", "oid": "merge-sha", "statusCheckRollup": None},
        {"id": "merge-id", "oid": "merge-sha", "statusCheckRollup": {"contexts": {"totalCount": 0}}},
    ],
)
def test_declared_policy_falls_back_to_head_without_test_merge_checks(merge):
    ci = gateway(meta=metadata(merge=merge)).get_pr(3).ci
    assert isinstance(ci, Observed) and ci.tested_revision == "head"
    assert isinstance(reduce_ci(ci), Clear)


def test_head_rollup_disappearing_between_metadata_and_reports_is_unavailable():
    meta = metadata()
    meta["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
        "state": "FAILURE"
    }
    reports = page([])
    reports["data"]["node"]["statusCheckRollup"] = None
    assert isinstance(gateway(branch={"protected": False}, meta=meta, pages=[reports]).get_pr(3).ci, Unavailable)


@pytest.mark.parametrize(
    "rule_type", ["required_deployments", "merge_queue", "pull_request", "workflows", "future_rule"]
)
def test_non_check_rule_leaves_green_ci_clear(rule_type):
    rules = [[{"type": rule_type, "parameters": {"required_deployment_environments": ["production"]}}]]
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


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)", "[[]]"),
        ("Resource not accessible by integration (HTTP 403)", ""),
        ("Not Found (HTTP 404)", ""),
        ("Upgrade to GitHub Pro or make this repository public (HTTP 404)", ""),
        (
            "API rate limit exceeded. Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)",
            "",
        ),
        ("Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 404)", ""),
    ],
)
def test_only_the_known_rulesets_capability_403_means_no_rulesets(monkeypatch, message, expected):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "issue_agent.adapters.github.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=message),
    )
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
    ci = (
        gateway(
            branch={"protected": False},
            pages=[page(nodes[:1], more=True, cursor="next", total=2), page(nodes[1:], total=2)],
        )
        .get_pr(3)
        .ci
    )
    assert isinstance(ci, Observed)
    assert len(ci.reports) == 1 and isinstance(reduce_ci(ci), Clear)


@pytest.mark.parametrize("ordered", [False, True])
def test_required_sibling_jobs_with_same_name_in_one_workflow_both_gate(ordered):
    from issue_agent.domain.ci import Failed

    suite = {"app": {"databaseId": 1}, "workflowRun": {"event": "pull_request", "workflow": {"id": "CI"}}}
    nodes = [
        report(id="job-a", databaseId=10, conclusion="FAILURE", checkSuite=suite),
        report(id="job-b", databaseId=11, checkSuite=suite),
    ]
    if ordered:
        nodes.reverse()
    ci = gateway(pages=[page(nodes[:1], more=True, cursor="next", total=2), page(nodes[1:], total=2)]).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.reports) == 2
    assert isinstance(reduce_ci(ci), Failed)


@pytest.mark.parametrize("rules", [[[{"type": ""}]], [[{"type": "pull_request"}], []]])
def test_malformed_rules_and_partial_ruleset_pages_are_unavailable(rules):
    assert isinstance(gateway(rules=rules).get_pr(3).ci, Unavailable)


def test_large_ruleset_pagination_unites_all_pages():
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": f"matrix-{i}", "integration_id": 1}]},
        }
        for i in range(1000)
    ]
    ci = gateway(rules=[rules[i : i + 100] for i in range(0, 1000, 100)]).get_pr(3).ci
    assert isinstance(ci, Observed) and len(ci.policy) == 1001


@pytest.mark.parametrize(
    "stdout,expected",
    [
        (
            json.dumps(
                [
                    {
                        "message": "Upgrade to GitHub Pro or make this repository public to enable this feature.",
                        "status": "403",
                    }
                ]
            ),
            "[[]]",
        ),
        (
            json.dumps(
                [
                    [{"type": "required_status_checks"}],
                    {"message": "Upgrade to GitHub Pro or make this repository public", "status": "403"},
                ]
            ),
            "",
        ),
        ("malformed", ""),
        (
            json.dumps(
                {
                    "message": "API rate limit exceeded. Upgrade to GitHub Pro or make this repository public to enable this feature.",
                    "status": "403",
                }
            ),
            "",
        ),
    ],
)
def test_ruleset_capability_error_body_and_partial_success_are_classified(monkeypatch, stdout, expected):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "issue_agent.adapters.github.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stdout=stdout,
            stderr="Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)",
        ),
    )
    assert _subprocess_runner(["gh", "api", "--paginate", "--slurp", "repos/o/r/rules/branches/dev"], None) == expected


def test_distinct_workflows_and_events_with_the_same_job_name_still_gate():
    def suite(workflow, event):
        return {
            "app": {"databaseId": 1},
            "workflowRun": {"event": event, "workflow": {"id": workflow, "name": workflow}},
        }

    nodes = [
        report(id="failed-pr", databaseId=10, conclusion="FAILURE", checkSuite=suite("CI", "pull_request")),
        report(id="green-push", databaseId=11, checkSuite=suite("CI", "push")),
        report(id="green-other", databaseId=12, checkSuite=suite("Other", "pull_request")),
    ]
    ci = gateway(pages=[page(nodes)]).get_pr(3).ci
    from issue_agent.domain.ci import Failed

    assert isinstance(ci, Observed) and len(ci.reports) == 3
    assert isinstance(reduce_ci(ci), Failed)


@pytest.mark.parametrize("cursor", [False, 0, 12, {}, ""])
def test_malformed_final_pagination_cursor_is_unavailable(cursor):
    assert isinstance(gateway(pages=[page([report()], cursor=cursor)]).get_pr(3).ci, Unavailable)


def test_required_status_infers_only_unambiguous_app_provenance():
    status = {
        "__typename": "StatusContext",
        "id": "status",
        "context": "unit",
        "state": "SUCCESS",
        "targetUrl": None,
        "isRequired": True,
        "createdAt": "2026-10-12T00:00:00Z",
    }
    ci = gateway(pages=[page([status])]).get_pr(3).ci
    assert isinstance(ci, Observed) and isinstance(reduce_ci(ci), Clear)
    assert ci.reports[0].app_id == 1
    branch = {
        "protected": True,
        "protection": {
            "required_status_checks": {
                "contexts": ["unit"],
                "checks": [{"context": "unit", "app_id": 1}, {"context": "unit", "app_id": 2}],
            }
        },
    }
    assert isinstance(gateway(branch=branch, pages=[page([status])]).get_pr(3).ci, Unavailable)


@pytest.mark.parametrize(
    "changes",
    [
        {"startedAt": False},
        {"detailsUrl": False},
        {"databaseId": True},
        {"databaseId": None},
        {"name": None},
        {"isRequired": 1},
    ],
)
def test_malformed_report_scalar_types_are_unavailable(changes):
    assert isinstance(gateway(pages=[page([report(**changes)])]).get_pr(3).ci, Unavailable)


def test_each_observed_check_run_carries_its_job_id_so_a_rerun_is_new_evidence():
    # tauri #1078: a failure's identity is its check run (an Actions job id),
    # which every run attempt mints anew.
    url = "https://github.com/o/r/actions/runs/77/job/10"
    nodes = [report(id="a1", databaseId=10, conclusion="FAILURE", detailsUrl=url),
             report(name="lint", id="b1", databaseId="11", isRequired=False)]
    ci = gateway(pages=[page(nodes)]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert {(c.name, c.run_id, c.check_id) for c in ci.reports} == {("unit", 77, 10), ("lint", None, 11)}


@pytest.mark.parametrize("attempt,observed", [(2, 2), (1, 1), (None, None), (0, None), ("2", None), (True, None)])
def test_an_actions_check_carries_its_runs_attempt(attempt, observed):
    # A re-run is visible in the observation: the harness re-runs a head once (PR #403).
    url = "https://github.com/o/r/actions/runs/77/job/10"
    suite = {"app": {"databaseId": 1}, "workflowRun": {"runAttempt": attempt}}
    other = report(name="lint", id="b1", databaseId=11, isRequired=False, checkSuite=suite)  # no Actions run URL
    nodes = [report(id="a1", databaseId=10, detailsUrl=url, checkSuite=suite), other]
    ci = gateway(pages=[page(nodes)], jobs=[{"jobs": [{"id": 10, "run_attempt": 2, "status": "completed"}]}]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert {c.name: c.run_attempt for c in ci.reports} == {"unit": observed, "lint": None}


# ── a re-run's superseded check runs are not current (owner, PR #403) ───────

RUN_URL = "https://github.com/o/r/actions/runs/77/job/"
SECOND = {"app": {"databaseId": 1}, "workflowRun": {"runAttempt": 2}}


def _rerun_ci(nodes, jobs):
    return gateway(pages=[page(nodes)], jobs=jobs).get_pr(3).ci


def _job(job, attempt, status="completed"):
    return {"id": job, "run_attempt": attempt, "status": status}


def _failed(job, name="unit", **kw):
    return report(name=name, id=f"n{job}", databaseId=job, conclusion="FAILURE", detailsUrl=RUN_URL + str(job),
                  checkSuite=SECOND, **kw)


def test_a_check_run_the_latest_attempt_superseded_is_ignored():
    new = report(id="a2", databaseId=20, detailsUrl=RUN_URL + "20", checkSuite=SECOND)
    ci = _rerun_ci([_failed(10), new], [{"jobs": [_job(10, 1), _job(20, 2)]}])
    assert isinstance(ci, Observed) and [(c.check_id, c.bucket) for c in ci.reports] == [(20, "pass")]
    assert isinstance(reduce_ci(ci), Clear)  # the old attempt's failure no longer counts


@pytest.mark.parametrize("latest", [[], [_job(20, 2, "queued")]])
def test_a_rerun_accepted_whose_jobs_are_not_done_is_pending_not_failed(latest):
    ci = _rerun_ci([_failed(10)], [{"jobs": [_job(10, 1), *latest]}])
    assert isinstance(ci, Observed)
    assert [(c.name, c.bucket, c.check_id, c.run_attempt) for c in ci.reports] == [("unit", "pending", None, 2)]
    assert isinstance(reduce_ci(ci), Waiting)


def test_a_superseded_job_absent_from_a_finished_attempt_is_dropped_not_pending_forever():
    # A matrix that changed on "re-run all jobs": attempt 1's job has no twin in attempt 2.
    ci = _rerun_ci([_failed(10, name="shard-a", isRequired=False)], [{"jobs": [_job(10, 1), _job(20, 2)]}])
    assert isinstance(ci, Observed) and ci.reports == ()


def test_a_partial_rerun_keeps_the_jobs_it_copied_into_the_new_attempt():
    # GitHub copies the jobs a partial re-run does not re-run into the new attempt, under new ids.
    copied = report(name="lint", id="b2", databaseId=21, isRequired=False, detailsUrl=RUN_URL + "21",
                    checkSuite=SECOND)
    ci = _rerun_ci([_failed(20), copied], [{"jobs": [_job(10, 1), _job(11, 1), _job(20, 2), _job(21, 2)]}])
    assert isinstance(ci, Observed) and {c.check_id for c in ci.reports} == {20, 21}


def test_a_check_that_is_no_job_of_its_run_is_kept():
    # A test reporter's check run attached to the workflow's suite: never a job, never superseded.
    ci = _rerun_ci([_failed(99, name="junit report", isRequired=False), _failed(20)],
                   [{"jobs": [_job(10, 1), _job(20, 2)]}])
    assert isinstance(ci, Observed) and {c.check_id for c in ci.reports} == {99, 20}


@pytest.mark.parametrize("jobs", [{}, [], [{}], [{"jobs": None}], [{"jobs": [{"id": "20", "run_attempt": 2}]}],
                                  [{"jobs": [{"id": 0, "run_attempt": 2}]}], [{"jobs": [None]}],
                                  [{"jobs": [{"id": True, "run_attempt": 2}]}], [{"jobs": [{"id": 20}]}],
                                  [{"jobs": [{"id": 20, "run_attempt": "2"}]}]])
def test_malformed_rerun_jobs_make_ci_unavailable(jobs):
    assert isinstance(_rerun_ci([_failed(10)], jobs), Unavailable)


def test_unreadable_rerun_jobs_make_ci_unavailable():
    assert isinstance(_rerun_ci([_failed(10)], None), Unavailable)  # the fake refuses an unexpected read


def test_a_first_attempt_or_an_absent_attempt_reads_no_jobs():
    for suite in ({"app": {"databaseId": 1}, "workflowRun": {"runAttempt": 1}},
                  {"app": {"databaseId": 1}, "workflowRun": None}):
        failed = report(id="a1", databaseId=10, conclusion="FAILURE", detailsUrl=RUN_URL + "10", checkSuite=suite)
        ci = _rerun_ci([failed], None)  # any jobs read would be refused
        assert isinstance(ci, Observed) and [c.check_id for c in ci.reports] == [10]


# ── when a failure's CI run began: what tells a resume from a new try (PR #403) ──


@pytest.mark.parametrize("created,start", [("2026-10-14T01:00:00Z", "2026-10-14T01:00:00Z"), (None, ""), (7, "")])
def test_an_actions_job_begins_when_its_run_did(created, start):
    # A job queued behind `needs` starts late, but it belongs to the run its push caused.
    suite = {"app": {"databaseId": 1}, "workflowRun": {"runAttempt": 1, "createdAt": created}}
    late = report(id="a1", databaseId=10, startedAt="2026-10-14T01:20:00Z", detailsUrl=RUN_URL + "10", checkSuite=suite)
    other = report(name="lint", id="b1", databaseId=11, isRequired=False, startedAt="2026-10-14T01:05:00Z")
    ci = gateway(pages=[page([late, other])]).get_pr(3).ci
    assert isinstance(ci, Observed)
    assert {c.name: c.started_at for c in ci.reports} == {"unit": start, "lint": "2026-10-14T01:05:00Z"}


def test_a_rerun_job_begins_when_its_attempt_did_copies_included():
    jobs = [_job(10, 1), {**_job(20, 2), "created_at": "2026-10-14T02:00:05Z"},
            {**_job(21, 2), "created_at": "2026-10-14T02:00:01Z"}]  # 21 a copy: GitHub created it with the attempt
    copied = report(name="lint", id="b2", databaseId=21, isRequired=False, detailsUrl=RUN_URL + "21", checkSuite=SECOND)
    ci = _rerun_ci([_failed(20), copied], [{"jobs": jobs}])
    assert isinstance(ci, Observed) and {c.started_at for c in ci.reports} == {"2026-10-14T02:00:01Z"}
    unread = _rerun_ci([_failed(20)], [{"jobs": [_job(10, 1), _job(20, 2)]}])  # no created_at: fails toward a new try
    assert isinstance(unread, Observed) and [c.started_at for c in unread.reports] == [""]
