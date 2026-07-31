"""Idempotent boot/suspend recovery coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

from ..adapters.process_supervisor import ProcessSupervisor as DurableProcessSupervisor
from ..domain.contracts import RunId
from ..domain.reservation_recovery import (
    ReservationRecoveryAction,
    ReservationRecoveryState,
    decide_reservation_recovery,
)
from ..domain.ports import (
    ProcessSupervisor,
    CommandRecovery,
    RecoveryControl,
    RunAbandonmentSink,
    SpendReconciler,
    WorktreeRecovery,
)


@dataclass(frozen=True)
class RecoveryReport:
    killed_orphans: tuple[RunId, ...]
    reclaimed_leases: tuple[RunId, ...]
    validated_worktrees: int
    recovered_commands: int
    spend_unknown: tuple[RunId, ...]
    pending_commands: int


@dataclass(frozen=True)
class ReservationSweepReport:
    reclaimed_leases: tuple[RunId, ...]
    spend_unknown: tuple[RunId, ...]


@dataclass
class RecoveryCoordinator:
    supervisor: ProcessSupervisor
    control: RecoveryControl
    worktrees: WorktreeRecovery
    commands: CommandRecovery
    spend: SpendReconciler
    abandonments: RunAbandonmentSink
    owner_id: str
    reservation_grace: timedelta = timedelta(minutes=2)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def recover(self) -> RecoveryReport:
        killed = self.supervisor.recover_orphans()
        reclaimed = self.control.reclaim_dead_leases(
            self.owner_id, _pid_alive
        )
        validated_worktrees = self.worktrees.prune_validate()
        recovered_commands = self.commands.drain_pending_commands()
        sweep = self._sweep(timedelta(0), reclaimed)
        return RecoveryReport(
            killed,
            sweep.reclaimed_leases,
            validated_worktrees,
            recovered_commands,
            sweep.spend_unknown,
            len(self.control.pending_commands()),
        )

    def recover_after_suspend(self) -> RecoveryReport:
        """Recover after wake without reaping a process owned by this daemon."""

        killed = self.supervisor.recover_abandoned_orphans()
        reclaimed = self.control.reclaim_dead_leases(
            self.owner_id, _pid_alive
        )
        validated_worktrees = self.worktrees.prune_validate()
        recovered_commands = self.commands.drain_pending_commands()
        sweep = self._sweep(self.reservation_grace, reclaimed)
        return RecoveryReport(
            killed,
            sweep.reclaimed_leases,
            validated_worktrees,
            recovered_commands,
            sweep.spend_unknown,
            len(self.control.pending_commands()),
        )

    def sweep_reservations(self) -> ReservationSweepReport:
        """Reconcile or release stale capacity without process-group reaping."""

        return self._sweep(self.reservation_grace)

    def _sweep(
        self,
        grace: timedelta,
        reclaimed_leases: tuple[RunId, ...] | None = None,
    ) -> ReservationSweepReport:
        reclaimed = (
            self.control.reclaim_dead_leases(self.owner_id, _pid_alive)
            if reclaimed_leases is None
            else reclaimed_leases
        )
        live_runs = {
            lease.run_id for lease in self.control.leases(self.owner_id)
        }
        cutoff = self.now() - grace
        candidates = self.control.stale_active_reservation_runs(
            self.owner_id, cutoff
        )
        unknown: list[RunId] = []
        abandoned = set(reclaimed)
        for run_id in candidates:
            state = ReservationRecoveryState.observed(
                live_lease=run_id in live_runs,
                lease_reclaimed=run_id in reclaimed,
            )
            while True:
                decision = decide_reservation_recovery(state)
                if decision.action is ReservationRecoveryAction.PRESERVE:
                    break
                if decision.action is ReservationRecoveryAction.RECONCILE_SPEND:
                    state = state.after_spend_reconciliation(
                        settled=self.spend.reconcile(run_id)
                    )
                    continue
                if decision.action is ReservationRecoveryAction.RELEASE_UNLAUNCHED:
                    result = self.control.release_unlaunched(
                        run_id, "unlaunched_reservation"
                    )
                    state = state.after_unlaunched_release(
                        released=result.disposition.value == "available"
                    )
                    continue
                if decision.action is ReservationRecoveryAction.RELEASE_RETAINED:
                    result = self.control.release_retained(
                        run_id,
                        "unmeasured_completed_reservation",
                        cutoff,
                    )
                    state = state.after_retained_release(
                        released=result.disposition.value == "available"
                    )
                    continue
                if decision.action is not ReservationRecoveryAction.COMPLETE:
                    raise AssertionError(
                        f"unsupported reservation recovery action: "
                        f"{decision.action}"
                    )
                if decision.report_spend_unknown:
                    unknown.append(run_id)
                if decision.record_abandonment:
                    abandoned.add(run_id)
                break
        for run_id in sorted(abandoned):
            try:
                self.abandonments.abandon_run(
                    run_id, "process ownership lost"
                )
            except Exception:
                pass
        return ReservationSweepReport(reclaimed, tuple(unknown))


class NoWorktreeRecovery:
    """Null object for roles that never own issue worktrees."""

    def prune_validate(self) -> int:
        return 0


def _pid_alive(pid: int, start_ticks: int) -> bool:
    return DurableProcessSupervisor.matches_identity(pid, start_ticks)
