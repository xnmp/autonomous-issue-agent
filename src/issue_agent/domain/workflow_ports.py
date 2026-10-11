"""Bound repository/owner ports for resolve and review-rejection workflow control; ADR 0015."""

from typing import Protocol, runtime_checkable
from contextlib import AbstractContextManager

from .contracts import CommandId, LifecycleCommand, WorkflowAttemptIdentity
from .continuation_components import CIEpisodeReset, SelectedIssueRecovery, RebaseAllowanceReset, IssueRecoverySelection, PRRepairSelection, PRRepairCleanup, RepairHandoff, StewardSelection, StewardAttemptSource, StewardReturned
from .world import IssueView
from .agent_task import RuntimeName
from .resolve_return import PendingResolveReturn, ReturnedResolve, ResolveCleanupCompleted, UnavailableResolveReturn
from .resolve_split import ContractResetCompleted, ResetIntent, ReauthorResetIntent, ContractRevision
from .resolve_workspace import ObservedResolveBase, ResolveWorkspaceObserved, EpisodeWorkspacePrepared
from .resolve_rearm import PendingResolveRearm, ResolveControlRead, UnavailableResolveRearm
from .workflow import (
    AuthorizeAbsentRetry, ReconciliationOnly, UnconsumedRetryAuthorization, WorkflowRejected,
    AuthorizedOperatorCommand, CancelRearm, ContinueGrantedRearm, OperatorActor, RequestRearm,
    HeldProjection, RearmIntent, RetryAlreadyConsumed, RetryDeclined, RetryStale,
    RetryBegan, ObservedIssue, NoLinkedPr, LinkedPrPresent, ResolveAttemptResult, WorkflowChanged, WorkflowCommitted, WorkflowConflict, WorkflowRead, WorkflowSnapshot,
    WorkflowUnavailable,
)
from .workflow_retry import RetryAbsenceRead
from .review_rearm import PendingReviewRecovery, ReleaseProjection, ReviewControlRead
from .review_workflow import ReviewWorkflowRead
from .workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowKey, WorkflowRevision, WorkflowWriter,
)


@runtime_checkable
class StewardControl(Protocol):
    def read_steward(self, key: WorkflowKey) -> StewardSelection: ...
    def steward_admission(self, attempt: WorkflowAttemptIdentity) -> StewardAttemptSource: ...
    def record_steward_outcome(self, outcome: StewardReturned, commands: tuple[LifecycleCommand, ...] = ()) -> bool: ...
    def complete_steward_delivery(self, source: StewardAttemptSource, observed: IssueView) -> bool: ...


