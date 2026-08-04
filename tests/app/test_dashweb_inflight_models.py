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


def test_done_model_column_still_excludes_reviewer_only_models():
    card = snapshots.issue_card(row_for(events(done=True)), NOW)

    assert card["models"] == ["gpt-5.6-terra"]


def test_a_reviewer_model_is_shown_when_it_also_ran_a_fixer_leg():
    rows = events(done=True)
    rows.insert(1, {
        "ts": "2026-07-30T10:30:00Z",
        "event": "agent_run",
        "role": "fix",
        "kind": "FIX_CI",
        "number": 12,
        "issue": 7,
        "model": "claude-opus-5",
        "effort": "medium",
        "status": "done",
    })
    rows[2]["effort"] = "high"

    card = snapshots.issue_card(row_for(rows), NOW)

    assert card["models"] == ["gpt-5.6-terra", "claude-opus-5"]
    assert card["model_badges"][-1] == {
        "model": "claude-opus-5",
        "effort": "medium",
    }


def test_fixer_model_is_recovered_from_start_when_old_settlement_omits_it():
    rows = events(done=False)
    rows[0].pop("model")
    rows.insert(0, {
        "ts": "2026-07-30T09:59:00Z",
        "event": "agent_run_start",
        "role": "fix",
        "kind": "RESOLVE",
        "number": 7,
        "issue": 7,
        "dispatch": "legacy-fix",
        "model": "gpt-5.6-terra",
    })
    rows[1]["dispatch"] = "legacy-fix"

    card = snapshots.issue_card(row_for(rows), NOW)

    assert card["models"] == ["gpt-5.6-terra"]
