"""CI lifecycle signals reach the HTTP board and detail from the GitHub read."""

from datetime import datetime, timedelta, timezone

from issue_agent.app.dashweb import server, service
from issue_agent.domain import dashboard as dash
from asgi_client import ASGITestClient


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _ts(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def backend(monkeypatch, enrichment):
    events = [
        {"ts": _ts(30), "event": "agent_run", "role": "fix", "kind": "RESOLVE",
         "issue": 7, "number": 7, "cost_usd": 1.0, "status": "ok",
         "exit_reason": "ok", "dispatch": "d1"},
        {"ts": _ts(20), "event": "agent_run", "role": "review", "kind": "REVIEW",
         "issue": 7, "number": 12, "cost_usd": 1.0, "status": "APPROVE",
         "exit_reason": "ok", "dispatch": "d2"},
    ]
    deployment = dash.build_deployment(
        "demo", "o/r", (("fix", "/j", "/l", events),), (), now_fn=lambda: NOW
    )
    result = service.Backend(now_fn=lambda: NOW)
    monkeypatch.setattr(
        result, "snapshot",
        lambda _name: dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,)),
    )
    monkeypatch.setattr(result, "known", lambda _: True)
    monkeypatch.setattr(
        result, "_deferred_gh_context", lambda *_args: (({}, enrichment, []), True)
    )
    monkeypatch.setattr(result, "verification", lambda: {"rows": []})
    monkeypatch.setattr(result, "_cached_detail_limits", lambda *_args: ({}, 0))
    monkeypatch.setattr(service.cli, "deployment_env_field", lambda _name, _field: "")
    return result


def test_http_board_and_detail_report_a_running_check_as_pending(monkeypatch):
    result = backend(monkeypatch, {12: {
        "state": "OPEN",
        "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS"}],
    }})
    http = ASGITestClient(
        server.create_app(backend=result, offload_backend=False),
        base_url="http://127.0.0.1:8321",
    )

    board = http.get("/api/repos/demo/issues").json()
    detail = http.get("/api/repos/demo/issues/7").json()

    assert board["in_flight"][0]["issue"] == 7
    assert board["in_flight"][0]["pr_ci_state"] == "pending"
    assert detail["issue"] == 7 and detail["pr_ci_state"] == "pending"
