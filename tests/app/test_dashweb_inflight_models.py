"""Model-column contracts for active and completed issue cards."""

from datetime import datetime, timezone

from issue_agent.app.dashweb import snapshots
from issue_agent.domain import dashboard as dash

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def row_for(events):
    feeds = (
        ("fix", "/j/fix", "/l/fix", events),
        ("review", "/j/review", "/l/review", []),
        ("steward", "/j/steward", "/l/steward", []),
    )
    deployment = dash.build_deployment(
        "demo",
        "owner/repo",
        feeds,
        (),
        now_fn=lambda: NOW,
    )
    return deployment.issues[0]


def events(done: bool):
    rows = [
        {
            "ts": "2026-07-30T10:00:00Z",
            "event": "agent_run",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "model": "gpt-5.6-terra",
            "status": "done",
        },
        {
            "ts": "2026-07-30T11:00:00Z",
            "event": "agent_run",
            "role": "review",
            "kind": "REVIEW",
            "number": 12,
            "issue": 7,
            "model": "claude-opus-5",
            "status": "APPROVE",
        },
    ]
    if done:
        rows.append({
            "ts": "2026-07-30T11:30:00Z",
            "event": "action",
            "desc": "clear state for pr #12, issue #7 (merged)",
        })
    return rows


def test_inflight_model_column_excludes_reviewer_only_models():
    card = snapshots.issue_card(row_for(events(done=False)), NOW)

    assert card["models"] == ["gpt-5.6-terra"]
    assert [badge["model"] for badge in card["model_badges"]] == ["gpt-5.6-terra"]


def test_done_model_history_keeps_reviewer_models():
    card = snapshots.issue_card(row_for(events(done=True)), NOW)

    assert card["models"] == ["gpt-5.6-terra", "claude-opus-5"]
