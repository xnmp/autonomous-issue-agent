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
    JsonValue,
)

WorldSubject = tuple[str, int]
WorldFeedSubject = tuple[str, str, str, str, int]


@dataclass(frozen=True)
class RunProjection:
    start: EventEnvelope
    completion: EventEnvelope | None = None
    abandoned: EventEnvelope | None = None


@dataclass(frozen=True)
class ReadModel:
    runs: Mapping[str, RunProjection] = field(default_factory=dict)
    levels: Mapping[str, EventEnvelope] = field(default_factory=dict)
    observed: Mapping[WorldSubject, EventEnvelope] = field(default_factory=dict)
    observed_by_feed: Mapping[WorldFeedSubject, EventEnvelope] = field(
        default_factory=dict
    )
    world_facts: Mapping[WorldSubject, Mapping[str, JsonValue]] = field(
        default_factory=dict
    )
    spend_micros_by_basis: Mapping[str, int] = field(default_factory=dict)
    applied_event_ids: frozenset[str] = frozenset()
    events: tuple[EventEnvelope, ...] = ()


def fold(events: Iterable[EventEnvelope]) -> ReadModel:
    unique = {event.event_id: event for event in events}
    ordered = sorted(unique.values(), key=lambda event: event.order_key)
    runs: dict[str, RunProjection] = {}
    levels: dict[str, EventEnvelope] = {}
    observed_by_feed: dict[WorldFeedSubject, EventEnvelope] = {}
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
            feed_key = (
                event.producer.deployment,
                event.producer.repository,
                event.producer.role,
                payload.subject_kind,
                payload.subject_number,
            )
            observed_by_feed[feed_key] = event
        elif isinstance(payload, SpendRecorded):
            spend[payload.basis] = (
                spend.get(payload.basis, 0) + payload.amount_micros
            )

    observed, world_facts = _aggregate_world(observed_by_feed)
    return ReadModel(
        runs=MappingProxyType(runs),
        levels=MappingProxyType(levels),
        observed=MappingProxyType(observed),
        observed_by_feed=MappingProxyType(observed_by_feed),
        world_facts=MappingProxyType(
            {
                subject: MappingProxyType(facts)
                for subject, facts in world_facts.items()
            }
        ),
        spend_micros_by_basis=MappingProxyType(spend),
        applied_event_ids=frozenset(unique),
        events=tuple(ordered),
    )


def _aggregate_world(
    by_feed: Mapping[WorldFeedSubject, EventEnvelope],
) -> tuple[
    dict[WorldSubject, EventEnvelope],
    dict[WorldSubject, dict[str, JsonValue]],
]:
    subjects = {
        (key[3], key[4])
        for key in by_feed
    }
    observed: dict[WorldSubject, EventEnvelope] = {}
    facts_by_subject: dict[WorldSubject, dict[str, JsonValue]] = {}
    for subject in subjects:
        candidates = [
            event
            for key, event in by_feed.items()
            if (key[3], key[4]) == subject
        ]
        active = [
            event
            for event in candidates
            if isinstance(event.payload, WorldObserved)
            and event.payload.facts.get("present") is not False
        ]
        if not active:
            latest = max(candidates, key=lambda event: event.order_key)
            observed[subject] = latest
            facts_by_subject[subject] = {"present": False}
            continue
        observed[subject] = max(active, key=lambda event: event.order_key)
        field_values: dict[
            str,
            tuple[tuple[object, ...], JsonValue],
        ] = {}
        for event in active:
            payload = event.payload
            assert isinstance(payload, WorldObserved)
            for field, value in payload.facts.items():
                prior = field_values.get(field)
                if prior is None or event.order_key > prior[0]:
                    field_values[field] = (event.order_key, value)
        facts_by_subject[subject] = {
            field: value
            for field, (_order, value) in field_values.items()
        }
        facts_by_subject[subject]["present"] = True
    return observed, facts_by_subject
