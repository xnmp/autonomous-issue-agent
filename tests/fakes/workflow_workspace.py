"""Typed workspace fixtures only; callers still establish persisted authority."""

from datetime import datetime

from issue_agent.domain.resolve_split import ContractResetResult
from issue_agent.domain.resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, NoPriorResolveAdmission,
    ObservedResolveBase, ResolveWorkspaceObserved, ResolveWorkspacePredecessor,
)
from issue_agent.domain.workflow_identity import WorkflowBinding


def workspace_observation(
    binding: WorkflowBinding, observed_at: datetime, *,
    predecessor: ResolveWorkspacePredecessor | None = None, branch: str | None = None,
) -> ResolveWorkspaceObserved:
    return ResolveWorkspaceObserved(
        binding, predecessor if predecessor is not None else NoPriorResolveAdmission(binding),
        branch or f"fix/{binding.fence.key.target.number}-workspace",
        ObservedResolveBase(binding.fence.key.repository, "refs/heads/dev", "a" * 40, observed_at),
    )


def workspace_ack(
    handoff: EpisodeWorkspaceHandoff, observed_at: datetime, *,
    performed_under: WorkflowBinding | None = None,
) -> EpisodeWorkspacePrepared:
    return EpisodeWorkspacePrepared(
        handoff, performed_under or handoff.destination,
        ContractResetResult(handoff.revision, handoff.archive_ref), observed_at,
    )
