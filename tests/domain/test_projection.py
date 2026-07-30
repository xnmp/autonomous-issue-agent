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
