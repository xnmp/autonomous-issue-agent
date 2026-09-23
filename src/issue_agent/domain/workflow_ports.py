"""Bound repository/owner ports for resolve workflow control; ADR 0015."""

from typing import Protocol
from contextlib import AbstractContextManager

from .contracts import CommandId, WorkflowAttemptIdentity
from .resolve_return import PendingResolveReturn, ReturnedResolve, ResolveCleanupCompleted, UnavailableResolveReturn
from .workflow import (
    CommandAbsenceEvidence, CommandRetryApplicability, HeldProjection, RearmIntent, RetryAlreadyConsumed,
    RetryBegan, ObservedIssue, NoLinkedPr, LinkedPrPresent, ResolveAttemptResult, WorkflowChanged, WorkflowCommitted, WorkflowConflict, WorkflowRead,
)
from .workflow_identity import (
    FamilyAuthority, WorkflowKey, WorkflowRevision, WorkflowWriter,
)


class WorkflowControl(Protocol):
    """Mutations validate the bound active owner and scope inside their transaction.

    Corruption raises. Verified absence and per-target unavailability are distinct
    read variants. No method accepts an operator-supplied owner identity.
    """

    @property
    def writer(self) -> WorkflowWriter: ...

    def load_workflow(self, key: WorkflowKey) -> WorkflowRead: ...

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

    def finalize_return(
        self, returned: ReturnedResolve, issue: ObservedIssue,
        linkage: NoLinkedPr | LinkedPrPresent,
        *, cleanup: ResolveCleanupCompleted | None = None,
    ) -> WorkflowCommitted | WorkflowConflict: ...

    def lookup_rearm(
        self, key: WorkflowKey, request_id: str
    ) -> RearmIntent | None: ...

    def held_projection(self, key: WorkflowKey) -> HeldProjection: ...


class WorkflowRetryControl(Protocol):
    @property
    def writer(self) -> WorkflowWriter: ...

    def begin_authorized_retry(
        self,
        command_id: CommandId,
        authorization_id: str,
        expected: WorkflowRevision,
        evidence: CommandAbsenceEvidence,
        applicability: CommandRetryApplicability,
    ) -> RetryBegan | RetryAlreadyConsumed: ...
