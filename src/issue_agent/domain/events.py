"""Versioned typed event envelope and codec registry.

Events are telemetry and projection input.  They are never queried to authorize
an effect, reserve spend, or deduplicate behavior.  Operational adapters emit
them from committed control-store transitions through the outbox.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import MISSING, dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping, TypeVar, get_origin, get_type_hints

from .repository import canonical_repository

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
PayloadT = TypeVar("PayloadT", bound="EventPayload")
MAX_EVENT_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Producer:
    deployment: str
    repository: str
    role: str
    pid: int
    boot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository", canonical_repository(self.repository)
        )
        if not self.deployment or not self.role or not self.boot_id:
            raise ValueError("event producer identity is incomplete")
        if self.pid <= 0:
            raise ValueError("producer pid must be positive")

    @property
    def identity(self) -> str:
        return (
            f"{self.deployment}:{self.repository}:{self.role}:"
            f"{self.pid}:{self.boot_id}"
        )

    @property
    def feed_identity(self) -> str:
        """Stable destination for one role's append-only projection feed."""

        return f"{self.deployment}:{self.repository}:{self.role}"


@dataclass(frozen=True)
class Correlation:
    issue: int | None = None
    pr: int | None = None
    dispatch_id: str | None = None
    run_id: str | None = None
    leg_id: str | None = None
    session_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self) -> None:
        if self.issue is not None and self.issue <= 0:
            raise ValueError("issue correlation must be positive")
        if self.pr is not None and self.pr <= 0:
            raise ValueError("PR correlation must be positive")


class EventPayload:
    event_type: ClassVar[str]


_REGISTRY: dict[str, type[EventPayload]] = {}


def event_type(name: str):
    def register(cls: type[PayloadT]) -> type[PayloadT]:
        if name in _REGISTRY:
            raise RuntimeError(f"duplicate event type {name}")
        cls.event_type = name
        _REGISTRY[name] = cls
        return cls

    return register


@event_type("measurement.observation.recorded.v1")
@dataclass(frozen=True)
class MeasurementObservationRecorded(EventPayload):
    record: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        from .measurement import observation_json, parse_observation

        normalized = observation_json(parse_observation(self.record))
        def freeze(value: Any) -> Any:
            if isinstance(value, dict):
                return MappingProxyType({key: freeze(item) for key, item in value.items()})
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value
        object.__setattr__(self, "record", freeze(normalized))


@event_type("agent.run.started.v1")
@dataclass(frozen=True)
class AgentRunStarted(EventPayload):
    model: str
    kind: str = ""
    attributes: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self, "attributes", MappingProxyType(dict(self.attributes))
            )


@event_type("agent.run.completed.v1")
@dataclass(frozen=True)
class AgentRunCompleted(EventPayload):
    status: str
    is_error: bool
    exit_reason: str | None
    duration_ms: int
    model: str
    runtime: str
    cost_micros: int | None = None
    currency: str | None = None
    pricing_basis: str | None = None
    usage: Mapping[str, int] | None = None
    summary: str | None = None
    kind: str = ""
    attributes: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("run duration cannot be negative")
        if self.cost_micros is not None and self.cost_micros < 0:
            raise ValueError("run cost cannot be negative")
        if (self.cost_micros is None) != (self.currency is None):
            raise ValueError("cost and currency must be present together")
        if self.usage is not None:
            if any(value < 0 for value in self.usage.values()):
                raise ValueError("usage counters cannot be negative")
            object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        if self.attributes is not None:
            object.__setattr__(
                self, "attributes", MappingProxyType(dict(self.attributes))
            )


@event_type("agent.run.abandoned.v1")
@dataclass(frozen=True)
class AgentRunAbandoned(EventPayload):
    corrects: str
    reason: str


@event_type("agent.run.stall_corrected.v1")
@dataclass(frozen=True)
class AgentRunStallCorrected(EventPayload):
    corrects: str
    stall_id: str
    suspended_ms: int


