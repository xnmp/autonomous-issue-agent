"""Pure recovery trigger policy defined by ADR 0006."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecoveryTrigger(str, Enum):
    BOOT = "boot"
    WAKE = "wake"
    PERIODIC = "periodic"


class RecoveryArm(str, Enum):
    REAP_ALL_PROCESSES = "reap_all_processes"
    REAP_ABANDONED_PROCESSES = "reap_abandoned_processes"
    RECLAIM_LEASES = "reclaim_leases"
    VALIDATE_WORKTREES = "validate_worktrees"
    SWEEP_RESERVATIONS = "sweep_reservations"


class RecoveryGrace(str, Enum):
    IMMEDIATE = "immediate"
    STALE = "stale"


@dataclass(frozen=True)
class RecoveryPlan:
    preflight_arms: frozenset[RecoveryArm]
    coordinator_arms: frozenset[RecoveryArm]
    reservation_grace: RecoveryGrace | None

    def __post_init__(self) -> None:
        unsupported_preflight = self.preflight_arms - {
            RecoveryArm.REAP_ALL_PROCESSES
        }
        if unsupported_preflight:
            raise ValueError("preflight may only reap all owned processes")
        arms = self.preflight_arms | self.coordinator_arms
        reapers = arms & {
            RecoveryArm.REAP_ALL_PROCESSES,
            RecoveryArm.REAP_ABANDONED_PROCESSES,
        }
        if len(reapers) > 1:
            raise ValueError("a recovery plan cannot select two process reapers")
        sweeps = RecoveryArm.SWEEP_RESERVATIONS in arms
        if sweeps != (self.reservation_grace is not None):
            raise ValueError(
                "reservation sweep and grace must be configured together"
            )
        if sweeps and RecoveryArm.RECLAIM_LEASES not in arms:
            raise ValueError("reservation sweep requires lease reclamation")


_FULL_ARMS = frozenset(
    {
        RecoveryArm.RECLAIM_LEASES,
        RecoveryArm.VALIDATE_WORKTREES,
        RecoveryArm.SWEEP_RESERVATIONS,
    }
)

_RECOVERY_PLANS = {
    RecoveryTrigger.BOOT: RecoveryPlan(
        frozenset({RecoveryArm.REAP_ALL_PROCESSES}),
        _FULL_ARMS,
        RecoveryGrace.IMMEDIATE,
    ),
    RecoveryTrigger.WAKE: RecoveryPlan(
        frozenset(),
        _FULL_ARMS | {RecoveryArm.REAP_ABANDONED_PROCESSES},
        RecoveryGrace.STALE,
    ),
    RecoveryTrigger.PERIODIC: RecoveryPlan(
        frozenset(),
        frozenset(
            {
                RecoveryArm.RECLAIM_LEASES,
                RecoveryArm.SWEEP_RESERVATIONS,
            }
        ),
        RecoveryGrace.STALE,
    ),
}


def recovery_plan(trigger: RecoveryTrigger) -> RecoveryPlan:
    """Return the exhaustive, immutable plan for one recovery trigger."""

    return _RECOVERY_PLANS[trigger]
