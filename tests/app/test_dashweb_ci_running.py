"""CI lifecycle signals come from observed checks, without a GitHub read."""

from tests.domain.builders import observed_pr, with_checks

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from issue_agent.app.dashweb import service
from issue_agent.domain import dashboard as dash
from issue_agent.domain.events import Correlation, EventEnvelope, Producer, WorldObserved, WorldFeedRetired
from issue_agent.domain.projection import fold
from issue_agent.domain.labels import Label
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.event_log import EventLog
from issue_agent.app.config import from_env
from issue_agent.domain.events import decode_event_line
from issue_agent.domain.world import Check, IssueView, PRView, WorldState
from issue_agent.app.dashweb import server
from asgi_client import ASGITestClient


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def check(state="IN_PROGRESS", bucket="pending", **overrides):
    return {"name": "build", "state": state, "bucket": bucket,  **overrides}


def observation(kind, number, facts, seq, role="fix"):
    return EventEnvelope(
        WorldObserved(kind, number, str(seq), facts), str(seq), seq,
        NOW + timedelta(seconds=seq), NOW + timedelta(seconds=seq), seq,
        Producer("demo", "o/r", role, 10, "boot"), Correlation(issue=7, pr=12 if kind == "pr" else None),
    )


def backend(monkeypatch, checks, *, extra=(), ignore="", enrichment=None):
    issue = observation("issue", 7, {"present": True, "state": "OPEN", "labels": ["ai-working"]}, 0)
    events = [issue]
    if checks is not None:
        events.append(observation("pr", 12, {
            "present": True, "closing_issue": 7, "state": "OPEN", "head_sha": "current",
            "checks_known": True, "checks_total": len(checks), "checks": checks,
        }, 1))
    events.extend(extra)
    deployment = dash.build_deployment_v2("demo", "o/r", (),
        fold(events, ), lambda: NOW)
    result = service.Backend(now_fn=lambda: NOW)
    monkeypatch.setattr(result, "snapshot", lambda _name: dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,)))
    monkeypatch.setattr(result, "known", lambda _: True)
    monkeypatch.setattr(result, "_deferred_gh_context", lambda *_args: (({}, enrichment or {}, []), True))
    monkeypatch.setattr(result, "verification", lambda: {"rows": []})
    monkeypatch.setattr(result, "_cached_detail_limits", lambda *_args: ({}, 0))
    monkeypatch.setattr(service.cli, "deployment_env_field", lambda _name, _field: ignore)
    return result


def signal(result):
    listed = result.issues("demo")["in_flight"][0]["pr_ci_state"]
    detailed = result.issue("demo", 7)["pr_ci_state"]
    assert listed == detailed
    return listed



def assert_http_signal(result, expected, group="in_flight"):
    http = ASGITestClient(server.create_app(backend=result, offload_backend=False),
        base_url="http://127.0.0.1:8321")
    listed = http.get("/api/repos/demo/issues").json()[group][0]["pr_ci_state"]
    detailed = http.get("/api/repos/demo/issues/7").json()["pr_ci_state"]
    assert (listed, detailed) == (expected, expected)



def test_legacy_http_board_and_detail_preserve_running_activity(monkeypatch):
    result = backend(monkeypatch, [check()], ignore="[", enrichment={12: {
        "state": "OPEN", "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS"}],
    }})
    snap = result.snapshot("demo")
    legacy = replace(snap, deployment=replace(snap.deployment, schema_version=1,
        issues=tuple(replace(row, facts={}) for row in snap.deployment.issues)))
    monkeypatch.setattr(result, "snapshot", lambda _: legacy)
    http = ASGITestClient(server.create_app(backend=result, offload_backend=False), base_url="http://127.0.0.1:8321")
    board = http.get("/api/repos/demo/issues").json()
    detail = http.get("/api/repos/demo/issues/7").json()
    assert board["in_flight"][0]["issue"] == 7
    assert board["in_flight"][0]["pr_ci_state"] == "pending"
    assert detail["issue"] == 7 and detail["pr_ci_state"] == "pending"








@pytest.mark.parametrize("summary", [None, [], 7, {}, {"state": "pending"}])
def test_malformed_ci_summary_falls_back_to_legacy_checks(monkeypatch, summary):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks": [check()], "ci_summary": summary}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), "pending")



@pytest.mark.parametrize("checks", [None, {}, [7], [{}], [check(name=None)], [check(name="")]])
def test_malformed_known_checks_claim_no_ci(monkeypatch, checks):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks": checks, "ci_state": "pending"}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), None)



@pytest.mark.parametrize("checks,expected", [([check()], "pending"), ([check("SUCCESS", "pass")], None)])
def test_truncated_observation_cannot_claim_ci_settlement(monkeypatch, checks, expected):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks_total": 101, "checks": checks, "observation_truncated": True}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), expected)



@pytest.mark.parametrize("state", [None, [], {}, 7, "invalid"])
def test_unreadable_or_historical_ci_claims_no_indicator(monkeypatch, state):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "ci_state": state}, 1)
    assert signal(backend(monkeypatch, None, extra=(event,))) is None