@event_type("lifecycle.action.applied.v1")
@dataclass(frozen=True)
class ActionApplied(EventPayload):
    command_id: str
    kind: str
    target: str
    display: str
    number: int | None = None
    attributes: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.number is not None and self.number <= 0:
            raise ValueError("action target number must be positive")
        if self.attributes is not None:
            object.__setattr__(
                self, "attributes", MappingProxyType(dict(self.attributes))
            )


@event_type("lifecycle.action.failed.v1")
@dataclass(frozen=True)
class ActionFailed(EventPayload):
    command_id: str | None
    display: str
    disposition: str
    error: str
    attempts: int = 0


@event_type("lifecycle.action.retried.v1")
@dataclass(frozen=True)
class ActionRetried(EventPayload):
    command_id: str | None
    display: str
    attempt: int
    delay_ms: int
    error: str


@event_type("lifecycle.action.verified.v1")
@dataclass(frozen=True)
class ActionVerified(EventPayload):
    command_id: str | None
    display: str


@event_type("lifecycle.escalation.raised.v1")
@dataclass(frozen=True)
class EscalationRaised(EventPayload):
    target: str
    number: int


@event_type("lifecycle.block_retired.v1")
@dataclass(frozen=True)
class BlockRetired(EventPayload):
    blocker: str
    blocked_head: str
    observed_head: str
    self_clear_count: int


@event_type("accounting.budget_block_retired.v1")
@dataclass(frozen=True)
class BudgetBlockRetired(EventPayload):
    scope: str
    old_headroom_micros: int | None
    new_headroom_micros: int | None
    reservation_micros: int
    cap_micros: int | None
    currency: str
    self_clear_count: int


@event_type("lifecycle.self_clear_ceiling_reached.v1")
@dataclass(frozen=True)
class SelfClearCeilingReached(EventPayload):
    blocker: str
    target: str
    number: int
    ceiling: int


@event_type("state.level.changed.v1")
@dataclass(frozen=True)
class LevelChanged(EventPayload):
    state_key: str
    state: str
    reason: str
    attributes: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self, "attributes", MappingProxyType(dict(self.attributes))
            )


@event_type("accounting.spend.recorded.v1")
@dataclass(frozen=True)
class SpendRecorded(EventPayload):
    run_id: str
    amount_micros: int
    currency: str
    basis: str
    rate_card_id: str | None
    rate_card_version: str | None


@event_type("accounting.quote.variance.v1")
@dataclass(frozen=True)
class BudgetVarianceDetected(EventPayload):
    run_id: str
    quoted_micros: int
    actual_micros: int
    variance_micros: int
    currency: str


@event_type("accounting.admission.blocked.v1")
@dataclass(frozen=True)
class SpendBlocked(EventPayload):
    scope: str
    status: str
    amount_micros: int | None
    cap_micros: int | None
    currency: str
    reason: str


@event_type("accounting.reservation.retained.v1")
@dataclass(frozen=True)
class ReservationRetained(EventPayload):
    run_id: str
    reason: str


@event_type("accounting.native_usage.remetered.v1")
@dataclass(frozen=True)
class NativeUsageRemetered(EventPayload):
    """Recovery re-read retained rollouts for a frozen meter failure and settled."""

    run_id: str
    failure_class: str
    outcome: str


@event_type("accounting.admission.reconciled.v1")
@dataclass(frozen=True)
class AdmissionReconciled(EventPayload):
    """One reconcile-then-retry attempt taken before a budget escalation.

    keifu #34 (2026-08-02): three legs died on a broken provider binary with
    $0 measured spend. Their conservatively retained reservations consumed the
    issue's headroom, so the next dispatch was denied and escalated to a human
    even though real spend was zero. Admission now runs the existing
    reservation recovery once before escalating. ADR 0012: exactly one such
    attempt per dispatch, journaled here.

    ``admitted`` false with ``released_runs`` zero is the ordinary shape of a
    genuine cap exhaustion, not a defect: it records that reconciliation was
    tried and found nothing releasable. ``scope`` names the limit that actually
    tripped, which is the cap an operator would have to move.
    """

    run_id: str
    scope: str
    released_runs: int
    admitted: bool
    reason: str


