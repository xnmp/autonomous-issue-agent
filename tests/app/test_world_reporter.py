from pathlib import Path

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.event_log import EventLog
from issue_agent.app.config import HarnessConfig
from issue_agent.app.roles import _reported_world
from issue_agent.domain.events import (
    Producer,
    WorldObserved,
    decode_event_line,
)
from issue_agent.domain.world import IssueView, WorldState


def test_production_world_wrapper_publishes_to_the_bound_projection_feed(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events-v2.jsonl"
    events = EventLog(
        str(event_path),
        Producer("deployment", "o/r", "fix", 123, "boot"),
    )
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    control.bind_event_outbox(events.prepare, events.append_committed)
    worlds = iter(
        (
            WorldState(issues=(IssueView(7, state="open"),)),
            WorldState(issues=(IssueView(7, state="open"),)),
            WorldState(issues=(IssueView(7, state="closed"),)),
            WorldState(issues=(IssueView(7, state="open"),)),
        )
    )
    observe = _reported_world(
        lambda: next(worlds),
        control,
        HarnessConfig(repo="o/r", target_dir=str(tmp_path)),
        "fix",
        lambda _message: None,
    )

    for _ in range(4):
        observe()

    observed = [
        event.payload
        for event in (
            decode_event_line(line)
            for line in event_path.read_text().splitlines()
        )
        if isinstance(event.payload, WorldObserved)
    ]
    assert [event.facts["state"] for event in observed] == [
        "open",
        "closed",
        "open",
    ]
