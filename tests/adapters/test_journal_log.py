"""The daemon log is one plain JSON object per line, readable without the harness."""

import json
import threading

from issue_agent.adapters.journal import Journal, read_events
from issue_agent.domain.events import Correlation, EscalationRaised, TelemetryEvent, TelemetryWarning


def test_each_record_is_one_self_describing_json_line(tmp_path):
    path = tmp_path / "state" / "journal.jsonl"
    journal = Journal(str(path))

    journal.emit(TelemetryEvent(EscalationRaised("pr", 7), Correlation(pr=7)))
    journal.emit(TelemetryEvent(TelemetryWarning("pricing", "rate card missing"), Correlation()))

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["escalation", "telemetry_warning"]
    assert lines[1]["warning"] == "rate card missing"
    assert lines[0]["number"] == 7 and lines[0]["target"] == "pr"
    assert all(isinstance(line["ts"], str) and line["ts"] for line in lines)
    assert [event["event"] for event in read_events(str(path))] == [line["event"] for line in lines]


def test_an_unwritable_log_drops_the_line_without_raising(tmp_path, capsys):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    journal = Journal(str(blocker / "journal.jsonl"))

    journal.emit(TelemetryEvent(EscalationRaised("issue", 3), Correlation(issue=3)))

    assert "journal: dropped EscalationRaised" in capsys.readouterr().err


def test_concurrent_writers_never_interleave_a_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(str(path))
    huge = "x" * 200_000

    def write(index):
        journal.emit(TelemetryEvent(TelemetryWarning("load", f"{index}:{huge}"), Correlation(issue=index + 1)))

    threads = [threading.Thread(target=write, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert sorted(line["issue"] for line in lines) == list(range(1, 17))
