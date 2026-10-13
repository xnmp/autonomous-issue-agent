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
from issue_agent.adapters.worktree import Worktrees
from issue_agent.app.recovery import RecoveryCoordinator
from issue_agent.domain.ports import (
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

# An AST contract requires every Protocol in domain/ports.py to appear here
# with a structural check against its production adapter.
COVERED_PORTS = frozenset(
    {
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


dashboard_github_queries: GitHubQueries = GhReader()


def recovery_control(store: SQLiteControlStore) -> RecoveryControl:
    return store


def process_supervisor(
    supervisor: DurableProcessSupervisor,
) -> ProcessSupervisor:
    return supervisor


def spend_reconciler(
    reconciler: CodexSpendReconciler,
) -> SpendReconciler:
    return reconciler


def stale_reservation_reconciler(
    reconciler: RecoveryCoordinator,
) -> StaleReservationReconciler:
    return reconciler


def event_sink(journal: Journal) -> EventSink:
    return journal


def worktree_recovery(
    recovery: Worktrees,
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
