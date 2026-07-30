"""Pure daemon supervision vocabulary and suspension attribution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class DaemonState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"


class ExitCode(IntEnum):
    CLEAN = 0
    TRANSIENT = 1
    INVALID_CONFIG = 2
    DUPLICATE_INSTANCE = 3
    INCOMPATIBLE_SCHEMA = 4
    STORE_CORRUPTION = 5
    OPERATOR_REPAIR_REQUIRED = 6
    RELOAD = 87


@dataclass(frozen=True)
class ClockSample:
    wall_seconds: float
    active_seconds: float


@dataclass(frozen=True)
class Suspension:
    suspended_seconds: float

    @property
    def detected(self) -> bool:
        return self.suspended_seconds > 0


def suspension_between(before: ClockSample, after: ClockSample) -> Suspension:
    """Attribute only wall time not explained by the monotonic active clock."""

    wall_elapsed = max(0.0, after.wall_seconds - before.wall_seconds)
    active_elapsed = max(0.0, after.active_seconds - before.active_seconds)
    return Suspension(max(0.0, wall_elapsed - active_elapsed))