class WorkflowControl(Protocol):
    """Mutations validate the bound active owner and scope inside their transaction.

    Corruption raises. Verified absence and per-target unavailability are distinct
    read variants. No method accepts an operator-supplied owner identity.
    """

    @property
    def writer(self) -> WorkflowWriter: ...

    def load_workflow(self, key: WorkflowKey) -> WorkflowRead: ...

    def read_pr_repair(self, key: WorkflowKey) -> PRRepairSelection: ...

    def read_issue_recovery(self, key: WorkflowKey) -> IssueRecoverySelection: ...

    def fix_admission_refusals(self, selected: SelectedIssueRecovery) -> int:
        """Refusals minted before admission (no run, no receipt), scoped to the
        selected recovery episode; they spend the infra-death ceiling (#262)."""
        ...

    def reset_ci_episode(self, source: CIEpisodeReset) -> bool: ...

    def reset_rebase_allowance(self, source: RebaseAllowanceReset) -> bool: ...
    def consume_blank_pr_reply(self, binding: WorkflowBinding, reply_at: str, body: str) -> bool: ...

    def reconcile_pr_repair_returns(self) -> int: ...

    def pr_repair_handoff(self, issue: int, pr: PullRequestRef, branch: str, runtime: RuntimeName) -> RepairHandoff | None: ...

    def cleanup_pr_repair(self, key: WorkflowKey, merged: bool, source: PRRepairCleanup) -> None: ...

    def consume_blank_issue_reply(self, binding: WorkflowBinding, reply_at: str, body: str) -> bool: ...

    def resolve_control_view(self, key: WorkflowKey, *, request_id: str | None = None) -> ResolveControlRead: ...

    def ensure_observed_workflow(self, observation: ObservedIssue) -> WorkflowCommitted: ...

    def commit_workflow(
        self,
        expected: WorkflowRevision,
        authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def record_attempt(
        self, result: ResolveAttemptResult
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def capture_return(self, returned: ReturnedResolve) -> WorkflowCommitted | WorkflowConflict: ...

    def returned_result(self, attempt: WorkflowAttemptIdentity) -> ReturnedResolve | None: ...

    def pending_returns(self, *, limit: int = 32) -> tuple[PendingResolveReturn | UnavailableResolveReturn, ...]: ...

    def defer_return(self, returned: ReturnedResolve, error: str) -> None: ...

    def return_effect_authority(self, returned: ReturnedResolve) -> AbstractContextManager[None]: ...

    def reauthor_reset_intent(self, binding: WorkflowBinding, input_fingerprint: str) -> ReauthorResetIntent | None: ...

    def contract_reset_authority(self, intent: ResetIntent) -> AbstractContextManager[ContractResetCompleted | None]: ...

    def complete_contract_reset(self, completed: ContractResetCompleted) -> ContractResetCompleted: ...

    def rearm_authority(self, expected: WorkflowRevision, *, binding: WorkflowBinding | None = None) -> AbstractContextManager[WorkflowSnapshot]: ...

    def observe_workspace(
        self, binding: WorkflowBinding, base: ObservedResolveBase, branch: str,
    ) -> ResolveWorkspaceObserved: ...

    def workspace_handoff_authority(
        self, handoff_id: str, expected: WorkflowRevision,
    ) -> AbstractContextManager[EpisodeWorkspacePrepared | None]: ...

    def complete_workspace_handoff(self, completed: EpisodeWorkspacePrepared) -> EpisodeWorkspacePrepared: ...

    def prepared_workspace(self, expected: WorkflowRevision) -> EpisodeWorkspacePrepared | None: ...

    def accepted_rearm(self, key: WorkflowKey, request_id: str) -> AuthorizedOperatorCommand | None: ...

    def pending_rearms(self, *, limit: int = 32) -> tuple[PendingResolveRearm | UnavailableResolveRearm, ...]: ...

    def defer_rearm(self, expected: WorkflowRevision, error: str) -> None: ...

    def reconcile_rearm(
        self, expected: WorkflowRevision, issue: ObservedIssue,
        linkage: NoLinkedPr | LinkedPrPresent, *, external_holds: tuple[str, ...] = (),
        workspace_verified: EpisodeWorkspacePrepared | None = None,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def cancel_rearm(self, command: CancelRearm, actor: OperatorActor, *, binding: WorkflowBinding | None = None) -> WorkflowCommitted | WorkflowConflict: ...

    def authorize_retry(self, command: AuthorizeAbsentRetry, actor: OperatorActor, *, binding: WorkflowBinding | None = None) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected: ...

    def finalize_return(
        self, returned: ReturnedResolve, issue: ObservedIssue,
        linkage: NoLinkedPr | LinkedPrPresent,
        *, cleanup: ResolveCleanupCompleted | None = None,
        contract_revision: ContractRevision | None = None,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def lookup_rearm(
        self, key: WorkflowKey, request_id: str
    ) -> RearmIntent | None: ...

    def held_projection(self, key: WorkflowKey) -> HeldProjection: ...


class ReviewWorkflowControl(Protocol):
    """PR-scoped review-rejection authority bound to the repository's review owner.

    Reads distinguish absence, unavailability and a never-activated family
    (`WorkflowMigrationRequired(family=REVIEW_REJECTIONS)`). Acceptance takes
    the deployment barriers, refreshes facts read-only through the review
    gateway and commits local intent only; the preflight is minted by the
    control from that read and is never a caller argument. No method accepts
    an owner identity.
    """

    @property
    def writer(self) -> WorkflowWriter: ...

    def load_workflow(self, key: WorkflowKey) -> ReviewWorkflowRead: ...

    def review_control_view(
        self, key: WorkflowKey, *, request_id: str | None = None,
    ) -> ReviewControlRead | WorkflowUnavailable: ...

    def commit_workflow(
        self,
        expected: WorkflowRevision,
        authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def request_rearm(
        self, command: RequestRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected: ...

    def continue_rearm(
        self, command: ContinueGrantedRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected: ...

    def cancel_rearm(self, command: CancelRearm, actor: OperatorActor) -> WorkflowCommitted | WorkflowConflict: ...

    def authorize_retry(
        self, command: AuthorizeAbsentRetry, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected: ...

    def pending_rearms(self, *, limit: int = 32) -> tuple[PendingReviewRecovery | UnavailableResolveRearm, ...]: ...

    def reconcile_rearm(self, expected: WorkflowRevision) -> WorkflowCommitted | WorkflowConflict: ...

    def release_projection(self, key: WorkflowKey) -> ReleaseProjection | None: ...

    def reconcile_release(self, key: WorkflowKey) -> ReleaseProjection | None: ...

    def reconcile_pending_rearms(self, *, limit: int = 32) -> int: ...


class WorkflowRetryControl(Protocol):
    """Effect-store seam spending one operator grant (ADR 0015).

    `expected` is the command's reconciliation state and `expected_attempts`
    its attempt count, both observed before the executor's read: a read taken
    before another attempt began can never spend or settle after it. The
    grant's binding pins generation, epoch and episode. A decision leaves the
    grant terminal; a stale or unavailable read leaves it open.
    """

    def retry_authorization(self, command_id: CommandId) -> UnconsumedRetryAuthorization | None: ...

    def begin_authorized_retry(
        self,
        command_id: CommandId,
        authorization_id: str,
        *,
        expected: ReconciliationOnly,
        expected_attempts: int,
        read: RetryAbsenceRead,
        executor_identity: str,
    ) -> RetryBegan | RetryAlreadyConsumed | RetryDeclined | RetryStale: ...
