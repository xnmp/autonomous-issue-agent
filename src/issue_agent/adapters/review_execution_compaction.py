"""Transaction-local observations for a future review execution checkpoint writer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.review_checkpoint_application import (
    ReviewCheckpointApplicationProposal, build_review_checkpoint_application_proposal,
)
from ..domain.review_execution import (
    ActivatedReviewExecution, MAX_EXECUTION_SUFFIX, ReviewExecutionBoundaryCursor,
    ReviewExecutionCheckpointReceipt, ReviewExecutionCompactionPlan, ReviewRoundOutcome,
    ReviewGenerationArchiveFingerprint, build_review_execution_checkpoint_receipt,
    plan_review_execution_compaction, same_execution_episode,
)
from ..domain.review_execution_activation import MAX_LEGACY_ROUND_JSON_BYTES
from ..domain.workflow_identity import WorkflowKey, WorkflowRevision
from .review_execution_activation import (
    BlockedReviewCheckpointRoundPage, BlockedReviewGenerationArchivePage,
    ObservedReviewCheckpointRoundPage, ObservedReviewGenerationArchivePage,
    read_checkpoint_round_archive_page, read_review_generation_archive_page,
)
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, _read_execution_origin, load_review_workflow_row,
    workflow_scope,
)


MAX_CHECKPOINT_MANIFEST_SOURCE_BYTES = 2**27


@dataclass(frozen=True)
class ObservedReviewCheckpointManifest:
    """A source-backed proposal; neither a committed receipt nor audit authority."""

    revision: WorkflowRevision
    plan: ReviewExecutionCompactionPlan
    receipt: ReviewExecutionCheckpointReceipt
    application: ReviewCheckpointApplicationProposal
    source_bytes: int

    def __post_init__(self) -> None:
        if (not isinstance(self.revision, WorkflowRevision)
                or not isinstance(self.plan, ReviewExecutionCompactionPlan)
                or not isinstance(self.receipt, ReviewExecutionCheckpointReceipt)
                or self.receipt.checkpoint != self.plan.checkpoint
                or not isinstance(self.application, ReviewCheckpointApplicationProposal)
                or self.application.checkpoint != self.plan.checkpoint
                or self.application.previous_checkpoint != self.receipt.previous_checkpoint
                or self.application.captured_revision != self.revision
                or type(self.source_bytes) is not int
                or not 0 <= self.source_bytes <= MAX_CHECKPOINT_MANIFEST_SOURCE_BYTES):
            raise ValueError("checkpoint manifest requires a bounded typed source proposal")


def _read_suffix_outcomes(
    db: sqlite3.Connection, execution: ActivatedReviewExecution,
    budget: int,
) -> tuple[tuple[ReviewRoundOutcome, ...], int]:
    binding = execution.binding
    scope = binding.rejection.fence.key
    expected = execution.last_sequence - execution.checkpoint.through_sequence
    if expected > MAX_EXECUTION_SUFFIX:
        raise WorkflowAuthorityError("checkpoint manifest round suffix exceeds its bound")
    metadata = db.execute(
        "SELECT rowid AS source_rowid,sequence,typeof(sequence) AS sequence_type,"
        "typeof(outcome_json) AS outcome_type,"
        "length(CAST(outcome_json AS BLOB)) AS outcome_bytes "
        "FROM review_execution_rounds WHERE repository=? AND target_number=? "
        "AND execution_epoch=? AND execution_episode=? AND sequence>? "
        "ORDER BY sequence LIMIT ?",
        (scope.repository, scope.target.number, binding.execution_epoch,
         binding.execution_episode, execution.checkpoint.through_sequence, MAX_EXECUTION_SUFFIX + 1),
    ).fetchall()
    if len(metadata) != expected:
        raise WorkflowAuthorityError("checkpoint manifest allocated round suffix has a gap")
    outcomes: list[ReviewRoundOutcome] = []
    source_bytes = 0
    for sequence, row in enumerate(metadata, execution.checkpoint.through_sequence + 1):
        if row["sequence_type"] != "integer" or row["sequence"] != sequence:
            raise WorkflowAuthorityError("checkpoint manifest round suffix has a changed sequence")
        if row["outcome_type"] == "null":
            continue
        size = row["outcome_bytes"]
        if (row["outcome_type"] != "text" or type(size) is not int
                or size > MAX_LEGACY_ROUND_JSON_BYTES or source_bytes + size > budget):
            raise WorkflowAuthorityError("checkpoint manifest outcome exceeds its byte bound")
        captured = db.execute(
            "SELECT outcome_json FROM review_execution_rounds WHERE rowid=? "
            "AND repository=? AND target_number=? AND execution_epoch=? "
            "AND execution_episode=? AND sequence=?",
            (row["source_rowid"], scope.repository, scope.target.number,
             binding.execution_epoch, binding.execution_episode, sequence),
        ).fetchone()
        if captured is None:
            raise WorkflowAuthorityError("checkpoint manifest round changed during its bounded read")
        try:
            outcome = decode_workflow(captured["outcome_json"], ReviewRoundOutcome)
            if (encode_workflow(outcome) != captured["outcome_json"]
                    or outcome.round.sequence != sequence
                    or not same_execution_episode(binding, outcome.round.binding)):
                raise ValueError("foreign or noncanonical outcome")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise WorkflowAuthorityError("checkpoint manifest has an unreadable outcome") from exc
        outcomes.append(outcome)
        source_bytes += size
    return tuple(outcomes), source_bytes


def observe_review_checkpoint_manifest(
    db: sqlite3.Connection, key: WorkflowKey, through_sequence: int,
    *, max_source_bytes: int = MAX_CHECKPOINT_MANIFEST_SOURCE_BYTES,
) -> ObservedReviewCheckpointManifest:
    """Rebuild a retired source proposal from canonical SQL in one snapshot.

    A writer must still census unclaimed rows and receipts, verify every
    retired receipt's semantic payload, and publish an audit gate atomically.
    The source-byte ceiling accounts for retained fields after bounded readers
    return; it does not bound the canonical loader's reads or peak memory.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or type(through_sequence) is not int
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_CHECKPOINT_MANIFEST_SOURCE_BYTES):
        raise ValueError("checkpoint manifest requires a transaction and bounded inputs")
    snapshot = load_review_workflow_row(db, key)
    if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
        raise WorkflowAuthorityError("checkpoint manifest requires active execution authority")
    execution = snapshot.execution
    try:
        outcomes, source_bytes = _read_suffix_outcomes(db, execution, max_source_bytes)
        plan = plan_review_execution_compaction(execution, outcomes, through_sequence)
        origin, _ = _read_execution_origin(db, workflow_scope(key), execution)
        origin_binding = origin.checkpoint.state.binding
        round_sources = []
        next_sequence = execution.checkpoint.through_sequence + 1
        while next_sequence <= through_sequence:
            page = read_checkpoint_round_archive_page(
                db, origin_binding, next_sequence, through_sequence,
            )
            if isinstance(page, BlockedReviewCheckpointRoundPage):
                raise WorkflowAuthorityError(
                    f"checkpoint manifest retired round {page.sequence} is unreadable: {page.reason}"
                )
            if not isinstance(page, ObservedReviewCheckpointRoundPage):
                raise WorkflowAuthorityError("checkpoint manifest has an invalid round page")
            for observed in page.rounds:
                if (len(round_sources) >= len(plan.retired_outcomes)
                        or observed.outcome != plan.retired_outcomes[len(round_sources)]):
                    raise WorkflowAuthorityError("checkpoint manifest retired outcome changed its replay")
                source_bytes += observed.source_bytes
                if source_bytes > max_source_bytes:
                    raise WorkflowAuthorityError("checkpoint manifest source stream exceeds its byte bound")
                round_sources.append(observed.source)
            next_sequence = page.next_sequence
        if tuple(source.round for source in round_sources) != tuple(
            outcome.round for outcome in plan.retired_outcomes
        ):
            raise WorkflowAuthorityError("checkpoint manifest retired round sources changed its replay")
        generation_sources = []
        next_generation = execution.checkpoint.state.binding.rejection.fence.generation + 1
        through_generation = plan.checkpoint.state.binding.rejection.fence.generation
        preceding = execution.checkpoint.boundary_cursor
        while next_generation <= through_generation:
            page = read_review_generation_archive_page(
                db, origin_binding, next_generation, through_generation, preceding=preceding,
            )
            if isinstance(page, BlockedReviewGenerationArchivePage):
                raise WorkflowAuthorityError(
                    f"checkpoint manifest retired generation {page.generation} is unreadable: {page.reason}"
                )
            if not isinstance(page, ObservedReviewGenerationArchivePage):
                raise WorkflowAuthorityError("checkpoint manifest has an invalid generation page")
            if not page.sources or page.next_generation <= next_generation:
                raise WorkflowAuthorityError("checkpoint manifest generation page made no progress")
            for observed in page.sources:
                if (len(generation_sources) >= len(plan.retired_boundaries)
                        or observed.boundary != plan.retired_boundaries[len(generation_sources)]):
                    raise WorkflowAuthorityError("checkpoint manifest retired boundary changed its replay")
                source_bytes += observed.source_bytes
                if source_bytes > max_source_bytes:
                    raise WorkflowAuthorityError("checkpoint manifest source stream exceeds its byte bound")
                generation_sources.append(ReviewGenerationArchiveFingerprint(
                    ReviewExecutionBoundaryCursor(
                        observed.boundary.receipt_id, observed.boundary.to_revision,
                    ),
                    observed.receipt_digest, observed.accepted_namespace,
                    observed.accepted_key, observed.accepted_digest,
                ))
            preceding = page.sources[-1].boundary
            next_generation = page.next_generation
        if tuple(source.cursor.receipt_id for source in generation_sources) != tuple(
            boundary.receipt_id for boundary in plan.retired_boundaries
        ):
            raise WorkflowAuthorityError("checkpoint manifest retired generations changed their replay")
        following_source = None
        if plan.remaining_boundaries:
            next_generation = plan.checkpoint.state.binding.rejection.fence.generation + 1
            following_page = read_review_generation_archive_page(
                db, origin_binding, next_generation, next_generation,
                preceding=plan.checkpoint.boundary_cursor, limit=1,
            )
            if (isinstance(following_page, BlockedReviewGenerationArchivePage)
                    or not isinstance(following_page, ObservedReviewGenerationArchivePage)
                    or len(following_page.sources) != 1
                    or following_page.sources[0].boundary != plan.remaining_boundaries[0]):
                raise WorkflowAuthorityError("checkpoint manifest following boundary is unreadable")
            observed_following = following_page.sources[0]
            source_bytes += observed_following.source_bytes
            if source_bytes > max_source_bytes:
                raise WorkflowAuthorityError("checkpoint manifest source stream exceeds its byte bound")
            following_source = ReviewGenerationArchiveFingerprint(
                ReviewExecutionBoundaryCursor(
                    observed_following.boundary.receipt_id,
                    observed_following.boundary.to_revision,
                ),
                observed_following.receipt_digest,
                observed_following.accepted_namespace,
                observed_following.accepted_key,
                observed_following.accepted_digest,
            )
        receipt = build_review_execution_checkpoint_receipt(
            execution, plan, tuple(round_sources), tuple(generation_sources),
        )
        application = build_review_checkpoint_application_proposal(
            execution, plan, snapshot.revision, following_source,
        )
        return ObservedReviewCheckpointManifest(
            snapshot.revision, plan, receipt, application, source_bytes,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        if isinstance(exc, WorkflowAuthorityError):
            raise
        raise WorkflowAuthorityError("checkpoint manifest source stream is unreadable") from exc
