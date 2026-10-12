"""The cached read-only gh façade: one call per (repo, query) per TTL, and a
failure that degrades to `(None, error)` instead of raising."""

import gc
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from issue_agent.adapters.dashweb import ghdata


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def reader(responses, clock=None, ttl_s=45.0, pr_ttl_s=180.0):
    calls = []

    def run(argv):
        calls.append(argv)
        return responses.pop(0) if responses else ("[]", "")

    return ghdata.GhReader(
        run=run,
        ttl_s=ttl_s,
        clock=clock or Clock(),
        pr_ttl_s=pr_ttl_s,
    ), calls


def test_repeated_reads_inside_the_ttl_shell_out_once():
    gh, calls = reader([(json.dumps([{"number": 1}]), "")])
    for _ in range(3):
        payload, error = gh.issues("o/r")
    assert len(calls) == 1
    assert error == "" and payload == [{"number": 1}]


def test_issue_history_requests_closed_dates_and_warnable_long_history_limit():
    gh, calls = reader([("[]", "")])
    gh.issues("o/r")
    argv = calls[0]
    assert "closedAt" in argv[argv.index("--json") + 1]
    assert "body" in argv[argv.index("--json") + 1]
    assert argv[argv.index("--limit") + 1] == str(ghdata.ISSUE_HISTORY_LIMIT)


def test_the_cache_expires_after_the_ttl():
    clock = Clock()
    gh, calls = reader([('[]', ""), ('[]', "")], clock=clock, ttl_s=45.0)
    gh.issues("o/r")
    clock.t = 46.0
    gh.issues("o/r")
    assert len(calls) == 2


def test_concurrent_cold_misses_share_one_gh_invocation():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def run(argv):
        calls.append(argv)
        entered.set()
        assert release.wait(timeout=2)
        return '[{"number": 1}]', ""

    gh = ghdata.GhReader(run=run, clock=Clock())
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(gh.issues, "o/r") for _ in range(12)]
        assert entered.wait(timeout=2)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert len(calls) == 1
    assert results == [([{"number": 1}], "")] * 12


def test_a_write_releases_expired_values_from_other_keys():
    class Payload:
        pass

    clock = Clock()
    cache = ghdata.TtlCache(ttl_s=45.0, clock=clock)
    payload = Payload()
    retained = weakref.ref(payload)
    cache.put("old", payload)
    del payload

    clock.t = 46.0
    cache.put("new", "live")
    gc.collect()

    assert retained() is None


def test_different_repos_do_not_share_a_cache_entry():
    gh, calls = reader([('[{"number": 1}]', ""), ('[{"number": 2}]', "")])
    assert gh.issues("o/a")[0] == [{"number": 1}]
    assert gh.issues("o/b")[0] == [{"number": 2}]
    assert len(calls) == 2


def test_the_repo_is_always_passed_to_gh_explicitly():
    gh, calls = reader([("[]", "")])
    gh.issues("o/r")
    assert calls[0][-2:] == ["-R", "o/r"]


def test_pr_detail_fetches_change_history_without_expanding_the_list_query():
    gh, calls = reader([(json.dumps({"number": 12, "commits": []}), "")])

    payload, error = gh.pr("o/r", 12)

    assert error == "" and payload == {"number": 12, "commits": []}
    argv = calls[0]
    assert argv[:3] == ["pr", "view", "12"]
    fields = argv[argv.index("--json") + 1]
    assert "commits" in fields and "headRefName" in fields and "files" in fields


def test_pr_detail_uses_the_longer_change_history_ttl():
    clock = Clock()
    gh, calls = reader(
        [(json.dumps({"number": 12}), ""), (json.dumps({"number": 12}), "")],
        clock=clock,
        ttl_s=45.0,
        pr_ttl_s=180.0,
    )

    gh.pr("o/r", 12)
    clock.t = 179.0
    gh.pr("o/r", 12)
    assert len(calls) == 1

    clock.t = 181.0
    gh.pr("o/r", 12)
    assert len(calls) == 2


def test_a_gh_failure_degrades_to_an_error_string():
    gh = ghdata.GhReader(run=lambda _argv: ("", "gh: not authenticated"))
    payload, error = gh.prs("o/r")
    assert payload is None and error == "gh: not authenticated"


