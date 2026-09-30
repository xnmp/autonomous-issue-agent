"""Sourced blocked migration decisions; these never grant execution authority."""

from dataclasses import dataclass
from enum import Enum

from .workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey,
    validate_workflow_integer, validate_workflow_text,
)


class ReviewImportBlockerKind(str, Enum):
    MALFORMED_COMMAND = "malformed_command"
    UNASSIGNED_COUNTER = "unassigned_counter"
    UNATTRIBUTED_RUN = "unattributed_run"
    UNATTRIBUTED_COMMAND = "unattributed_command"
    SESSION_PRODUCER = "session_producer"
    OPEN_ROUND = "open_round"
    PREDECESSOR_DEATH = "predecessor_death"
    NONZERO_DEBIT = "nonzero_debit"
    ORIGIN = "origin"
    UNATTRIBUTED_RECEIPT = "unattributed_receipt"
    CURRENT_OBSERVATION = "current_observation"
    CROSS_TARGET_SOURCE = "cross_target_source"


@dataclass(frozen=True)
class ReviewImportSource:
    kind: str
    identity: str
    digest: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.kind, "review import source kind", limit=64)
        validate_workflow_text(self.identity, "review import source identity", limit=4096)
        if not isinstance(self.digest, str) or len(self.digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.digest
        ):
            raise ValueError("review import source requires an exact identity and digest")


@dataclass(frozen=True)
class ReviewImportBlocker:
    kind: ReviewImportBlockerKind
    sources: tuple[ReviewImportSource, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.kind, ReviewImportBlockerKind)
                or not isinstance(self.sources, tuple) or not self.sources
                or any(not isinstance(item, ReviewImportSource) for item in self.sources)):
            raise ValueError("review import blocker requires a typed cause and source")


@dataclass(frozen=True)
class ReviewImportHistoryBlocked:
    pr: int
    blockers: tuple[ReviewImportBlocker, ...]

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "blocked review import PR", minimum=1)
        if (not isinstance(self.blockers, tuple) or not self.blockers
                or any(not isinstance(item, ReviewImportBlocker) for item in self.blockers)
                or len({item.kind for item in self.blockers}) != len(self.blockers)):
            raise ValueError("blocked review import requires distinct reasons")



@dataclass(frozen=True)
class BlockedReviewExecutionMigration:
    key: WorkflowKey
    authority: FamilyAuthority
    history: ReviewImportHistoryBlocked
    activation_plan_digest: str
    source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.key, WorkflowKey) or not isinstance(self.key.target, PullRequestRef)
                or not isinstance(self.authority, FamilyAuthority)
                or self.authority.family is not WorkflowFamily.REVIEW_EXECUTION
                or not isinstance(self.history, ReviewImportHistoryBlocked)
                or self.key.target.number != self.history.pr):
            raise ValueError("blocked migration requires its exact target and execution epoch")
        for digest in (self.activation_plan_digest, self.source_digest):
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("blocked migration requires exact plan and source digests")
