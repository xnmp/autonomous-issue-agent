"""Compile-time assertions for authority-bearing hexagonal seams.

The repository is migrating a large legacy annotation backlog to Pyrefly's
default preset. These assignments deliberately re-enable structural checks for
the ports where a drift could skip a durable write or corrupt control state.
"""

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    Lease,
    PendingCommand,
    ReleaseResult,
    SQLiteControlStore,
)
from issue_agent.adapters.dashweb.ghdata import GhReader
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.github import GitHubGateway, MergedRef
from issue_agent.adapters.journal import LegacyJournalLocator
from issue_agent.adapters.process_supervisor import (
    ProcessSupervisor as DurableProcessSupervisor,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.adapters.store import Store
from issue_agent.adapters.typed_journal import TypedJournal
from issue_agent.adapters.worktree import Worktrees
from issue_agent.app.execute import Executor
from issue_agent.app.dashweb.ports import GitHubQueries
from issue_agent.app.infra_breaker import InfraBreaker
from issue_agent.app.recovery import NoWorktreeRecovery
from issue_agent.app.roles import (
    _LateReservationReconciler,
    _SweepReservationReconciler,
)
import issue_agent.domain.workflow_ports as workflow_ports
from issue_agent.domain.ports import (
    ActionExecutor,
    CiPromptReads,
    CommandRecovery,
    ControlStore,
    EventSink,
    FixWorktreePort,
    GitHubReads,
    InfraBreakerPort,
    IssueStateReasonReads,
    LegacyJournal,
    LevelState,
    PendingEffect,
    PendingEffectReads,
    PredictionReader,
    ProcessSupervisor,
    RecentlyMergedRef,
    RecentMergesReads,
    RecoveryControl,
    RecoveryDisposition,
    RecoveryLease,
    RecoveryRelease,
    ReviewPromptReads,
    RunAbandonmentSink,
    SpendReconciler,
    StaleReservationReconciler,
    WorkflowGitHubReads,
    WorkflowState,
    WorktreePort,
    WorktreeRecovery,
    WorldObservationStore,
)

# An AST contract requires every Protocol in domain/ports.py to appear in
# exactly one set. Exemptions are limited to obsolete/injected abstractions
# without one production adapter; all authority-bearing ports stay structural.
COVERED_PORTS = frozenset(
    {
        "ActionExecutor",
        "CiPromptReads",
        "CommandRecovery",
        "ControlStore",
        "EventSink",
        "FixWorktreePort",
        "GitHubReads",
        "InfraBreakerPort",
        "IssueStateReasonReads",
        "LegacyJournal",
        "LevelState",
        "PendingEffect",
        "PendingEffectReads",
        "PredictionReader",
        "ProcessSupervisor",
        "RecentlyMergedRef",
        "RecentMergesReads",
        "RecoveryControl",
        "RecoveryDisposition",
        "RecoveryLease",
        "RecoveryRelease",
        "ReviewPromptReads",
        "RunAbandonmentSink",
        "SpendReconciler",
        "StaleReservationReconciler",
        "WorkflowGitHubReads",
        "WorkflowState",
        "WorktreePort",
        "WorktreeRecovery",
        "WorldObservationStore",
    }
)
EXEMPT_PORTS = frozenset(
    {
        # Time is injected as three callables in the current daemon loop.
        "Clock",
        # Superseded by ReservationRequest/SettlementRequest control-store APIs.
        "SpendAccount",
    }
)


def workflow_state(store: Store) -> WorkflowState:
    return store


dashboard_github_queries: GitHubQueries = GhReader()


def control_store(store: SQLiteControlStore) -> ControlStore:
    return store


def recovery_control(store: SQLiteControlStore) -> RecoveryControl:
    return store


def level_state(store: SQLiteControlStore) -> LevelState:
    return store


def pending_effect_reads(store: SQLiteControlStore) -> PendingEffectReads:
    return store


def pending_effect(pending: PendingCommand) -> PendingEffect:
    return pending


def world_observations(
    store: SQLiteControlStore,
) -> WorldObservationStore:
    return store


def fix_worktrees(worktrees: Worktrees) -> FixWorktreePort:
    return worktrees


def worktrees(worktrees: Worktrees) -> WorktreePort:
    return worktrees


def github_reads(gateway: GitHubGateway) -> GitHubReads:
    return gateway


def workflow_github_reads(
    gateway: GitHubGateway,
) -> WorkflowGitHubReads:
    return gateway


def issue_state_reason(
    gateway: GitHubGateway,
) -> IssueStateReasonReads:
    return gateway


def recent_merges(gateway: GitHubGateway) -> RecentMergesReads:
    return gateway


def review_prompts(gateway: GitHubGateway) -> ReviewPromptReads:
    return gateway


def ci_prompts(gateway: GitHubGateway) -> CiPromptReads:
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


def infra_breaker(breaker: InfraBreaker) -> InfraBreakerPort:
    return breaker


def event_sink(journal: TypedJournal) -> EventSink:
    return journal


def abandonment_sink(journal: TypedJournal) -> RunAbandonmentSink:
    return journal


def worktree_recovery(
    recovery: NoWorktreeRecovery,
) -> WorktreeRecovery:
    return recovery


def legacy_journal(locator: LegacyJournalLocator) -> LegacyJournal:
    return locator


def prediction_reader(store: Store) -> PredictionReader:
    return store


def recently_merged_ref(ref: MergedRef) -> RecentlyMergedRef:
    return ref


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
