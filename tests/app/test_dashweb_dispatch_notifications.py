"""Behavioral contracts for dashboard dispatch notifications."""

from datetime import datetime, timedelta, timezone

from issue_agent.app.dashweb import snapshots
from issue_agent.domain import dashboard as dash

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def ts(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def rows_for(events):
    feeds = (
        ("fix", "/j/fix", "/l/fix", events),
        ("review", "/j/review", "/l/review", []),
        ("steward", "/j/steward", "/l/steward", []),
    )
    return dash.build_deployment(
        "demo",
        "owner/repo",
        feeds,
        (),
        now_fn=lambda: NOW + timedelta(hours=1),
    ).issues


def test_agent_run_start_announces_each_new_dispatch():
    events = [
        {
            "ts": ts(1),
            "event": "agent_run_start",
            "role": "fix",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "dispatch": "resolve-7",
        },
        {
            "ts": ts(2),
            "event": "agent_run_start",
            "role": "review",
            "kind": "REVIEW",
            "number": 12,
            "issue": 7,
            "dispatch": "review-12",
        },
        {
            "ts": ts(3),
            "event": "agent_run_start",
            "role": "fix",
            "kind": "PR_RESUME",
            "number": 12,
            "issue": 7,
            "dispatch": "pr-resume-12",
        },
        {
            "ts": ts(4),
            "event": "agent_run_start",
            "role": "steward",
            "kind": "GROOM",
            "number": 7,
            "issue": 7,
            "dispatch": "groom-7",
        },
    ]

    notifications = snapshots.journal_notifications(rows_for(events))

    assert [(item["title"], item["detail"]) for item in notifications] == [
        ("Issue #7 dispatched", "RESOLVE started"),
        ("Issue #7 dispatched", "REVIEW for PR #12 started"),
        ("Issue #7 dispatched", "PR_RESUME for PR #12 started"),
        ("Issue #7 dispatched", "GROOM started"),
    ]
    assert all(item["severity"] == "info" for item in notifications)
    assert len({item["id"] for item in notifications}) == 4


def test_distinct_legs_for_one_dispatch_emit_one_notification():
    shared = {
        "event": "agent_run_start",
        "role": "fix",
        "number": 7,
        "issue": 7,
        "dispatch": "resolve-7",
    }
    events = [
        {**shared, "ts": ts(1), "kind": "ROUTE", "model": "router"},
        {**shared, "ts": ts(2), "kind": "RESOLVE", "model": "resolver"},
        {**shared, "ts": ts(3), "kind": "RESOLVE", "model": "resolver-retry"},
    ]

    notifications = snapshots.journal_notifications(rows_for(events))

    assert [item["title"] for item in notifications] == ["Issue #7 dispatched"]
    assert notifications[0]["detail"] == "RESOLVE started"


def test_malformed_dispatch_receipts_fail_open():
    valid = {
        "ts": ts(1),
        "event": "agent_run_start",
        "role": "fix",
        "kind": "RESOLVE",
        "number": 7,
        "issue": 7,
        "dispatch": "resolve-7",
    }
    malformed = []
    # The deployment builder restores an absent role from its source feed;
    # fields with no authoritative envelope fallback must still fail open.
    for field in ("kind", "number", "dispatch"):
        event = {**valid, "ts": ts(len(malformed) + 2)}
        event.pop(field)
        malformed.append(event)
    malformed.extend([
        {**valid, "ts": ts(6), "kind": "UNKNOWN"},
        {**valid, "ts": ts(7), "role": "future-role"},
        {**valid, "ts": ts(8), "number": "not-a-number"},
    ])

    notifications = snapshots.journal_notifications(rows_for(malformed))

    assert notifications == []
