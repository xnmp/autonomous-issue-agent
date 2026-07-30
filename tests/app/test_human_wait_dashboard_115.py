"""Dashboard projection coverage for human-input wait spans."""

from issue_agent.app.dashweb import snapshots

from test_dashweb_snapshots import NOW, RESOLVE, row_for, ts


def test_human_wait_is_idle_until_reply_driven_resume():
    events = [
        RESOLVE,
        {"ts": ts(hours=2), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "human", "blocker": "human"},
        {"ts": ts(hours=1), "event": "deferred", "state": "cleared",
         "issue": 7, "reason": "human", "blocker": "human"},
    ]

    detail = snapshots.issue_detail(row_for(events, 7), NOW)

    assert detail["idle_s"] == 3_600
    assert detail["working_s"] == detail["elapsed_s"] - 3_600
    assert detail["idle_active"] is False


def test_human_and_dependency_waits_are_unioned_not_double_counted():
    events = [
        RESOLVE,
        {"ts": ts(hours=3), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "human", "blocker": "human"},
        {"ts": ts(hours=2.5), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "dependency", "blocker_pr": 90},
        {"ts": ts(hours=2), "event": "deferred", "state": "cleared",
         "issue": 7, "reason": "human", "blocker": "human"},
        {"ts": ts(hours=1.5), "event": "deferred", "state": "cleared",
         "issue": 7, "reason": "dependency", "blocker_pr": 90},
    ]

    detail = snapshots.issue_detail(row_for(events, 7), NOW)

    assert detail["idle_s"] == 5_400
    assert detail["working_s"] == detail["elapsed_s"] - 5_400