@event_type("telemetry.warning.v1")
@dataclass(frozen=True)
class TelemetryWarning(EventPayload):
    code: str
    warning: str
    details: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.details is not None:
            object.__setattr__(
                self, "details", MappingProxyType(dict(self.details))
            )


@event_type("daemon.tick.gated.v1")
@dataclass(frozen=True)
class TickGated(EventPayload):
    reason: str


@event_type("github.unavailable.v1")
@dataclass(frozen=True)
class GithubUnavailable(EventPayload):
    kind: str
    detail: str
    retry_after_ms: int | None = None


@event_type("daemon.stall.detected.v1")
@dataclass(frozen=True)
class StallDetected(EventPayload):
    stall_id: str
    started_at: str
    woke_at: str
    suspended_ms: int
    elapsed_ms: int = 0
    clock_offset_ms: int = 0


@event_type("world.observed.v1")
@dataclass(frozen=True)
class WorldObserved(EventPayload):
    subject_kind: str
    subject_number: int
    content_hash: str
    facts: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.subject_number <= 0:
            raise ValueError("observed subject number must be positive")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


@event_type("world.feed.retired.v1")
@dataclass(frozen=True)
class WorldFeedRetired(EventPayload):
    deployment: str
    repository: str
    role: str
    restartable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository", canonical_repository(self.repository)
        )
        if not self.deployment or not self.repository or not self.role:
            raise ValueError("retired world feed identity is incomplete")


@event_type("handover.written.v1")
@dataclass(frozen=True)
class HandoverWritten(EventPayload):
    trigger: str
    bytes_written: int


@event_type("router.path_prediction.v1")
@dataclass(frozen=True)
class PathPrediction(EventPayload):
    predicted_paths: tuple[str, ...]
    changed_files: tuple[str, ...]
    matched_paths: tuple[str, ...]
    signature: str = ""


@event_type("issue.recurrence.detected.v1")
@dataclass(frozen=True)
class RecurrenceDetected(EventPayload):
    kind: str
    original_issue: int | None
    signature: str = ""


@event_type("issue.triage.disposition.v1")
@dataclass(frozen=True)
class TriageDispositionRecorded(EventPayload):
    """Harness-minted state marker; no classifier prose crosses this boundary."""

    triage_state: Literal["processed", "blocked_on_human_reply"]

    def __post_init__(self) -> None:
        if self.triage_state not in {"processed", "blocked_on_human_reply"}:
            raise ValueError("triage disposition state is invalid")


@event_type("review.skipped.v1")
@dataclass(frozen=True)
class ReviewSkipped(EventPayload):
    reviewer: str
    head_sha: str
    reason: str


@event_type("ci.flake.detected.v1")
@dataclass(frozen=True)
class CiFlakeDetected(EventPayload):
    head_sha: str
    signature: str = ""


@event_type("ci.pending.deadline.v1")
@dataclass(frozen=True)
class CiPendingDeadline(EventPayload):
    head_sha: str
    seconds: int
    checks: tuple[str, ...]
    signature: str = ""


@event_type("pr.duplicate.prevented.v1")
@dataclass(frozen=True)
class DuplicatePrPrevented(EventPayload):
    author: str
    signature: str = ""


@event_type("routing.escalated.v1")
@dataclass(frozen=True)
class RoutingEscalated(EventPayload):
    leg: str
    from_tier: int | None
    to_tier: int | None
    from_effort: str | None
    to_effort: str | None
    evidence: Mapping[str, int | bool]
    model: str | None = None
    ceiling: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@event_type("budget.path.entered.v1")
@dataclass(frozen=True)
class BudgetPathEntered(EventPayload):
    leg: str
    exit_reason: str


