"""Fail-closed offline classification of legacy review session files."""

from __future__ import annotations

from dataclasses import dataclass

from .review_execution import NoReviewSession, ReviewExecutionBinding
from .review_execution_activation import (
    CompleteLegacyExecutionFileValues, LegacyReviewSessionCandidate,
    LegacyReviewSessionModeMarker,
)
from .workflow_identity import (
    PullRequestRef, canonical_workflow_repository, validate_workflow_integer, validate_workflow_text,
)


def _digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


@dataclass(frozen=True)
class UnprovenLegacyReviewSession:
    """A real file handle whose producing review cannot be identified."""

    repository: str
    pr: int
    root: str
    roots_digest: str
    candidate: LegacyReviewSessionCandidate
    reason: str

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("legacy session repository must be canonical")
        validate_workflow_integer(self.pr, "unproven session PR", minimum=1)
        validate_workflow_text(self.root, "unproven session root", limit=4096)
        validate_workflow_text(self.reason, "unproven session reason")
        if not _digest(self.roots_digest) or not isinstance(self.candidate, LegacyReviewSessionCandidate):
            raise ValueError("unproven session requires its complete file source")


@dataclass(frozen=True)
class BlockedLegacyReviewSessionImport:
    pr: int
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "blocked session PR", minimum=1)
        validate_workflow_text(self.reason, "blocked session reason")


def plan_legacy_review_session_import(
    files: CompleteLegacyExecutionFileValues,
    roots_digest: str,
    binding: ReviewExecutionBinding,
) -> NoReviewSession | UnprovenLegacyReviewSession:
    """Retain real handles as blockers until their source reviewer is proved.

    The old writer could retain a handle after a newer successful review that
    returned no session, so the file plus a current baseline cannot authorize
    reuse. Absence and known mode markers carry no reusable handle.
    """
    if not isinstance(files, CompleteLegacyExecutionFileValues) or not isinstance(binding, ReviewExecutionBinding):
        raise ValueError("session import requires typed complete files and execution binding")
    if not _digest(roots_digest):
        raise ValueError("session import requires a complete root source digest")
    pr = binding.rejection.fence.key.target
    assert isinstance(pr, PullRequestRef)
    source = next((item for item in files.prs if item.source.pr == pr.number), None)
    if source is None or isinstance(source.session, LegacyReviewSessionModeMarker):
        return NoReviewSession()
    candidate = source.session
    if not isinstance(candidate, LegacyReviewSessionCandidate):
        return NoReviewSession()
    return UnprovenLegacyReviewSession(
        binding.rejection.fence.key.repository, pr.number, source.source.root, roots_digest,
        candidate, "legacy session file lacks proof of its latest producing review",
    )
