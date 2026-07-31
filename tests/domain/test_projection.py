from datetime import datetime, timezone

from issue_agent.domain.events import (
    AgentRunAbandoned,
    AgentRunCompleted,
    AgentRunStarted,
    Correlation,
    EventEnvelope,
    LevelChanged,
    Producer,
    ProjectionSnapshot,
    SpendRecorded,
    WorldFeedRetired,
    WorldObserved,
)
from issue_agent.domain.projection import fold


def _event(payload, event_id, seq, *, run_id=None, role="fix"):
    stamp = datetime(2026, 7, 30, tzinfo=timezone.utc)
    return EventEnvelope(
        payload,
        event_id,
        seq,
        stamp,
        stamp,
        seq,
        Producer("test", "o/r", role, 10, "boot"),
        Correlation(run_id=run_id),
    )


def test_fold_is_duplicate_safe_and_joins_runs_by_run_id() -> None:
    start = _event(AgentRunStarted("m"), "a", 0, run_id="run")
    complete = _event(
        AgentRunCompleted("ok", False, None, 1, "m", "codex"),
        "b",
        1,
        run_id="run",
    )
    model = fold([complete, start, complete])
    assert model.runs["run"].completion == complete
    assert len(model.applied_event_ids) == 2


def test_duplicate_start_does_not_erase_a_completed_run() -> None:
    first = _event(AgentRunStarted("m"), "start-1", 0, run_id="run")
    complete = _event(
        AgentRunCompleted("ok", False, None, 1, "m", "codex"),
        "complete",
        1,
        run_id="run",
    )
    duplicate = _event(
        AgentRunStarted("m"), "start-2", 2, run_id="run"
    )

    projected = fold((first, complete, duplicate)).runs["run"]

    assert projected.start == first
    assert projected.completion == complete


def test_completion_without_start_remains_in_the_read_model() -> None:
    complete = _event(
        AgentRunCompleted("ok", False, None, 1, "m", "codex"),
        "complete",
        1,
        run_id="run",
    )

    projected = fold((complete,)).runs["run"]

    assert projected.start is None
    assert projected.completion == complete


def test_abandonment_corrects_the_specific_start_event() -> None:
    start = _event(AgentRunStarted("m"), "start", 0, run_id="run")
    abandoned = _event(AgentRunAbandoned("start", "dead lease"), "end", 1)
    assert fold([abandoned, start]).runs["run"].abandoned == abandoned


def test_levels_close_by_shared_state_key_and_spend_splits_by_basis() -> None:
    opened = _event(LevelChanged("issue:1", "waiting", "dependency"), "1", 1)
    closed = _event(LevelChanged("issue:1", "cleared", "ready"), "2", 2)
    spend = _event(
        SpendRecorded("run", 125, "USD", "reconciled", None, None), "3", 3
    )
    model = fold([closed, spend, opened])
    assert model.levels == {}
    assert model.spend_micros_by_basis == {"reconciled": 125}


def test_projection_snapshot_replaces_levels_before_later_changes() -> None:
    snapshot = _event(
        ProjectionSnapshot(
            {
                "issue:1": {"state": "waiting"},
                "issue:stale": {"state": "waiting"},
            }
        ),
        "snapshot",
        1,
    )
    cleared = _event(
        LevelChanged("issue:stale", "cleared", "ready"), "clear", 2
    )
    opened = _event(
        LevelChanged("issue:2", "waiting", "dependency"), "open", 3
    )

    assert set(fold([opened, snapshot, cleared]).levels) == {
        "issue:1",
        "issue:2",
    }


def test_projection_snapshot_replaces_only_its_own_feed_levels() -> None:
    fix_open = _event(
        LevelChanged("fix:issue:1", "waiting", "dependency"),
        "fix-open",
        1,
    )
    review_open = _event(
        LevelChanged("review:pr:2", "waiting", "review"),
        "review-open",
        2,
        role="review",
    )
    review_snapshot = _event(
        ProjectionSnapshot({"review:pr:2": {"state": "waiting"}}),
        "review-snapshot",
        3,
        role="review",
    )

    assert set(fold([fix_open, review_open, review_snapshot]).levels) == {
        "fix:issue:1",
        "review:pr:2",
    }


def test_cleanly_stopped_feed_cannot_keep_newer_tombstone_present() -> None:
    present = _event(
        WorldObserved(
            "issue",
            7,
            "present",
            {"present": True, "state": "OPEN"},
        ),
        "present",
        1,
    )
    retired = _event(
        WorldFeedRetired("test", "o/r", "fix", restartable=True),
        "retired",
        2,
    )
    tombstone = _event(
        WorldObserved(
            "issue",
            7,
            "absent",
            {"present": False, "reason": "absent_from_feed"},
        ),
        "absent",
        3,
        role="steward",
    )

    model = fold((present, retired, tombstone))

    assert model.world_facts[("issue", 7)] == {"present": False}
    assert all(key[2] != "fix" for key in model.observed_by_feed)


def test_restarted_feed_reactivates_stable_deployment_identity() -> None:
    present = _event(
        WorldObserved("issue", 7, "old", {"present": True}),
        "old",
        1,
    )
    retired = _event(
        WorldFeedRetired("test", "o/r", "fix", restartable=True),
        "retired",
        2,
    )
    restarted = _event(
        WorldObserved(
            "issue",
            8,
            "new",
            {"present": True, "state": "OPEN"},
        ),
        "new",
        3,
    )

    model = fold((present, retired, restarted))

    assert ("issue", 7) not in model.world_facts
    assert model.world_facts[("issue", 8)] == {
        "present": True,
        "state": "OPEN",
    }
