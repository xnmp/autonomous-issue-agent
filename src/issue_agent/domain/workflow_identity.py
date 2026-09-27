"""Stable workflow identities, independent of command and episode payloads."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .repository import canonical_repository, repository_validation_error

# Shared workflow bounds live with the identities so family modules below
# `workflow.py` can derive their own bounds without an import cycle.
MAX_REARM_COMMANDS = 64   # durable obligations of one transition or re-arm
MAX_WORKFLOW_GATES = 64
MAX_DETAIL_CHARS = 16384


def validate_workflow_integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be an integer in [{minimum}, 2**63 - 1]")


def validate_workflow_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be a nonempty bounded identifier")


def validate_workflow_text(value: str, name: str, *, limit: int = 512, empty: bool = False) -> None:
    if limit == 512 and not empty:
        validate_workflow_identifier(value, name)
    elif not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"{name} must be a bounded string")


def canonical_workflow_repository(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("workflow repository must be a string")
    repository = canonical_repository(value)
    if repository.count("/") != 1 or repository_validation_error(repository):
        raise ValueError("workflow repository must be a canonical owner/name slug")
    return repository


@dataclass(frozen=True)
class IssueRef:
    number: int

    def __post_init__(self) -> None:
        validate_workflow_integer(self.number, "issue number", minimum=1)


@dataclass(frozen=True)
class PullRequestRef:
    number: int

    def __post_init__(self) -> None:
        validate_workflow_integer(self.number, "pull request number", minimum=1)


WorkflowTarget = IssueRef | PullRequestRef


@dataclass(frozen=True)
class WorkflowKey:
    repository: str
    target: WorkflowTarget

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", canonical_workflow_repository(self.repository))
        if not isinstance(self.target, (IssueRef, PullRequestRef)):
            raise ValueError("workflow target must be an issue or pull request identity")


class WorkflowFamily(str, Enum):
    RESOLVE = "resolve"
    REVIEW_REJECTIONS = "review_rejections"
    REVIEW_EXECUTION = "review_execution"


def target_family(target: WorkflowTarget) -> WorkflowFamily:
    """Phase 3 owns one family per target: resolve is issue-owned and review
    rejection is PR-owned regardless of changing issue linkage (ADR 0015)."""
    if isinstance(target, IssueRef):
        return WorkflowFamily.RESOLVE
    if isinstance(target, PullRequestRef):
        return WorkflowFamily.REVIEW_REJECTIONS
    raise ValueError("workflow target must be an issue or pull request identity")


@dataclass(frozen=True)
class WorkflowFence:
    key: WorkflowKey
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("workflow fence requires a validated workflow key")
        validate_workflow_integer(self.generation, "workflow generation")


@dataclass(frozen=True)
class FamilyAuthority:
    family: WorkflowFamily
    epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.family, WorkflowFamily):
            raise ValueError("workflow family must be a recognized variant")
        validate_workflow_integer(self.epoch, "family authority epoch", minimum=1)


@dataclass(frozen=True)
class WorkflowRevision:
    fence: WorkflowFence
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.fence, WorkflowFence):
            raise ValueError("workflow revision requires a validated fence")
        validate_workflow_integer(self.version, "workflow version")


@dataclass(frozen=True)
class WorkflowBinding:
    fence: WorkflowFence
    authority: FamilyAuthority
    episode_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.fence, WorkflowFence) or not isinstance(self.authority, FamilyAuthority):
            raise ValueError("workflow binding requires a validated fence and authority")
        if self.authority.family is WorkflowFamily.RESOLVE and not isinstance(self.fence.key.target, IssueRef):
            raise ValueError("resolve episodes belong to issues, not pull requests")
        if self.authority.family in (WorkflowFamily.REVIEW_REJECTIONS, WorkflowFamily.REVIEW_EXECUTION) and not isinstance(self.fence.key.target, PullRequestRef):
            raise ValueError("review episodes belong to pull requests, not issues")
        validate_workflow_identifier(self.episode_id, "workflow episode")


@dataclass(frozen=True)
class WorkflowWriter:
    repository: str
    role: Literal["fix", "review", "steward"]
    owner_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", canonical_workflow_repository(self.repository))
        if self.role not in ("fix", "review", "steward"):
            raise ValueError("workflow writer requires a deployment role")
        validate_workflow_identifier(self.owner_id, "workflow writer owner")
