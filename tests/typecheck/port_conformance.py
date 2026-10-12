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
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.github import GitHubGateway
from issue_agent.adapters.journal import Journal
from issue_agent.adapters.process_supervisor import (
    ProcessSupervisor as DurableProcessSupervisor,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.store import Store
from issue_agent.app.execute import Executor
from issue_agent.app.dashweb.ports import GitHubQueries
from issue_agent.app.recovery import NoWorktreeRecovery
from issue_agent.app.roles import (
    _LateReservationReconciler,
    _SweepReservationReconciler,
)
import issue_agent.domain.workflow_ports as workflow_ports
from issue_agent.domain.ports import (
    ActionExecutor,
    CommandRecovery,
    EventSink,
    GitHubReads,
    ProcessSupervisor,
    RecoveryControl,
    RecoveryDisposition,
    RecoveryLease,
    RecoveryRelease,
    SpendReconciler,
    StaleReservationReconciler,
    WorkflowGitHubReads,
    WorkflowState,
    WorktreeRecovery,
)

# An AST contract requires every Protocol in domain/ports.py to appear in
# exactly one set. Exemptions are limited to obsolete/injected abstractions
# without one production adapter; all authority-bearing ports stay structural.
COVERED_PORTS = frozenset(
    {
        "ActionExecutor",
        "CommandRecovery",
        "EventSink",
        "GitHubReads",
        "ProcessSupervisor",
        "RecoveryControl",
        "RecoveryDisposition",
        "RecoveryLease",
        "RecoveryRelease",
        "SpendReconciler",
        "StaleReservationReconciler",
        "WorkflowGitHubReads",
        "WorkflowState",
        "WorktreeRecovery",
    }
)
EXEMPT_PORTS = frozenset(
    {
        # Time is injected as three callables in the current daemon loop.
        "Clock",
    }
)


def workflow_state(store: Store) -> WorkflowState:
    return store


dashboard_github_queries: GitHubQueries = GhReader()



def recovery_control(store: SQLiteControlStore) -> RecoveryControl:
    return store






def github_reads(gateway: GitHubGateway) -> GitHubReads:
    return gateway


def workflow_github_reads(
    gateway: GitHubGateway,
) -> WorkflowGitHubReads:
    return gateway




def action_executor(
    executor: Executor,
) -> ActionExecutor:
    return executor


def command_recovery(
    executor: GitHubEffectExecutor,
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


# Workflow authority ports live in domain/workflow_ports.py, outside the
# domain/ports.py manifest above; the qualified name keeps them out of it.
def review_workflow_control(
    control: SQLiteReviewWorkflowControl,
) -> workflow_ports.ReviewWorkflowControl:
    return control
