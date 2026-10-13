"""The daemon log is one plain JSON object per line, readable without the harness."""

import json
import threading

from issue_agent.adapters.journal import Journal
from tests.fakes.legacy_journal import read_events


def test_each_record_is_one_self_describing_json_line(tmp_path):
    path = tmp_path / "state" / "journal.jsonl"
    journal = Journal(str(path))

    journal.emit("escalation", {"target": "pr", "number": 7})
    journal.emit("telemetry_warning", {"warning": "rate card missing"})

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

    journal.emit("escalation", {"target": "issue", "number": 3})

    assert "journal: dropped escalation" in capsys.readouterr().err


def test_concurrent_writers_never_interleave_a_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(str(path))
    huge = "x" * 200_000

    def write(index):
        journal.emit("telemetry_warning", {"warning": f"{index}:{huge}", "issue": index + 1})

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

    journal.emit("telemetry_warning", {"code": "w", "warning": body})
    journal.emit("escalation", {"target": "pr", "number": 7})

    raw = path.read_text()
    assert raw.count("\n") == 2
    assert json.loads(raw.splitlines()[0])["warning"] == body


def test_the_journal_is_private_to_the_operator(tmp_path):
    path = tmp_path / "state" / "journal.jsonl"

    Journal(str(path)).emit("escalation", {"target": "pr", "number": 7})

    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
