"""Bound repository/owner ports for resolve workflow control; ADR 0015."""

from typing import Protocol
from contextlib import AbstractContextManager

from .contracts import CommandId, WorkflowAttemptIdentity
from .resolve_return import PendingResolveReturn, ReturnedResolve, ResolveCleanupCompleted, UnavailableResolveReturn
from .resolve_split import ContractResetCompleted, ResetIntent, ReauthorResetIntent, ContractRevision
from .resolve_workspace import ObservedResolveBase, ResolveWorkspaceObserved, EpisodeWorkspacePrepared
from .resolve_rearm import PendingResolveRearm, ResolveControlRead, UnavailableResolveRearm
from .workflow import (
    AuthorizeAbsentRetry, ReconciliationOnly, UnconsumedRetryAuthorization, WorkflowRejected,
    AuthorizedOperatorCommand, CancelRearm, OperatorActor,
    HeldProjection, RearmIntent, RetryAlreadyConsumed, RetryDeclined, RetryStale,
    RetryBegan, ObservedIssue, NoLinkedPr, LinkedPrPresent, ResolveAttemptResult, WorkflowChanged, WorkflowCommitted, WorkflowConflict, WorkflowRead, WorkflowSnapshot,
)
from .workflow_retry import RetryAbsenceRead
from .workflow_identity import (
    FamilyAuthority, WorkflowBinding, WorkflowKey, WorkflowRevision, WorkflowWriter,
)


class WorkflowControl(Protocol):
    """Mutations validate the bound active owner and scope inside their transaction.

    Corruption raises. Verified absence and per-target unavailability are distinct
    read variants. No method accepts an operator-supplied owner identity.
    """

    @property
    def writer(self) -> WorkflowWriter: ...

    def load_workflow(self, key: WorkflowKey) -> WorkflowRead: ...

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

