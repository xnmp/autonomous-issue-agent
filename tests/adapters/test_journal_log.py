"""The daemon log is one plain JSON object per line, readable without the harness."""

import json
import threading

from issue_agent.adapters.journal import Journal, read_events, read_events_tail
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


def test_a_record_with_embedded_newlines_is_still_exactly_one_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    body = "line one\nline two\r\n\nline four"
    journal = Journal(str(path))

    journal.emit(TelemetryEvent(TelemetryWarning("w", body), Correlation()))
    journal.emit(TelemetryEvent(EscalationRaised("pr", 7), Correlation(pr=7)))

    raw = path.read_text()
    assert raw.count("\n") == 2
    assert json.loads(raw.splitlines()[0])["warning"] == body


def test_the_journal_is_private_to_the_operator(tmp_path):
    path = tmp_path / "state" / "journal.jsonl"

    Journal(str(path)).emit(TelemetryEvent(EscalationRaised("pr", 7), Correlation(pr=7)))

    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_the_tail_reader_stops_at_its_bound_and_keeps_the_newest_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("".join(json.dumps({"event": "e", "n": n, "pad": "x" * 100}) + "\n" for n in range(1000)))
    bound = path.stat().st_size // 4

    tail = read_events_tail(str(path), max_bytes=bound)

    assert 0 < len(tail) < 300
    assert tail[-1]["n"] == 999
    assert [e["n"] for e in tail] == list(range(1000 - len(tail), 1000))  # contiguous, no torn first line
    assert len(read_events_tail(str(path))) == 1000
    assert read_events_tail(str(tmp_path / "missing.jsonl")) == []
