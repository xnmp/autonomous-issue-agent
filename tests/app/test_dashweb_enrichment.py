"""First-paint behavior for optional GitHub dashboard enrichment."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from issue_agent.app.dashweb import service
from issue_agent.domain import dashboard as dash


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _snapshot(*events: dict) -> dash.Snapshot:
    feeds = (
        ("fix", "/j/fix", "/l/fix", events),
        ("review", "/j/review", "/l/review", ()),
        ("steward", "/j/steward", "/l/steward", ()),
    )
    deployment = dash.build_deployment(
        "demo",
        "o/r",
        feeds,
        (),
        now_fn=lambda: NOW,
    )
    return dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,))


def _run(kind: str, number: int, *, hours: int) -> dict:
    return {
        "ts": (NOW - timedelta(hours=hours)).isoformat(),
        "event": "agent_run",
        "role": "review" if kind == "REVIEW" else "fix",
        "kind": kind,
        "issue": 7,
        "number": number,
        "cost_usd": 0.2,
        "status": "APPROVE" if kind == "REVIEW" else "done",
        "exit_reason": "ok",
        "model": "gpt-5.6-terra",
    }


class BlockingGh:
    """Network-shaped fake whose release points make blocking observable."""

    def __init__(self) -> None:
        self.issues_started = threading.Event()
        self.prs_started = threading.Event()
        self.pr_started = threading.Event()
        self.base_release = threading.Event()
        self.detail_release = threading.Event()

    def issues(self, _repo: str):
        self.issues_started.set()
        assert self.base_release.wait(2)
        return ([{
            "number": 7,
            "title": "Enriched issue title",
            "url": "https://github.com/o/r/issues/7",
            "state": "OPEN",
            "labels": [],
        }], "")

    def prs(self, _repo: str):
        self.prs_started.set()
        assert self.base_release.wait(2)
        return ([{
            "number": 12,
            "url": "https://github.com/o/r/pull/12",
            "state": "OPEN",
            "headRefName": "fix/7",
            "closingIssuesReferences": [{"number": 7}],
        }], "")

    def pr(self, _repo: str, _number: int):
        self.pr_started.set()
        assert self.detail_release.wait(2)
        return ({
            "number": 12,
            "url": "https://github.com/o/r/pull/12",
            "state": "OPEN",
            "headRefName": "fix/7",
            "closingIssuesReferences": [{"number": 7}],
            "commits": [],
            "files": [],
        }, "")


def _backend(snapshot: dash.Snapshot, gh: BlockingGh) -> service.Backend:
    backend = service.Backend(gh=gh, now_fn=lambda: NOW)
    backend.snapshot = lambda _name: snapshot  # type: ignore[method-assign]
    backend.verification = lambda: {"rows": []}  # type: ignore[method-assign]
    backend._cached_detail_limits = (  # type: ignore[method-assign]
        lambda _name, _repo: ({}, 0)
    )
    backend._review_unproductive_rounds = (  # type: ignore[method-assign]
        lambda _name, _snapshot, row, _prs: (
            {number: 0 for number in row.prs},
            [],
        )
    )
    return backend


def _eventually(load, predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = load()
        if predicate(value):
            return value
        threading.Event().wait(0.01)
    raise AssertionError("background enrichment did not reach the next poll")


def test_issue_list_returns_the_fold_while_cold_github_reads_continue() -> None:
    gh = BlockingGh()
    backend = _backend(_snapshot(_run("RESOLVE", 7, hours=1)), gh)

    started = time.perf_counter()
    first = backend.issues("demo")

    assert time.perf_counter() - started < 0.5
    assert first["in_flight"][0]["title"] is None
    assert first["gh_ok"] is True
    assert gh.issues_started.wait(1)
    assert gh.prs_started.wait(1), "independent gh reads should start in parallel"

    gh.base_release.set()
    enriched = _eventually(
        lambda: backend.issues("demo"),
        lambda payload: payload["in_flight"][0]["title"] is not None,
    )

    assert enriched["in_flight"][0]["title"] == "Enriched issue title"
    assert enriched["in_flight"][0]["url"].endswith("/issues/7")


def test_issue_detail_defers_list_and_per_pr_enrichment_together() -> None:
    gh = BlockingGh()
    snapshot = _snapshot(
        _run("RESOLVE", 7, hours=2),
        _run("REVIEW", 12, hours=1),
    )
    backend = _backend(snapshot, gh)

    started = time.perf_counter()
    first = backend.issue("demo", 7)

    assert first is not None
    assert time.perf_counter() - started < 0.5
    assert first["title"] is None
    assert first["pull_requests"] == [{"number": 12, "url": None, "state": None}]
    assert gh.issues_started.wait(1)
    assert gh.prs_started.wait(1)

    gh.base_release.set()
    assert gh.pr_started.wait(1)
    gh.detail_release.set()
    enriched = _eventually(
        lambda: backend.issue("demo", 7),
        lambda payload: payload is not None and payload["pull_requests"][0]["url"] is not None,
    )

    assert enriched is not None
    assert enriched["title"] == "Enriched issue title"
    assert enriched["pull_requests"][0]["url"].endswith("/pull/12")
