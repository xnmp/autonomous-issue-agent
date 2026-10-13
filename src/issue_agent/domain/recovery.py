"""Recovery triggers (ADR 0006).

BOOT reaps every registered process group before adapters exist (main), then
reclaims dead leases, validates worktrees and sweeps reservations at once.
WAKE reaps only abandoned groups and sweeps with the stale grace. PERIODIC
reclaims leases and sweeps; it never touches processes or worktrees.
"""

from __future__ import annotations

from enum import Enum


class RecoveryTrigger(str, Enum):
    BOOT = "boot"
    WAKE = "wake"
    PERIODIC = "periodic"
