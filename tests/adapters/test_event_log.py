import os
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.event_log import EventLog
from issue_agent.domain.events import (
    Correlation,
    LevelChanged,
    Producer,
    ProjectionSnapshot,
    TelemetryWarning,
    WorldFeedRetired,
    WorldObserved,
    decode_event_line,
    encode_event,
    event_registry,
)
from issue_agent.domain.projection import fold


def _producer() -> Producer:
    return Producer("test", "owner/repo", "fix", 100, "boot")


def test_event_log_appends_complete_v2_lines(tmp_path) -> None:
    log = EventLog(str(tmp_path / "events.jsonl"), _producer())
    emitted = log.emit(
        LevelChanged("issue:1:deferred", "waiting", "dependency"),
        correlation=Correlation(issue=1),
    )

    decoded = decode_event_line((tmp_path / "events.jsonl").read_text().strip())
    assert decoded == emitted
    assert decoded.correlation.issue == 1


def test_event_log_completes_a_short_write_without_splitting_the_line(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path), _producer())
    real_write = os.write
    calls = 0

    def short_once(fd, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, content[: len(content) // 2])
        return real_write(fd, content)

    monkeypatch.setattr(os, "write", short_once)
    emitted = log.emit(TelemetryWarning("write", "short"))

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert decode_event_line(lines[0]) == emitted
    assert calls == 2


def test_event_log_rolls_back_a_fragment_before_outbox_retry(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path), _producer())
    real_write = os.write
    calls = 0

    def fail_after_fragment(fd, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, content[: len(content) // 2])
        raise OSError("disk write failed")

    monkeypatch.setattr(os, "write", fail_after_fragment)
    with pytest.raises(OSError, match="disk write failed"):
        log.emit(TelemetryWarning("write", "failed"))
    assert path.read_bytes() == b""

    monkeypatch.setattr(os, "write", real_write)
    retried = log.emit(TelemetryWarning("write", "retried"))
    assert decode_event_line(path.read_text()) == retried


def test_rotation_preserves_open_level_in_snapshot(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=1
    )
    log.emit(LevelChanged("issue:1:deferred", "waiting", "dependency"))
    log.emit(LevelChanged("issue:2:deferred", "waiting", "dependency"))

    current = path.read_text().splitlines()
    assert decode_event_line(current[0]).type == "projection.snapshot.v1"
    assert len(log.paths()) == 2


def test_rotation_retention_bounds_files_and_restart_snapshot(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=2
    )
    for issue in range(1, 8):
        log.emit(
            LevelChanged(f"issue:{issue}:deferred", "waiting", "dependency")
        )

    assert len(log.paths()) == 3

    restarted = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=2
    )
    restarted.emit(
        LevelChanged("issue:8:deferred", "waiting", "dependency")
    )
    snapshot = decode_event_line(path.read_text().splitlines()[0]).payload
    assert isinstance(snapshot, ProjectionSnapshot)
    assert "issue:1:deferred" in snapshot.open_levels
    assert "issue:8:deferred" in snapshot.open_levels


def test_restart_does_not_resurrect_a_cleared_level(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=1
    )
    log.emit(LevelChanged("issue:1:deferred", "waiting", "dependency"))
    log.emit(LevelChanged("issue:1:deferred", "cleared", "ready"))

    restarted = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=1
    )
    restarted.emit(
        LevelChanged("issue:2:deferred", "waiting", "dependency")
    )
    snapshot = decode_event_line(path.read_text().splitlines()[0]).payload
    assert isinstance(snapshot, ProjectionSnapshot)
    assert set(snapshot.open_levels) == {"issue:2:deferred"}


def test_rotation_retention_keeps_latest_world_observation(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(
        str(path), _producer(), max_bytes=1, max_rotated_files=2
    )
    original = log.emit(
        WorldObserved("issue", 7, "world-hash", {"state": "OPEN"}),
        correlation=Correlation(issue=7),
    )
    for index in range(4):
        log.emit(TelemetryWarning("unrelated", str(index)))

    events = tuple(
        decode_event_line(line)
        for event_path in log.paths()
        for line in event_path.read_text().splitlines()
    )
    observed = fold(events).observed[("issue", 7)]
    payload = observed.payload
    assert isinstance(payload, WorldObserved)
    assert payload.facts == {"state": "OPEN"}
    assert observed.event_id == original.event_id
    assert observed.occurred_at == original.occurred_at


def test_world_fold_merges_fields_and_applies_tombstones_per_feed(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    fix = EventLog(
        str(tmp_path / "fix.jsonl"),
        Producer("fix-deployment", "owner/repo", "fix", 100, "boot"),
    )
    steward = EventLog(
        str(tmp_path / "steward.jsonl"),
        Producer(
            "steward-deployment",
            "owner/repo",
            "steward",
            101,
            "boot",
        ),
    )
    fix_observation = fix.emit(
        WorldObserved(
            "issue",
            7,
            "fix-hash",
            {
                "present": True,
                "state": "open",
                "labels": ["ai-working"],
            },
        ),
        correlation=Correlation(issue=7),
        causation_time=now,
    )
    steward_observation = steward.emit(
        WorldObserved(
            "issue",
            7,
            "steward-hash",
            {
                "present": True,
                "title": "Operator title",
                "comments": [{"author": "human", "body": "reply"}],
            },
        ),
        correlation=Correlation(issue=7),
        causation_time=now + timedelta(seconds=1),
    )

    combined = fold((fix_observation, steward_observation))

    assert combined.world_facts[("issue", 7)] == {
        "present": True,
        "state": "open",
        "labels": ["ai-working"],
        "title": "Operator title",
        "comments": [{"author": "human", "body": "reply"}],
    }
    assert len(combined.observed_by_feed) == 2

    steward_tombstone = steward.emit(
        WorldObserved(
            "issue",
            7,
            "tombstone:steward",
            {"present": False, "reason": "absent_from_feed"},
        ),
        correlation=Correlation(issue=7),
        causation_time=now + timedelta(seconds=2),
    )
    after_tombstone = fold(
        (fix_observation, steward_observation, steward_tombstone)
    )

    assert after_tombstone.world_facts[("issue", 7)] == {
        "present": True,
        "state": "open",
        "labels": ["ai-working"],
    }
    assert after_tombstone.observed[("issue", 7)] == fix_observation


def test_world_fold_marks_subject_absent_only_after_every_feed_tombstones(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    log = EventLog(str(tmp_path / "events.jsonl"), _producer())
    present = log.emit(
        WorldObserved(
            "pr",
            9,
            "present",
            {"present": True, "state": "OPEN"},
        ),
        correlation=Correlation(pr=9),
        causation_time=now,
    )
    absent = log.emit(
        WorldObserved(
            "pr",
            9,
            "tombstone:absent",
            {"present": False, "reason": "absent_from_feed"},
        ),
        correlation=Correlation(pr=9),
        causation_time=now + timedelta(seconds=1),
    )

    model = fold((present, absent))

    assert model.world_facts[("pr", 9)] == {"present": False}
    assert model.observed[("pr", 9)] == absent


def test_world_fold_retires_every_subject_from_an_adopted_feed(tmp_path) -> None:
    old = EventLog(
        str(tmp_path / "old.jsonl"),
        Producer("old-deployment", "Acme/App", "fix", 100, "old-boot"),
    )
    current = EventLog(
        str(tmp_path / "current.jsonl"),
        Producer("new-deployment", "acme/app", "fix", 101, "new-boot"),
    )
    present = old.emit(
        WorldObserved(
            "issue",
            7,
            "present",
            {"present": True, "state": "open"},
        )
    )
    retired = current.emit(
        WorldFeedRetired("old-deployment", "acme/app", "fix")
    )

    model = fold((present, retired))

    assert ("issue", 7) not in model.world_facts
    assert model.observed_by_feed == {}


def test_oversized_event_is_rejected_before_write(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path), _producer(), line_limit=100)

    try:
        log.emit(LevelChanged("x" * 200, "waiting", "reason"))
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("oversized event was accepted")
    assert not path.exists()


_SAMPLE = {
    "model": "model",
    "status": "ok",
    "is_error": False,
    "exit_reason": None,
    "duration_ms": 1,
    "runtime": "runtime",
    "cost_micros": None,
    "currency": None,
    "pricing_basis": None,
    "usage": None,
    "summary": None,
    "corrects": "event-id",
    "reason": "reason",
    "stall_id": "stall-id",
    "suspended_ms": 1,
    "command_id": "command-id",
    "kind": "kind",
    "target": "target",
    "display": "display",
    "disposition": "retryable_failure",
    "error": "error",
    "attempt": 1,
    "attempts": 1,
    "delay_ms": 1,
    "number": 1,
    "state_key": "state-key",
    "state": "waiting",
    "run_id": "run-id",
    "amount_micros": 1,
    "quoted_micros": 1,
    "actual_micros": 2,
    "variance_micros": 1,
    "basis": "provider_reported",
    "rate_card_id": None,
    "rate_card_version": None,
    "scope": "repository",
    "cap_micros": None,
    "code": "warning",
    "warning": "warning",
    "detail": "detail",
    "details": {},
    "retry_after_ms": None,
    "started_at": "2026-07-30T00:00:00Z",
    "woke_at": "2026-07-30T00:00:01Z",
    "clock_offset_ms": 1,
    "elapsed_ms": 1,
    "subject_kind": "issue",
    "subject_number": 1,
    "content_hash": "hash",
    "facts": {},
    "deployment": "deployment",
    "repository": "owner/repo",
    "role": "fix",
    "trigger": "budget",
    "bytes_written": 1,
    "predicted_paths": ("a.py",),
    "changed_files": ("a.py",),
    "matched_paths": ("a.py",),
    "original_issue": None,
    "reviewer": "bot",
    "head_sha": "sha",
    "seconds": 1,
    "checks": ("ci",),
    "author": "bot",
    "leg": "resolve",
    "from_tier": 1,
    "to_tier": 2,
    "from_effort": None,
    "to_effort": None,
    "ceiling": "maximum",
    "evidence": {},
    "signature": "signature",
    "open_levels": {},
    "previous": "starting",
    "entity_kind": "run",
    "entity_id": "run-id",
    "attributes": {},
}


def test_every_registered_payload_has_a_real_emit_codec_golden(tmp_path) -> None:
    path = tmp_path / "all-events.jsonl"
    log = EventLog(str(path), _producer())
    emitted = []
    for name, payload_type in event_registry().items():
        kwargs = {field.name: _SAMPLE[field.name] for field in fields(payload_type)}
        envelope = log.emit(payload_type(**kwargs))
        golden = encode_event(envelope)
        assert golden == {
            "schema_version": 2,
            "event_id": envelope.event_id,
            "type": name,
            "seq": envelope.seq,
            "occurred_at": golden["occurred_at"],
            "observed_at": golden["observed_at"],
            "monotonic_ns": envelope.monotonic_ns,
            "producer": golden["producer"],
            "correlation": golden["correlation"],
            "data": {
                field.name: golden["data"][field.name]
                for field in fields(payload_type)
            },
        }
        emitted.append(envelope)

    decoded = tuple(
        decode_event_line(line) for line in path.read_text().splitlines()
    )
    assert decoded == tuple(emitted)
