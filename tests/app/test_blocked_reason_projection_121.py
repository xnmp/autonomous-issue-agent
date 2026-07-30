"""Dashboard projection regressions for persisted blocked reasons."""

from issue_agent.app.dashweb import snapshots

from test_dashweb_snapshots import NOW, RESOLVE, row_for, ts


def test_blocked_reason_round_trips_to_card_and_detail_as_one_bounded_line():
    events = [
        RESOLVE,
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "RESUME", "issue": 7, "number": 7, "status": "blocked",
         "needs": "Choose endpoint A or B.\nThe remaining text belongs in drill-down."},
        {"ts": ts(minutes=59), "event": "action",
         "desc": "relabel issue #7: -ai-working +ai-blocked"},
    ]
    row = row_for(events, 7)

    card = snapshots.issue_card(row, NOW)
    detail = snapshots.issue_detail(row, NOW)
    assert card["attention_reason"] == "Choose endpoint A or B."
    assert detail["attention_reason"] == "Choose endpoint A or B."


def test_historical_blocked_run_without_reason_stays_renderable():
    row = row_for([
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "status": "blocked"},
    ], 7)

    card = snapshots.issue_card(row, NOW)
    assert card["stage"] == "awaiting human reply"
    assert card["attention_reason"] is None
