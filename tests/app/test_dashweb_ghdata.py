"""The cached read-only gh façade: one call per (repo, query) per TTL, and a
failure that degrades to `(None, error)` instead of raising."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor


from issue_agent.adapters.dashweb import ghdata


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def reader(responses, clock=None, ttl_s=45.0):
    calls = []

    def run(argv):
        calls.append(argv)
        return responses.pop(0) if responses else ("[]", "")

    return ghdata.GhReader(run=run, ttl_s=ttl_s, clock=clock or Clock()), calls


def test_repeated_reads_inside_the_ttl_shell_out_once():
    gh, calls = reader([(json.dumps([{"number": 1}]), "")])
    for _ in range(3):
        payload, error = gh.merged_prs("o/r")
    assert len(calls) == 1
    assert error == "" and payload == [{"number": 1}]


def test_only_open_issues_carry_body_and_comments():
    # 2026-10-13: body+comments for 1000 issues took ~28s and hit the 30s gh timeout.
    history = [{"number": 1, "state": "CLOSED"}, {"number": 2, "state": "OPEN"}]
    gh, calls = reader([(json.dumps(history), ""), (json.dumps([{"number": 2, "state": "OPEN", "body": "b"}]), "")])
    payload, error = gh.issues("o/r")
    assert error == "" and payload == [{"number": 1, "state": "CLOSED"}, {"number": 2, "state": "OPEN", "body": "b"}]
    fields = [argv[argv.index("--json") + 1].split(",") for argv in calls]
    assert "body" not in fields[0] and "comments" not in fields[0]
    assert {"body", "comments"} <= set(fields[1]) and "open" in calls[1]


def test_a_failed_open_issue_read_fails_the_issue_read():
    gh, _calls = reader([("[]", ""), ("", "gh timed out after 30s")])
    assert gh.issues("o/r") == (None, "gh timed out after 30s")


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
        futures = [pool.submit(gh.merged_prs, "o/r") for _ in range(12)]
        assert entered.wait(timeout=2)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert len(calls) == 1
    assert results == [([{"number": 1}], "")] * 12


def test_the_repo_is_always_passed_to_gh_explicitly():
    gh, calls = reader([("[]", "")])
    gh.issues("o/r")
    assert calls[0][-2:] == ["-R", "o/r"]


def test_a_gh_failure_degrades_to_an_error_string():
    gh = ghdata.GhReader(run=lambda _argv: ("", "gh: not authenticated"))
    payload, error = gh.merged_prs("o/r")
    assert payload is None and error == "gh: not authenticated"


def test_labels_read_from_objects_or_strings():
    assert ghdata.labels_of({"labels": [{"name": "a"}, "b", {"name": ""}, None]}) == ["a", "b"]


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
        [{"number": 1, "title": "a"}, {"title": "no number"}, "junk", {"number": "x"}, {"number": 0}]
    )
    assert list(indexed) == [1]
    assert ghdata.index_by_number(None) == {}
    assert ghdata.index_by_number({"number": 1}) == {}


def test_run_gh_reports_a_missing_binary_rather_than_raising(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    out, error = ghdata.run_gh(["issue", "list"])
    assert out == "" and "not installed" in error
