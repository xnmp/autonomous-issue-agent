"""Dashboard projection coverage for human-input wait spans.

Since the leg-based work/idle partition, a wait on a human needs no
`deferred` bookkeeping to read as idle: any time outside a task is idle by
definition. These tests pin that a declared human wait neither inflates nor
double-counts the partition.
"""

from issue_agent.app.dashweb import snapshots

from test_dashweb_snapshots import NOW, RESOLVE, row_for, ts


def test_a_human_wait_reads_as_idle_without_inflating_working_time():
    events = [
        RESOLVE,
        {"ts": ts(hours=2), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "human", "blocker": "human"},
        {"ts": ts(hours=1), "event": "deferred", "state": "cleared",
         "issue": 7, "reason": "human", "blocker": "human"},
    ]

    detail = snapshots.issue_detail(row_for(events, 7), NOW)

    # RESOLVE reports no runtime, so every second of this issue's life was
    # spent waiting on something — none of it is working time.
    assert detail["working_s"] == 0
    assert detail["idle_s"] == detail["elapsed_s"]
    assert detail["idle_active"] is True


def test_overlapping_waits_cannot_push_idle_past_elapsed():
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

    assert detail["idle_s"] == detail["elapsed_s"]
    assert detail["working_s"] == 0
