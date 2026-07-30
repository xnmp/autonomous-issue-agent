from dataclasses import fields

from issue_agent.adapters.event_log import EventLog
from issue_agent.domain.events import (
    Correlation,
    LevelChanged,
    Producer,
    ProjectionSnapshot,
    TelemetryWarning,
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
    "retry_after_ms": None,
    "started_at": "2026-07-30T00:00:00Z",
    "woke_at": "2026-07-30T00:00:01Z",
    "subject_kind": "issue",
    "subject_number": 1,
    "content_hash": "hash",
    "facts": {},
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
