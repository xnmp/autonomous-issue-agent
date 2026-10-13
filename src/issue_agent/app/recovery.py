"""Idempotent boot, wake, and periodic recovery coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..adapters.process_supervisor import ProcessSupervisor as DurableProcessSupervisor
from ..domain.contracts import RunId
from ..domain.recovery import (
    RecoveryArm,
    RecoveryGrace,
    RecoveryTrigger,
    recovery_plan,
)
from ..domain.reservation_recovery import (
    ReservationRecoveryAction,
    ReservationRecoveryState,
    decide_reservation_recovery,
)
from ..domain.ports import (
    ProcessSupervisor,
    RecoveryControl,
    SpendReconciler,
    WorktreeRecovery,
)


@dataclass(frozen=True)
class RecoveryReport:
    trigger: RecoveryTrigger
    killed_orphans: tuple[RunId, ...]
    reclaimed_leases: tuple[RunId, ...]
    validated_worktrees: int
    spend_unknown: tuple[RunId, ...]


@dataclass
class RecoveryCoordinator:
    supervisor: ProcessSupervisor
    control: RecoveryControl
    worktrees: WorktreeRecovery
    spend: SpendReconciler
    owner_id: str
    reservation_grace: timedelta = timedelta(minutes=2)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def reconcile(self, trigger: RecoveryTrigger) -> RecoveryReport:
        """Execute the trigger-selected arm set from ADR 0006."""

        plan = recovery_plan(trigger)
        arms = plan.coordinator_arms
        killed: tuple[RunId, ...] = ()
        if RecoveryArm.REAP_ALL_PROCESSES in arms:
            killed = self.supervisor.recover_orphans()
        elif RecoveryArm.REAP_ABANDONED_PROCESSES in arms:
            killed = self.supervisor.recover_abandoned_orphans()

        reclaimed: tuple[RunId, ...] = ()
        if RecoveryArm.RECLAIM_LEASES in arms:
            reclaimed = self.control.reclaim_dead_leases(
                self.owner_id, _pid_alive
            )

        validated_worktrees = (
            self.worktrees.prune_validate()
            if RecoveryArm.VALIDATE_WORKTREES in arms
            else 0
        )
        spend_unknown: tuple[RunId, ...] = ()
        if RecoveryArm.SWEEP_RESERVATIONS in arms:
            grace = (
                timedelta(0)
                if plan.reservation_grace is RecoveryGrace.IMMEDIATE
                else self.reservation_grace
            )
            reclaimed, spend_unknown = self._sweep(grace, reclaimed)

        return RecoveryReport(
            trigger,
            killed,
            reclaimed,
            validated_worktrees,
            spend_unknown,
        )

    def _sweep(
        self,
        grace: timedelta,
        reclaimed_leases: tuple[RunId, ...],
    ) -> tuple[tuple[RunId, ...], tuple[RunId, ...]]:
        reclaimed = reclaimed_leases
        live_runs = {
            lease.run_id for lease in self.control.leases() if lease.run_id is not None
        }
        cutoff = self.now() - grace
        candidates = self.control.stale_active_reservation_runs(
            self.owner_id, cutoff
        )
        unknown: list[RunId] = []
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
                        settled=(self.spend.reconcile(run_id) or
                                 self.control.settle_terminal_estimate(run_id, "terminal recovery has no usable request evidence"))
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
                break
        return reclaimed, tuple(unknown)


class NoWorktreeRecovery:
    """Null object for roles that never own issue worktrees."""

    def prune_validate(self) -> int:
        return 0


def _pid_alive(pid: int, start_ticks: int) -> bool:
    return DurableProcessSupervisor.matches_identity(pid, start_ticks)
