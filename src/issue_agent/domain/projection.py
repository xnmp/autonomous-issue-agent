"""Single deterministic fold from schema-v2 telemetry to a read model."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .ci import ci_state
from .world import Check
from .events import (
    AgentRunAbandoned,
    AgentRunCompleted,
    AgentRunStarted,
    EventEnvelope,
    LevelChanged,
    ProjectionSnapshot,
    SpendRecorded,
    WorldFeedRetired,
    WorldObserved,
    JsonValue,
)

WorldSubject = tuple[str, int]
WorldFeedSubject = tuple[str, str, str, str, int]


@dataclass(frozen=True)
class _PRCI:
    head: str = ""
    state: str = ""
    ci_state: str | None = None

    def observe(self, facts: Mapping[str, JsonValue], ignore: str) -> _PRCI:
        head = facts.get("head_sha", self.head)
        state = "MERGED" if self.state == "MERGED" else facts.get("state", self.state)
        head = head if isinstance(head, str) else ""
        state = state if isinstance(state, str) else ""
        signal = self.ci_state if head == self.head else None
        if facts.get("checks_known") is True:
            signal = _observed_ci(facts, ignore) or signal
        return _PRCI(head, state, signal if head and state == "OPEN" else None)


@dataclass(frozen=True)
class RunProjection:
    start: EventEnvelope | None = None
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


def fold(events: Iterable[EventEnvelope], *, ci_ignore_checks: str = "") -> ReadModel:
    unique = {event.event_id: event for event in events}
    ordered = sorted(unique.values(), key=lambda event: event.order_key)
    runs: dict[str, RunProjection] = {}
    levels: dict[str, EventEnvelope] = {}
    observed_by_feed: dict[WorldFeedSubject, EventEnvelope] = {}
    spend: dict[str, int] = {}
    starts_by_event_id: dict[str, str] = {}
    retired_feeds: set[tuple[str, str, str]] = set()
    pr_ci: dict[int, _PRCI] = {}

    for event in ordered:
        payload = event.payload
        run_id = event.correlation.run_id
        feed = (
            event.producer.deployment,
            event.producer.repository,
            event.producer.role,
        )
        if (
            isinstance(payload, (LevelChanged, ProjectionSnapshot, WorldObserved))
            and feed in retired_feeds
        ):
            continue
        if isinstance(payload, AgentRunStarted) and run_id:
            prior = runs.get(run_id)
            if prior is None:
                runs[run_id] = RunProjection(event)
            elif prior.start is None:
                runs[run_id] = RunProjection(
                    event, prior.completion, prior.abandoned
                )
            starts_by_event_id[event.event_id] = run_id
        elif isinstance(payload, AgentRunCompleted) and run_id:
            prior = runs.get(run_id)
            runs[run_id] = RunProjection(
                prior.start if prior is not None else None,
                event,
                prior.abandoned if prior is not None else None,
            )
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
        elif isinstance(payload, WorldFeedRetired):
            retired_feed = (
                payload.deployment,
                payload.repository,
                payload.role,
            )
            # Alias adoption permanently fences the old deployment. A clean
            # stop only ends the current incarnation; the stable identity may
            # legitimately publish again after restart.
            if not payload.restartable:
                retired_feeds.add(retired_feed)
            levels = {
                state_key: level
                for state_key, level in levels.items()
                if (
                    level.producer.deployment,
                    level.producer.repository,
                    level.producer.role,
                )
                != retired_feed
            }
            observed_by_feed = {
                key: observed
                for key, observed in observed_by_feed.items()
                if key[:3] != retired_feed
            }
        elif isinstance(payload, WorldObserved):
            if payload.subject_kind == "pr" and payload.facts.get("present") is not False:
                pr_ci[payload.subject_number] = pr_ci.get(payload.subject_number, _PRCI()).observe(
                    payload.facts, ci_ignore_checks
                )
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
    for number, projected in pr_ci.items():
        facts = world_facts.get(("pr", number))
        if facts is not None and facts.get("present") is True:
            facts["ci_state"] = projected.ci_state
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


def _observed_ci(facts: Mapping[str, JsonValue], ignore: str) -> str | None:
    summary = facts.get("ci_summary")
    if isinstance(summary, dict) and summary.get("policy") == hashlib.sha256(ignore.encode()).hexdigest():
        state = summary.get("state")
        if isinstance(state, str) and state in ("pending", "ok", "failure", "none"):
            return state
    checks = facts.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(check, dict)
        or any(not isinstance(check.get(key), str) for key in ("name", "state", "bucket"))
        or not isinstance(check.get("advisory", False), bool)
        for check in checks
    ):
        return None
    state = ci_state(
        (Check(check["name"], check["state"], check["bucket"], advisory=check.get("advisory", False))
         for check in checks), ignore
    ).value
    # A retained running job proves activity; omitted jobs cannot prove settlement.
    return state if facts.get("checks_total", len(checks)) == len(checks) or state == "pending" else None


def _aggregate_world(
    by_feed: Mapping[WorldFeedSubject, EventEnvelope],
) -> tuple[
    dict[WorldSubject, EventEnvelope],
    dict[WorldSubject, dict[str, JsonValue]],
]:
    observed: dict[WorldSubject, EventEnvelope] = {}
    facts_by_subject: dict[WorldSubject, dict[str, JsonValue]] = {}
    for event in sorted(by_feed.values(), key=lambda event: event.order_key):
        payload = event.payload
        assert isinstance(payload, WorldObserved)
        subject = (payload.subject_kind, payload.subject_number)
        facts = facts_by_subject.setdefault(subject, {"present": False})
        if payload.facts.get("present") is False:
            if facts["present"] is True:
                continue
        else:
            facts.update(payload.facts)
            facts["present"] = True
        observed[subject] = event
    return observed, facts_by_subject
