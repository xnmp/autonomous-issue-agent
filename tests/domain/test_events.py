from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.events import (
    AgentRunCompleted,
    Correlation,
    EventEnvelope,
    Producer,
    TriageDispositionRecorded,
    decode_event,
    decode_event_line,
    encode_event,
    encode_event_line,
    event_registry,
    new_event_id,
)


def _event() -> EventEnvelope:
    now = datetime(2026, 7, 30, 1, 2, 3, 456000, tzinfo=timezone.utc)
    return EventEnvelope(
        payload=AgentRunCompleted(
            status="ok",
            is_error=False,
            exit_reason="ok",
            duration_ms=12,
            model="gpt",
            runtime="codex",
            cost_micros=123,
            currency="USD",
            pricing_basis="configured_rate_card",
            usage={"input_tokens": 4},
        ),
        event_id=new_event_id(now),
        seq=7,
        occurred_at=now,
        observed_at=now + timedelta(milliseconds=1),
        monotonic_ns=99,
        producer=Producer("prod", "o/r", "fix", 123, "boot"),
        correlation=Correlation(
            issue=1,
            dispatch_id="dispatch",
            run_id="run",
            leg_id="leg",
        ),
    )


def test_event_codec_round_trips_real_typed_payload():
    event = _event()
    assert decode_event_line(encode_event_line(event)) == event
    assert encode_event(event)["schema_version"] == 2
    assert event.order_key == (1785373323456, event.producer.identity, 7)


def test_event_codec_tolerates_additive_fields_but_rejects_schema_drift():
    event = _event()
    raw = encode_event(event)
    raw["future"] = {"ignored": True}
    raw["data"]["future_payload_field"] = "ignored"
    assert decode_event(raw) == event
    with pytest.raises(ValueError, match="unsupported"):
        decode_event({**raw, "schema_version": 99})


def test_event_payload_is_strictly_json_serializable():
    event = _event()
    bad_payload = replace(event.payload, summary=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="not JSON-serializable"):
        encode_event(replace(event, payload=bad_payload))


def test_registry_is_exhaustive_and_read_only():
    registry = event_registry()
    assert registry["agent.run.completed.v1"] is AgentRunCompleted
    with pytest.raises(TypeError):
        registry["new"] = AgentRunCompleted  # type: ignore[index]


def test_triage_disposition_rejects_an_untrusted_state():
    with pytest.raises(ValueError, match="triage disposition state"):
        TriageDispositionRecorded("waiting")  # type: ignore[arg-type]
