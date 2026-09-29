"""Read the first retained generation named by a checkpoint tail choice."""

from __future__ import annotations

import sqlite3

from ..domain.review_checkpoint_application import (
    ReviewCheckpointApplicationProposal, validate_checkpoint_following_boundary,
)
from ..domain.review_execution import (
    ReviewExecutionBinding, ReviewExecutionBoundaryCursor,
    ReviewGenerationArchiveFingerprint, same_execution_episode,
)
from .review_execution_activation import (
    BlockedReviewGenerationArchivePage, ObservedReviewGenerationArchivePage,
    ObservedReviewGenerationArchiveSource, read_review_generation_archive_page,
)
from .workflow_rows import WorkflowAuthorityError


def checkpoint_following_fingerprint(
    source: ObservedReviewGenerationArchiveSource,
) -> ReviewGenerationArchiveFingerprint:
    if not isinstance(source, ObservedReviewGenerationArchiveSource):
        raise ValueError("checkpoint following source requires a typed observation")
    return ReviewGenerationArchiveFingerprint(
        ReviewExecutionBoundaryCursor(
            source.boundary.receipt_id, source.boundary.to_revision,
        ),
        source.receipt_digest, source.accepted_namespace,
        source.accepted_key, source.accepted_digest,
    )


def read_checkpoint_following_application_source(
    db: sqlite3.Connection, origin_binding: ReviewExecutionBinding,
    current_generation: int, application: ReviewCheckpointApplicationProposal,
) -> ObservedReviewGenerationArchiveSource | None:
    """Check applicability in the caller's SQL snapshot; no checkpoint authority."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(origin_binding, ReviewExecutionBinding)
            or type(current_generation) is not int
            or not isinstance(application, ReviewCheckpointApplicationProposal)):
        raise ValueError("checkpoint following source requires a transaction and typed inputs")
    if not same_execution_episode(
        origin_binding, application.previous_checkpoint.state.binding,
    ) or (origin_binding.rejection.fence.generation
          > application.previous_checkpoint.state.binding.rejection.fence.generation):
        raise ValueError("checkpoint following source requires the same execution episode")
    checkpoint = application.checkpoint
    following_generation = checkpoint.state.binding.rejection.fence.generation + 1
    if current_generation < following_generation - 1:
        raise WorkflowAuthorityError("checkpoint is ahead of its execution generation")
    if current_generation < following_generation:
        validate_checkpoint_following_boundary(application, None, None)
        return None
    page = read_review_generation_archive_page(
        db, origin_binding, following_generation, following_generation,
        preceding=checkpoint.boundary_cursor, limit=1,
    )
    if isinstance(page, BlockedReviewGenerationArchivePage):
        raise WorkflowAuthorityError(
            f"checkpoint following generation {page.generation} is unreadable: {page.reason}"
        )
    if not isinstance(page, ObservedReviewGenerationArchivePage) or len(page.sources) != 1:
        raise WorkflowAuthorityError("checkpoint following generation source is absent")
    source = page.sources[0]
    validate_checkpoint_following_boundary(
        application, source.boundary, checkpoint_following_fingerprint(source),
    )
    return source
