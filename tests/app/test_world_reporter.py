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
from issue_agent.domain.world import (
    Comment,
    IssueView,
    PRView,
    Review,
    WorldState,
)


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


def test_world_wrapper_emits_feed_tombstone_once_and_reappearance(
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
            WorldState(),
            WorldState(),
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
        and event.payload.subject_kind == "issue"
    ]
    assert [event.facts["present"] for event in observed] == [
        True,
        False,
        True,
    ]
    assert observed[1].facts["reason"] == "absent_from_feed"


def test_world_wrapper_includes_bounded_issue_and_pr_interactions(
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
    comments = tuple(
        Comment(f"user-{index}", "x" * 3_000, f"t-{index}")
        for index in range(25)
    )
    reviews = tuple(
        Review("COMMENTED", f"reviewer-{index}", f"r-{index}", "y" * 3_000)
        for index in range(25)
    )
    observe = _reported_world(
        lambda: WorldState(
            issues=(IssueView(7, comments=comments),),
            prs=(PRView(8, comments=comments, reviews=reviews),),
        ),
        control,
        HarnessConfig(repo="o/r", target_dir=str(tmp_path)),
        "fix",
        lambda _message: None,
    )

    observe()

    payloads = [
        event.payload
        for event in (
            decode_event_line(line)
            for line in event_path.read_text().splitlines()
        )
        if isinstance(event.payload, WorldObserved)
    ]
    issue = next(p for p in payloads if p.subject_kind == "issue")
    pr = next(p for p in payloads if p.subject_kind == "pr")
    assert issue.facts["comments_total"] == 25
    assert len(issue.facts["comments"]) == 20
    assert issue.facts["comments"][0]["author"] == "user-5"
    assert len(issue.facts["comments"][0]["body"]) == 750
    assert pr.facts["reviews_total"] == 25
    assert len(pr.facts["reviews"]) == 20
    assert pr.facts["reviews"][0]["author"] == "reviewer-5"
