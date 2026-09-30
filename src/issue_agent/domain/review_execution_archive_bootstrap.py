"""Observational progress towards the first review execution checkpoint."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .review_execution_archive_evidence import ObservedInitialReviewExecutionArchiveEvidence
from .workflow_identity import PullRequestRef, WorkflowKey, validate_workflow_text


class ReviewExecutionArchiveBootstrapStage(str, Enum):
    IDENTITIES = "identities"
    EPISODE_SOURCES = "episode_sources"


def _request(key: WorkflowKey, observed_at: datetime) -> None:
    if (not isinstance(key, WorkflowKey) or not isinstance(key.target, PullRequestRef)
            or not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or observed_at.utcoffset() is None):
        raise ValueError("archive bootstrap progress requires a PR key and aware time")


@dataclass(frozen=True)
class PendingReviewExecutionArchiveBootstrap:
    key: WorkflowKey
    stage: ReviewExecutionArchiveBootstrapStage
    observed_at: datetime

    def __post_init__(self) -> None:
        _request(self.key, self.observed_at)
        if not isinstance(self.stage, ReviewExecutionArchiveBootstrapStage):
            raise ValueError("archive bootstrap progress requires a typed stage")


@dataclass(frozen=True)
class BlockedReviewExecutionArchiveBootstrap:
    key: WorkflowKey
    reason: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _request(self.key, self.observed_at)
        validate_workflow_text(self.reason, "archive bootstrap blocker", limit=512)


ReviewExecutionArchiveBootstrapResult = (
    PendingReviewExecutionArchiveBootstrap | BlockedReviewExecutionArchiveBootstrap
    | ObservedInitialReviewExecutionArchiveEvidence
)
