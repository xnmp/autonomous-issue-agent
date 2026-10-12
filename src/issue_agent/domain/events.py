"""Typed records the role daemons write to their plain JSONL journal.

Telemetry only: nothing reads these back to authorize an effect, reserve
spend, or deduplicate behaviour. SQLite remains the only authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class Correlation:
    issue: int | None = None
    pr: int | None = None
    dispatch_id: str | None = None
    run_id: str | None = None
    leg_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.issue is not None and self.issue <= 0:
            raise ValueError("issue correlation must be positive")
        if self.pr is not None and self.pr <= 0:
            raise ValueError("PR correlation must be positive")


class EventPayload:
    """Base for typed journal records."""


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


@dataclass(frozen=True)
class ActionFailed(EventPayload):
    command_id: str | None
    display: str
    disposition: str
    error: str
    attempts: int = 0


@dataclass(frozen=True)
class ActionRetried(EventPayload):
    command_id: str | None
    display: str
    attempt: int
    delay_ms: int
    error: str


@dataclass(frozen=True)
class EscalationRaised(EventPayload):
    target: str
    number: int


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


@dataclass(frozen=True)
class TickGated(EventPayload):
    reason: str


@dataclass(frozen=True)
class GithubUnavailable(EventPayload):
    kind: str
    detail: str
    retry_after_ms: int | None = None


@dataclass(frozen=True)
class StallDetected(EventPayload):
    stall_id: str
    started_at: str
    woke_at: str
    suspended_ms: int
    elapsed_ms: int = 0
    clock_offset_ms: int = 0


@dataclass(frozen=True)
class HandoverWritten(EventPayload):
    trigger: str
    bytes_written: int


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


@dataclass(frozen=True)
class BudgetPathEntered(EventPayload):
    leg: str
    exit_reason: str


@dataclass(frozen=True)
class DaemonStateChanged(EventPayload):
    previous: str
    state: str
    reason: str


@dataclass(frozen=True)
class TelemetryEvent:
    """A producer-created typed payload with explicit correlation."""

    payload: EventPayload
    correlation: Correlation = Correlation()
