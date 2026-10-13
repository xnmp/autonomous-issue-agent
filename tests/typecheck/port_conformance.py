"""Compile-time assertions for authority-bearing hexagonal seams.

The repository is migrating a large legacy annotation backlog to Pyrefly's
default preset. These assignments deliberately re-enable structural checks for
the ports where a drift could skip a durable write or corrupt control state.
"""

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    Lease,
    ReleaseResult,
    SQLiteControlStore,
)
from issue_agent.adapters.dashweb.ghdata import GhReader
from issue_agent.adapters.journal import Journal
from issue_agent.adapters.process_supervisor import (
    ProcessSupervisor as DurableProcessSupervisor,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.app.dashweb.ports import GitHubQueries
from issue_agent.app.recovery import NoWorktreeRecovery
from issue_agent.app.roles import (
    _LateReservationReconciler,
    _NoCommands,
    _SweepReservationReconciler,
)
from issue_agent.domain.ports import (
    CommandRecovery,
    EventSink,
    ProcessSupervisor,
    RecoveryControl,
    RecoveryDisposition,
    RecoveryLease,
    RecoveryRelease,
    SpendReconciler,
    StaleReservationReconciler,
    WorktreeRecovery,
)

# An AST contract requires every Protocol in domain/ports.py to appear in
# exactly one set. Exemptions are limited to obsolete/injected abstractions
# without one production adapter; all authority-bearing ports stay structural.
COVERED_PORTS = frozenset(
    {
        "CommandRecovery",
        "EventSink",
        "ProcessSupervisor",
        "RecoveryControl",
        "RecoveryDisposition",
        "RecoveryLease",
        "RecoveryRelease",
        "SpendReconciler",
        "StaleReservationReconciler",
        "WorktreeRecovery",
    }
)
EXEMPT_PORTS = frozenset(
    {
        # Time is injected as three callables in the current daemon loop.
        "Clock",
    }
)


dashboard_github_queries: GitHubQueries = GhReader()


def recovery_control(store: SQLiteControlStore) -> RecoveryControl:
    return store


def command_recovery(
    executor: _NoCommands,
) -> CommandRecovery:
    return executor


def process_supervisor(
    supervisor: DurableProcessSupervisor,
) -> ProcessSupervisor:
    return supervisor


def spend_reconciler(
    reconciler: CodexSpendReconciler,
) -> SpendReconciler:
    return reconciler


def sweep_reservation_reconciler(
    reconciler: _SweepReservationReconciler,
) -> StaleReservationReconciler:
    return reconciler


def late_reservation_reconciler(
    reconciler: _LateReservationReconciler,
) -> StaleReservationReconciler:
    return reconciler


def event_sink(journal: Journal) -> EventSink:
    return journal


def worktree_recovery(
    recovery: NoWorktreeRecovery,
) -> WorktreeRecovery:
    return recovery


def recovery_lease(lease: Lease) -> RecoveryLease:
    return lease


def recovery_disposition(
    disposition: AccountingDisposition,
) -> RecoveryDisposition:
    return disposition


def recovery_release(release: ReleaseResult) -> RecoveryRelease:
    return release

