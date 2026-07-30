# ADR 0004 — Event envelope and retention

Status: Accepted
Date: 2026-07-30
Governs: src/issue_agent/domain/events.py src/issue_agent/domain/projection.py src/issue_agent/adapters/event_log.py

## Decision

Operational telemetry uses schema-v2 frozen event envelopes with sortable
event ids, per-producer sequence, wall/observed/monotonic clocks, producer
identity, and explicit correlation ids. Payloads are registered frozen
dataclasses; incompatible versions and unknown types fail explicitly. There is
no `default=str` coercion.

Per-role JSONL remains append-only telemetry. Writers use one bounded
`O_APPEND` write, rotate by size/age, and carry open level state into a
projection snapshot. Duplicate ids are ignored; late events are ordered by
`(occurred_at_ms, producer_identity, seq)`. `domain/projection.fold` is the
canonical rebuild path.

## Consequences

The dashboard may discard pre-v2 history. JSONL loss can reduce observability
but cannot admit spend, duplicate a remote effect, or authorize a gate.
