"""Exact workspace obligations for a new resolve allowance, not AUTHOR evidence.

Adapters establish upstream observation and predecessor absence. Construction
checks scope, but does not itself grant permission to mutate a checkout.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from .contracts import WorkflowAttemptIdentity
from .refs import issue_from_branch
from .resolve_split import ContractResetResult, ContractRevision, validate_git_object_id
from .workflow_identity import (
    WorkflowBinding, WorkflowFamily, canonical_workflow_repository, validate_workflow_identifier,
    validate_workflow_integer,
)


def _time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("workspace observation requires an aware datetime")


def _digest(parts: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _binding(binding: WorkflowBinding) -> tuple[object, ...]:
    return (binding.fence.key.repository, "issue", binding.fence.key.target.number,
            binding.authority.family.value, binding.authority.epoch,
            binding.fence.generation, binding.episode_id)


@dataclass(frozen=True)
class ObservedResolveBase:
    repository: str
    base_ref: str
    commit_sha: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", canonical_workflow_repository(self.repository))
        validate_workflow_identifier(self.base_ref, "configured upstream branch")
        # The adapter additionally applies Git's exact check-ref-format rules.
        if not self.base_ref.startswith("refs/heads/") or not self.base_ref.removeprefix("refs/heads/"):
            raise ValueError("workspace base must name an exact upstream branch ref")
        validate_git_object_id(self.commit_sha)
        _time(self.observed_at)

    @property
    def pin_ref(self) -> str:
        return "refs/issue-agent/episode-bases/" + _digest((self.repository, self.base_ref, self.commit_sha))


@dataclass(frozen=True)
class NoPriorResolveAdmission:
    """Trusted absence must be checked against retained admission authority."""

    binding: WorkflowBinding

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding):
            raise ValueError("prior admission absence requires its workflow binding")


@dataclass(frozen=True)
class PriorResolveAdmission:
    attempt: WorkflowAttemptIdentity
    admission_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity):
            raise ValueError("workspace predecessor requires its admitted attempt")
        validate_workflow_integer(self.admission_version, "workspace predecessor admission", minimum=1)


ResolveWorkspacePredecessor = NoPriorResolveAdmission | PriorResolveAdmission


@dataclass(frozen=True)
class EpisodeWorkspaceHandoff:
    grant_request_id: str
    source: WorkflowBinding
    destination: WorkflowBinding
    predecessor: ResolveWorkspacePredecessor
    branch: str
    base: ObservedResolveBase

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.grant_request_id, "workspace grant request")
        if not isinstance(self.source, WorkflowBinding) or not isinstance(self.destination, WorkflowBinding):
            raise ValueError("workspace handoff requires source and destination bindings")
        if (self.source.authority.family is not WorkflowFamily.RESOLVE
                or self.source.fence.key != self.destination.fence.key
                or self.source.authority != self.destination.authority
                or self.destination.fence.generation != self.source.fence.generation + 1
                or self.source.episode_id == self.destination.episode_id):
            raise ValueError("workspace handoff requires one new generation and a new episode in the same authority")
        if not isinstance(self.base, ObservedResolveBase) or self.base.repository != self.source.fence.key.repository:
            raise ValueError("workspace handoff base belongs to another repository")
        ContractRevision(self.branch, self.base.commit_sha)
        if issue_from_branch(self.branch) != self.source.fence.key.target.number:
            raise ValueError("workspace handoff branch belongs to another issue")
        prior = self.predecessor
        if isinstance(prior, NoPriorResolveAdmission):
            if prior.binding != self.source:
                raise ValueError("workspace absence belongs to another source binding")
        elif isinstance(prior, PriorResolveAdmission):
            binding = prior.attempt.binding
            if (binding.fence.key != self.source.fence.key or binding.authority != self.source.authority
                    or binding.fence.generation > self.source.fence.generation
                    or (binding.fence.generation == self.source.fence.generation
                        and binding.episode_id != self.source.episode_id)):
                raise ValueError("workspace predecessor is foreign or from a future generation")
        else:
            raise ValueError("workspace handoff requires verified admission or explicit absence")

    @property
    def revision(self) -> ContractRevision:
        return ContractRevision(self.branch, self.base.commit_sha)

    @property
    def handoff_id(self) -> str:
        prior = self.predecessor
        provenance = ("absent", _binding(prior.binding)) if isinstance(prior, NoPriorResolveAdmission) else (
            "admitted", _binding(prior.attempt.binding), str(prior.attempt.run_id),
            prior.attempt.leg_id, prior.admission_version,
        )
        return "episode-workspace:" + _digest((self.grant_request_id, _binding(self.source),
            _binding(self.destination), provenance, self.branch, self.base.base_ref, self.base.commit_sha))

    @property
    def archive_ref(self) -> str:
        return "refs/issue-agent/contract-reset/" + hashlib.sha256(self.handoff_id.encode()).hexdigest()


@dataclass(frozen=True)
class PrepareResolveWorkspace:
    handoff: EpisodeWorkspaceHandoff

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, EpisodeWorkspaceHandoff):
            raise ValueError("workspace preparation requires its exact handoff")


@dataclass(frozen=True)
class EpisodeWorkspacePrepared:
    handoff: EpisodeWorkspaceHandoff
    performed_under: WorkflowBinding
    result: ContractResetResult
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, EpisodeWorkspaceHandoff) or not isinstance(self.performed_under, WorkflowBinding):
            raise ValueError("workspace acknowledgement requires its handoff and execution authority")
        destination = self.handoff.destination
        binding = self.performed_under
        if (binding.fence.key != destination.fence.key or binding.authority != destination.authority
                or binding.episode_id != destination.episode_id or binding.fence.generation < destination.fence.generation):
            raise ValueError("workspace acknowledgement is outside the granted episode")
        if (not isinstance(self.result, ContractResetResult) or self.result.revision != self.handoff.revision
                or self.result.archive_ref != self.handoff.archive_ref):
            raise ValueError("workspace acknowledgement disagrees with its exact preparation obligation")
        _time(self.observed_at)
        if self.observed_at < self.handoff.base.observed_at:
            raise ValueError("workspace preparation cannot precede its upstream observation")
