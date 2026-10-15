"""Idempotent boot, wake, and periodic recovery coordinator (ADR 0006)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..adapters.process_supervisor import ProcessSupervisor as DurableProcessSupervisor
from ..domain.contracts import RunId
from ..domain.ports import ProcessSupervisor, RecoveryControl, SpendReconciler
from ..domain.recovery import RecoveryTrigger


@dataclass(frozen=True)
class RecoveryReport:
    trigger: RecoveryTrigger
    killed_orphans: tuple[RunId, ...]
    reclaimed_leases: tuple[RunId, ...]
    spend_unknown: tuple[RunId, ...]


@dataclass
class RecoveryCoordinator:
    supervisor: ProcessSupervisor
    control: RecoveryControl
    spend: SpendReconciler
    owner_id: str
    reservation_grace: timedelta = timedelta(minutes=2)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def reconcile(self, trigger: RecoveryTrigger) -> RecoveryReport:
        """Fixed order: kill orphans, reclaim dead leases, then settle or release stale reservations."""
        killed = self.supervisor.recover_abandoned_orphans() if trigger is RecoveryTrigger.WAKE else ()
        reclaimed = self.control.reclaim_dead_leases(self.owner_id, _pid_alive)
        grace = timedelta(0) if trigger is RecoveryTrigger.BOOT else self.reservation_grace
        return RecoveryReport(trigger, killed, reclaimed, self._sweep(grace))

    def reconcile_stale_reservations(self) -> int:
        """Admission's on-demand sweep (keifu #34): the periodic plan, which
        never reaps processes, so it cannot disturb a live paid child. Counted
        by differencing active reservations: an exact settlement frees
        headroom too but reports nothing unknown."""
        before = frozenset(self.control.active_reservation_runs(self.owner_id))
        self.reconcile(RecoveryTrigger.PERIODIC)
        return len(before - frozenset(self.control.active_reservation_runs(self.owner_id)))

    def _sweep(self, grace: timedelta) -> tuple[RunId, ...]:
        """A reservation whose lease is live is preserved. Otherwise surviving
        request evidence settles it, else an ended leg settles at a zero
        estimate; only a reservation whose process never registered is
        released (spend unknown). Anything else keeps its hold for next time."""
        live = {lease.run_id for lease in self.control.leases() if lease.run_id is not None}
        unknown: list[RunId] = []
        for run_id in self.control.stale_active_reservation_runs(self.owner_id, self.now() - grace):
            if run_id in live:
                continue
            if self.spend.reconcile(run_id) or self.control.settle_terminal_estimate(
                    run_id, "terminal recovery has no usable request evidence"):
                continue
            if self.control.release_unlaunched(run_id, "unlaunched_reservation").disposition.value == "available":
                unknown.append(run_id)
        return tuple(unknown)


def _pid_alive(pid: int, start_ticks: int) -> bool:
    return DurableProcessSupervisor.matches_identity(pid, start_ticks)
