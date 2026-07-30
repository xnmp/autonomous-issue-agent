"""Idempotent boot/suspend recovery coordinator."""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.process_supervisor import ProcessSupervisor as DurableProcessSupervisor
from ..domain.contracts import RunId
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


@dataclass
class RecoveryCoordinator:
    supervisor: ProcessSupervisor
    control: RecoveryControl
    worktrees: WorktreeRecovery
    commands: CommandRecovery
    spend: SpendReconciler
    abandonments: RunAbandonmentSink
    owner_id: str

    def recover(self) -> RecoveryReport:
        killed = self.supervisor.recover_orphans()
        reclaimed = self.control.reclaim_dead_leases(
            self.owner_id, _pid_alive
        )
        validated_worktrees = self.worktrees.prune_validate()
        recovered_commands = self.commands.drain_pending_commands()
        unknown: list[RunId] = []
        live_runs = {
            lease.run_id for lease in self.control.leases(self.owner_id)
        }
        abandoned = set(reclaimed)
        for run_id in self.control.active_reservation_runs(self.owner_id):
            if run_id not in reclaimed and run_id in live_runs:
                continue
            abandoned.add(run_id)
            if self.spend.reconcile(run_id):
                continue
            result = self.control.release(run_id, "spend_unknown")
            if result.disposition.value == "available":
                unknown.append(run_id)
        for run_id in sorted(abandoned):
            self.abandonments.abandon_run(run_id, "process ownership lost")
        return RecoveryReport(
            killed,
            reclaimed,
            validated_worktrees,
            recovered_commands,
            tuple(unknown),
            len(self.control.pending_commands()),
        )


class NoWorktreeRecovery:
    """Null object for roles that never own issue worktrees."""

    def prune_validate(self) -> int:
        return 0


def _pid_alive(pid: int, start_ticks: int) -> bool:
    return DurableProcessSupervisor.matches_identity(pid, start_ticks)
