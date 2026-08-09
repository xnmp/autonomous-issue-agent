"""The cached read-only gh façade: one call per (repo, query) per TTL, and a
failure that degrades to `(None, error)` instead of raising."""

import gc
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

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
    gh, _calls = reader([("", "gh: not authenticated")])
    payload, error = gh.prs("o/r")
    assert payload is None and error == "gh: not authenticated"


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