def test_pr_reads_run_in_parallel_and_keep_full_open_facts_over_light_history():
    rendezvous = threading.Barrier(2)
    calls = []
    light = {"number": 12, "state": "OPEN", "title": "light title"}
    full = {**light, "title": "full title", "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "comments": [{"body": "reply"}], "reviews": [{"state": "APPROVED"}]}
    merged = {"number": 13, "state": "MERGED", "mergedAt": "2026-10-12T00:00:00Z",
              "reviewDecision": "APPROVED", "mergeable": "MERGEABLE"}
    closed = {"number": 14, "state": "CLOSED", "labels": [{"name": "needs-human-review"}]}

    def run(argv):
        calls.append(argv)
        rendezvous.wait(timeout=2)
        return json.dumps([full] if argv[argv.index("--state") + 1] == "open"
                          else [light, merged, closed]), ""

    gh = ghdata.GhReader(run=run)
    for _ in range(2):
        payload, error = gh.prs("o/r")
        assert error == ""
        assert ghdata.index_by_number(payload) == {12: full, 13: merged, 14: closed}
    assert len(calls) == 2
    by_state = {argv[argv.index("--state") + 1]: argv for argv in calls}
    assert set(by_state) == {"open", "all"}
    for state, argv in by_state.items():
        assert argv[argv.index("--limit") + 1] == "200"
        assert argv[-2:] == ["-R", "o/r"]
        fields = set(argv[argv.index("--json") + 1].split(","))
        if state == "all":
            assert fields == {"number", "title", "url", "state", "mergedAt", "updatedAt",
                              "labels", "headRefName", "headRefOid", "closingIssuesReferences",
                              "reviewDecision", "mergeable"}
        else:
            assert {"comments", "statusCheckRollup", "latestReviews", "reviews"} <= fields


@pytest.mark.parametrize("failed_state", ["open", "all"])
@pytest.mark.parametrize("failure", [("", "HTTP 504"), ("not json", "")])
def test_either_pr_read_failure_discards_the_pr_payload(failed_state, failure):
    def run(argv):
        return failure if argv[argv.index("--state") + 1] == failed_state else ('[{"number": 12}]', "")

    payload, error = ghdata.GhReader(run=run).prs("o/r")
    assert payload is None
    assert error == (failure[1] or "gh returned unparseable JSON")


def test_pr_history_at_the_limit_keeps_the_existing_no_warning_contract():
    def run(argv):
        rows = [] if argv[argv.index("--state") + 1] == "open" else [
            {"number": number, "state": "MERGED"} for number in range(1, 201)
        ]
        return json.dumps(rows), ""

    payload, error = ghdata.GhReader(run=run).prs("o/r")
    assert isinstance(payload, list) and len(payload) == 200
    assert error == ""


def test_a_failure_is_cached_too_so_a_down_gh_is_not_hammered():
    gh, calls = reader([("", "gh down"), ("[]", "")])
    gh.issues("o/r")
    gh.issues("o/r")
    assert len(calls) == 1


def test_unparseable_json_is_an_error_not_an_exception():
    gh, _calls = reader([("not json", "")])
    payload, error = gh.issues("o/r")
    assert payload is None and "unparseable" in error


def test_a_deployment_with_no_repo_configured_never_shells_out():
    gh, calls = reader([("[]", "")])
    payload, error = gh.issues("")
    assert calls == [] and payload is None and error


def test_indexing_drops_unusable_entries_instead_of_failing_the_view():
    indexed = ghdata.index_by_number(
        [{"number": 1, "title": "a"}, {"title": "no number"}, "junk", {"number": "x"}]
    )
    assert list(indexed) == [1]
    assert ghdata.index_by_number(None) == {}
    assert ghdata.index_by_number({"number": 1}) == {}


def test_open_state_is_read_case_insensitively():
    assert ghdata.is_open({"state": "OPEN"}) is True
    assert ghdata.is_open({"state": "closed"}) is False
    assert ghdata.is_open({}) is False


def test_run_gh_reports_a_missing_binary_rather_than_raising(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    out, error = ghdata.run_gh(["issue", "list"])
    assert out == "" and "not installed" in error
