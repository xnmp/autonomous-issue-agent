"""Typed maintenance results; ordinary admission keeps its own authority checks."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .workflow_identity import PullRequestRef, WorkflowKey, WorkflowRevision, validate_workflow_text


class ReviewExecutionMaintenanceStage(str, Enum):
    INITIAL_ARCHIVE = "initial_archive"
    RETIRED_ARCHIVE = "retired_archive"


def _request(key: WorkflowKey, checked_at: datetime) -> None:
    if (not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef)
            or not isinstance(checked_at, datetime) or checked_at.tzinfo is None
            or checked_at.utcoffset() is None):
        raise ValueError("execution maintenance requires a PR key and aware time")


@dataclass(frozen=True)
class ReadyReviewExecutionMaintenance:
    key: WorkflowKey
    revision: WorkflowRevision
    checked_at: datetime

    def __post_init__(self) -> None:
        _request(self.key, self.checked_at)
        if (not isinstance(self.revision, WorkflowRevision)
                or self.revision.fence.key != self.key):
            raise ValueError("execution maintenance result requires its current revision")


@dataclass(frozen=True)
class PendingReviewExecutionMaintenance:
    key: WorkflowKey
    stage: ReviewExecutionMaintenanceStage
    checked_at: datetime

    def __post_init__(self) -> None:
        _request(self.key, self.checked_at)
        if not isinstance(self.stage, ReviewExecutionMaintenanceStage):
            raise ValueError("execution maintenance requires its typed pending stage")


@dataclass(frozen=True)
class BlockedReviewExecutionMaintenance:
    key: WorkflowKey
    reason: str
    checked_at: datetime

    def __post_init__(self) -> None:
        _request(self.key, self.checked_at)
        validate_workflow_text(self.reason, "execution maintenance blocker", limit=512)


ReviewExecutionMaintenanceResult = (
    ReadyReviewExecutionMaintenance | PendingReviewExecutionMaintenance
    | BlockedReviewExecutionMaintenance
)
