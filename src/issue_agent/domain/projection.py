"""Single deterministic fold from schema-v2 telemetry to a read model."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .events import (
    AgentRunAbandoned,
    AgentRunCompleted,
    AgentRunStarted,
    EventEnvelope,
    LevelChanged,
    ProjectionSnapshot,
    SpendRecorded,
    WorldObserved,
)


@dataclass(frozen=True)
class RunProjection:
    start: EventEnvelope
    completion: EventEnvelope | None = None
    abandoned: EventEnvelope | None = None


@dataclass(frozen=True)
class ReadModel:
    runs: Mapping[str, RunProjection] = field(default_factory=dict)
    levels: Mapping[str, EventEnvelope] = field(default_factory=dict)
    observed: Mapping[tuple[str, int], EventEnvelope] = field(default_factory=dict)
    spend_micros_by_basis: Mapping[str, int] = field(default_factory=dict)
    applied_event_ids: frozenset[str] = frozenset()
    events: tuple[EventEnvelope, ...] = ()


def fold(events: Iterable[EventEnvelope]) -> ReadModel:
    unique = {event.event_id: event for event in events}
    ordered = sorted(unique.values(), key=lambda event: event.order_key)
    runs: dict[str, RunProjection] = {}
    levels: dict[str, EventEnvelope] = {}
    observed: dict[tuple[str, int], EventEnvelope] = {}
    spend: dict[str, int] = {}
    starts_by_event_id: dict[str, str] = {}

    for event in ordered:
        payload = event.payload
        run_id = event.correlation.run_id
        if isinstance(payload, AgentRunStarted) and run_id:
            runs[run_id] = RunProjection(event)
            starts_by_event_id[event.event_id] = run_id
        elif isinstance(payload, AgentRunCompleted) and run_id:
            prior = runs.get(run_id)
            if prior is not None:
                runs[run_id] = RunProjection(prior.start, event, prior.abandoned)
        elif isinstance(payload, AgentRunAbandoned):
            corrected_run = starts_by_event_id.get(payload.corrects)
            if corrected_run:
                prior = runs[corrected_run]
                runs[corrected_run] = RunProjection(
                    prior.start, prior.completion, event
                )
        elif isinstance(payload, LevelChanged):
            if payload.state in {"cleared", "closed", "resolved", "stopped"}:
                levels.pop(payload.state_key, None)
            else:
                levels[payload.state_key] = event
        elif isinstance(payload, ProjectionSnapshot):
            feed = (
                event.producer.deployment,
                event.producer.repository,
                event.producer.role,
            )
            levels = {
                state_key: level
                for state_key, level in levels.items()
                if (
                    level.producer.deployment,
                    level.producer.repository,
                    level.producer.role,
                )
                != feed
            }
            levels.update(
                {state_key: event for state_key in payload.open_levels}
            )
        elif isinstance(payload, WorldObserved):
            observed[(payload.subject_kind, payload.subject_number)] = event
        elif isinstance(payload, SpendRecorded):
            spend[payload.basis] = (
                spend.get(payload.basis, 0) + payload.amount_micros
            )

    return ReadModel(
        runs=MappingProxyType(runs),
        levels=MappingProxyType(levels),
        observed=MappingProxyType(observed),
        spend_micros_by_basis=MappingProxyType(spend),
        applied_event_ids=frozenset(unique),
        events=tuple(ordered),
    )