@event_type("infra.path.entered.v1")
@dataclass(frozen=True)
class InfraPathEntered(EventPayload):
    """A failed leg was classed INFRA: it was billed to the infra ceiling
    rather than to the issue (a quality counter it would otherwise have
    advanced, or a decay it would otherwise have earned).

    ADR 0012: the exemption is a skip, and a skip needs a journal event and a
    ceiling. The ceiling is the infra-death counter this same leg bumps;
    `infra_deaths` carries where it now stands so "why didn't the breaker
    move?" is answerable from the event log alone (keifu #130).
    """

    leg: str
    exit_reason: str
    infra_deaths: int = 0


@event_type("infra.breaker.tripped.v1")
@dataclass(frozen=True)
class InfraBreakerTripped(EventPayload):
    """Infra-class deaths crossed issues: paid dispatch paused deployment-wide.

    ONE event for a storm that used to produce one escalation per affected
    issue (keifu 2026-08-02). `cause` and `remediation` are machine-checkable
    strings in ADR 0013's shape — the predicate plus its evidence, and what the
    harness already tried automatically — so the escalation can be re-evaluated
    rather than re-read.
    """

    cause: str
    remediation: str
    deaths: int = 0
    issues: tuple[int, ...] = ()
    window_seconds: int = 0


@event_type("infra.breaker.cleared.v1")
@dataclass(frozen=True)
class InfraBreakerCleared(EventPayload):
    """The breaker re-armed. `reason` is `probe_succeeded` (a leg completed, so
    the runtime is demonstrably alive) or `cooldown_elapsed` (the bound on the
    hold expired; a still-broken runtime re-trips within one window)."""

    reason: str
    cause: str = ""
    deaths: int = 0


@event_type("issue.groomed.v1")
@dataclass(frozen=True)
class IssueGroomed(EventPayload):
    signature: str
    disposition: str
    attributes: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self, "attributes", MappingProxyType(dict(self.attributes))
            )


@event_type("projection.snapshot.v1")
@dataclass(frozen=True)
class ProjectionSnapshot(EventPayload):
    open_levels: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "open_levels", MappingProxyType(dict(self.open_levels))
        )


@event_type("daemon.state.changed.v1")
@dataclass(frozen=True)
class DaemonStateChanged(EventPayload):
    previous: str
    state: str
    reason: str


@event_type("control.transition.committed.v1")
@dataclass(frozen=True)
class ControlTransitionCommitted(EventPayload):
    """Committed shadow of authoritative control-store state."""

    entity_kind: str
    entity_id: str
    state: str
    attributes: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.entity_kind or not self.entity_id or not self.state:
            raise ValueError("control transition identity and state are required")
        object.__setattr__(
            self,
            "attributes",
            MappingProxyType(dict(self.attributes)),
        )


@dataclass(frozen=True)
class TelemetryEvent:
    """A producer-created typed payload with explicit correlation."""

    payload: EventPayload
    correlation: Correlation = Correlation()


@dataclass(frozen=True)
class EventEnvelope:
    payload: EventPayload
    event_id: str
    seq: int
    occurred_at: datetime
    observed_at: datetime
    monotonic_ns: int
    producer: Producer
    correlation: Correlation = Correlation()
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("only event schema version 2 is supported")
        if not self.event_id or self.seq < 0 or self.monotonic_ns < 0:
            raise ValueError("event identity and clocks must be non-negative")
        _aware(self.occurred_at, "occurred_at")
        _aware(self.observed_at, "observed_at")
        if self.observed_at < self.occurred_at:
            raise ValueError("observed_at cannot precede occurred_at")

    @property
    def type(self) -> str:
        return self.payload.event_type

    @property
    def order_key(self) -> tuple[int, str, int]:
        timestamp_ms = int(self.occurred_at.timestamp() * 1000)
        return timestamp_ms, self.producer.identity, self.seq


