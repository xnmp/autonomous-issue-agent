"""Behavioral contracts for actionable dashboard transitions and backlog metadata."""

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


def test_actionable_journal_transitions_have_stable_severity_and_targets():
    events = [
        {
            "ts": ts(1),
            "event": "agent_run",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "status": "blocked",
        },
        {
            "ts": ts(2),
            "event": "agent_run",
            "kind": "REVIEW",
            "number": 12,
            "issue": 7,
            "status": "APPROVE",
        },
        {
            "ts": ts(3),
            "event": "action",
            "desc": "clear state for pr #12, issue #7 (merged)",
        },
    ]

    notifications = snapshots.journal_notifications(rows_for(events))

    assert [(item["severity"], item["title"]) for item in notifications] == [
        ("attention", "Issue #7 needs input"),
        ("info", "PR #12 approved"),
        ("info", "Issue #7 completed"),
    ]
    assert notifications[-1]["detail"] == "PR #12 merged"
    assert len({item["id"] for item in notifications}) == 3


def test_budget_timeout_and_escalation_demand_attention():
    events = [
        {
            "ts": ts(1),
            "event": "agent_run",
            "kind": "FIX_CI",
            "number": 12,
            "issue": 7,
            "status": "error_max_budget_usd",
        },
        {
            "ts": ts(2),
            "event": "agent_run",
            "kind": "RESUME",
            "number": 7,
            "issue": 7,
            "status": "timeout",
        },
        {
            "ts": ts(3),
            "event": "escalation",
            "target": "issue",
            "number": 7,
            "issue": 7,
        },
    ]

    notifications = snapshots.journal_notifications(rows_for(events))

    assert [item["detail"] for item in notifications] == [
        "Budget limit reached",
        "Run timed out",
        "Human review is required",
    ]
    assert {item["severity"] for item in notifications} == {"attention"}


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
    ]

    notifications = snapshots.journal_notifications(rows_for(events))

    assert [(item["title"], item["detail"]) for item in notifications] == [
        ("Issue #7 dispatched", "RESOLVE started"),
        ("Issue #7 dispatched", "REVIEW for PR #12 started"),
    ]
    assert all(item["severity"] == "info" for item in notifications)
    assert len({item["id"] for item in notifications}) == 2


def test_duplicate_dispatch_receipts_emit_one_notification():
    dispatch = {
        "ts": ts(1),
        "event": "agent_run_start",
        "role": "fix",
        "kind": "RESOLVE",
        "number": 7,
        "issue": 7,
        "dispatch": "resolve-7",
    }

    notifications = snapshots.journal_notifications(rows_for([
        dispatch,
        dict(dispatch),
    ]))

    assert [item["title"] for item in notifications] == ["Issue #7 dispatched"]


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


def test_unknown_and_malformed_events_fail_open_and_history_is_bounded():
    events = [
        {"ts": "not-a-time", "event": "agent_run", "issue": 7, "status": "blocked"},
        {"ts": ts(1), "event": "future_transition", "issue": 7},
        {
            "ts": ts(2),
            "event": "agent_run",
            "kind": "REVIEW",
            "number": 12,
            "issue": 7,
            "status": "REQUEST_CHANGES",
        },
        {
            "ts": ts(3),
            "event": "agent_run",
            "kind": "REVIEW",
            "number": 12,
            "issue": 7,
            "status": "APPROVE",
        },
    ]

    notifications = snapshots.journal_notifications(rows_for(events), limit=1)

    assert len(notifications) == 1
    assert notifications[0]["title"] == "PR #12 approved"


def test_notification_identity_does_not_change_when_older_events_arrive_late():
    approved = {
        "ts": ts(3),
        "event": "agent_run",
        "kind": "REVIEW",
        "number": 12,
        "issue": 7,
        "status": "APPROVE",
    }
    original = snapshots.journal_notifications(rows_for([approved]))
    with_older_event = snapshots.journal_notifications(rows_for([
        {
            "ts": ts(1),
            "event": "agent_run",
            "kind": "RESOLVE",
            "number": 7,
            "issue": 7,
            "status": "blocked",
        },
        approved,
    ]))

    assert original[0]["id"] == next(
        item["id"] for item in with_older_event if item["title"] == "PR #12 approved"
    )


def test_backlog_metadata_is_derived_from_planning_labels():
    card = snapshots.backlog_card(
        {
            "number": 120,
            "labels": [
                {"name": "feature"},
                {"name": "effort:medium"},
                {"name": "impact:high"},
            ],
        },
        NOW,
    )

    assert (card["issue_type"], card["difficulty"], card["impact"]) == (
        "feature",
        "medium",
        "high",
    )
