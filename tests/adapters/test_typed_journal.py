from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.typed_journal import TypedJournal
from issue_agent.domain.contracts import RunId
from issue_agent.domain.events import (
    AgentRunAbandoned,
    AgentRunCompleted,
    AgentRunStallCorrected,
    AgentRunStarted,
    Correlation,
    Producer,
    TelemetryEvent,
    TickGated,
    decode_event_line,
)
from issue_agent.domain.projection import fold


def _journal(tmp_path) -> TypedJournal:
    return TypedJournal(
        str(tmp_path / "journal.jsonl"),
        EventLog(
            str(tmp_path / "events-v2.jsonl"),
            Producer("deployment", "owner/repo", "fix", 100, "boot"),
        ),
    )


def test_run_pair_has_first_class_run_and_leg_correlation(tmp_path) -> None:
    journal = _journal(tmp_path)
    correlation = Correlation(
        issue=7,
        dispatch_id="dispatch-1",
        run_id="run-1",
        leg_id="leg-1",
    )
    journal.emit(TelemetryEvent(AgentRunStarted("model", "RESOLVE"), correlation))
    journal.emit(
        TelemetryEvent(
            AgentRunCompleted(
                "done", False, None, 1250, "model", "codex",
                500_000, "USD", "configured_rate_card",
            ),
            correlation,
        )
    )

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    model = fold(events)

    assert events[0].correlation.run_id == "run-1"
    assert events[0].correlation.leg_id == "leg-1"
    assert model.runs["run-1"].completion == events[1]


def test_typed_events_are_single_written_to_the_canonical_feed(tmp_path) -> None:
    journal = _journal(tmp_path)
    journal.emit(TelemetryEvent(TickGated("cap exhausted")))
    journal.emit(
        TelemetryEvent(
            AgentRunStarted("model", "RESOLVE"),
            Correlation(issue=1, run_id="run-1", leg_id="leg-1"),
        )
    )

    lines = (tmp_path / "events-v2.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert all(decode_event_line(line).schema_version == 2 for line in lines)
    assert not (tmp_path / "journal.jsonl").exists()


def test_stall_correction_preserves_the_explicit_corrected_event_id(tmp_path) -> None:
    journal = _journal(tmp_path)
    correlation = Correlation(
        issue=7,
        dispatch_id="dispatch-1",
        run_id="run-1",
        leg_id="leg-1",
    )
    journal.emit(TelemetryEvent(AgentRunStarted("model", "RESOLVE"), correlation))
    journal.emit(
        TelemetryEvent(
            AgentRunCompleted(
                "done", False, None, 1000, "model", "codex",
                0, "USD", "configured_rate_card",
            ),
            correlation,
        )
    )
    journal.emit(
        TelemetryEvent(
            AgentRunStallCorrected("start-1", "stall-1", 60_000),
            correlation,
        )
    )

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    correction = events[-1].payload
    assert isinstance(correction, AgentRunStallCorrected)
    assert correction.corrects == "start-1"
    assert correction.stall_id == "stall-1"


def test_boot_recovery_abandons_persisted_start_idempotently(tmp_path) -> None:
    journal = _journal(tmp_path)
    journal.emit(
        TelemetryEvent(
            AgentRunStarted("model", "RESOLVE"),
            Correlation(
                issue=7,
                dispatch_id="dispatch-1",
                run_id="run-1",
                leg_id="leg-1",
            ),
        )
    )

    assert journal.abandon_run(RunId("run-1"), "dead lease")
    assert journal.abandon_run(RunId("run-1"), "dead lease")

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    corrections = [
        event for event in events if isinstance(event.payload, AgentRunAbandoned)
    ]
    assert len(corrections) == 1
    assert corrections[0].payload.corrects == events[0].event_id


def test_abandonment_emit_failure_is_isolated(tmp_path, monkeypatch) -> None:
    journal = _journal(tmp_path)
    journal.emit(
        TelemetryEvent(
            AgentRunStarted("model", "RESOLVE"),
            Correlation(run_id="run-1"),
        )
    )

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal.events, "emit", fail)

    assert journal.abandon_run(RunId("run-1"), "dead lease") is False