def new_event_id(now: datetime) -> str:
    """Sortable 128-bit identifier: 48-bit epoch milliseconds + 80 random bits."""

    _aware(now, "event-id time")
    timestamp_ms = int(now.timestamp() * 1000)
    if timestamp_ms < 0 or timestamp_ms >= 1 << 48:
        raise ValueError("event timestamp is outside sortable-id range")
    return f"{timestamp_ms:012x}{uuid.uuid4().hex[12:]}"


def encode_event(event: EventEnvelope) -> dict[str, JsonValue]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "type": event.type,
        "seq": event.seq,
        "occurred_at": _stamp(event.occurred_at),
        "observed_at": _stamp(event.observed_at),
        "monotonic_ns": event.monotonic_ns,
        "producer": _encode_dataclass(event.producer),
        "correlation": _encode_dataclass(event.correlation),
        "data": _encode_dataclass(event.payload),
    }


def encode_event_line(event: EventEnvelope) -> str:
    return json.dumps(encode_event(event), sort_keys=True, separators=(",", ":"))


def decode_event(raw: Mapping[str, Any]) -> EventEnvelope:
    version = raw.get("schema_version")
    if version != 2:
        raise ValueError(f"unsupported event schema_version {version!r}")
    type_name = raw.get("type")
    payload_cls = _REGISTRY.get(str(type_name))
    if payload_cls is None:
        raise ValueError(f"unknown event type {type_name!r}")
    producer_raw = _mapping(raw.get("producer"), "producer")
    correlation_raw = _mapping(raw.get("correlation"), "correlation")
    data_raw = _mapping(raw.get("data"), "data")
    return EventEnvelope(
        payload=_construct(payload_cls, data_raw),
        event_id=str(raw["event_id"]),
        seq=int(raw["seq"]),
        occurred_at=_parse_stamp(raw["occurred_at"]),
        observed_at=_parse_stamp(raw["observed_at"]),
        monotonic_ns=int(raw["monotonic_ns"]),
        producer=_construct(Producer, producer_raw),
        correlation=_construct(Correlation, correlation_raw),
    )


def decode_event_line(line: str) -> EventEnvelope:
    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ValueError("event line must contain an object")
    return decode_event(raw)


def event_registry() -> Mapping[str, type[EventPayload]]:
    return MappingProxyType(dict(_REGISTRY))


@lru_cache(maxsize=None)
def _decode_plan(cls: type) -> tuple[tuple[str, bool, bool], ...]:
    """Per-field ``(name, listify_to_tuple, required)`` resolved once per class.

    `get_type_hints` compiles and evaluates the class's string annotations
    (this module is `from __future__ import annotations`). Doing that per
    decoded event — three times, for the payload, producer and correlation —
    cost ~0.46ms/event and was ~95% of `read_event_log`, which pinned a CPU
    core on the dashboard's periodic re-fold (keifu, 2026-08-02). A class's
    fields and annotations cannot change after definition, so the plan is a
    pure function of the class and safe to memoize for the process lifetime.
    """
    type_hints = get_type_hints(cls)
    return tuple(
        (
            field.name,
            isinstance(field.default, tuple)
            or get_origin(type_hints.get(field.name, field.type)) is tuple,
            field.default is MISSING and field.default_factory is MISSING,
        )
        for field in fields(cls)
    )


def _construct(cls: type[PayloadT], raw: Mapping[str, Any]) -> PayloadT:
    kwargs: dict[str, Any] = {}
    for name, listify, required in _decode_plan(cls):
        if name in raw:
            value = raw[name]
            if listify and isinstance(value, list):
                value = tuple(value)
            kwargs[name] = value
        elif required:
            raise ValueError(f"{cls.__name__}.{name} is required")
    return cls(**kwargs)


def _encode_dataclass(value: object) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for field in fields(value):
        result[field.name] = _json_value(getattr(value, field.name))
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, datetime):
        return _stamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not JSON-serializable event data")


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"event {field_name} must be an object")
    return value


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("event timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _aware(parsed, "event timestamp")
    return parsed
