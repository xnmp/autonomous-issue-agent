"""Read one complete, bounded SQL source set for review execution import."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from ..domain.review_execution_activation import (
    CompleteLegacyReviewGenerationReceipts, CompleteLegacyReviewOutcomeReceipts,
    CompleteLegacyReviewRounds, CompleteLegacyReviewRuns,
    CompleteLegacyReviewSubmissionReceipts, CompleteLegacyReviewSubmissions,
    CompleteLegacyRoundOutcomeStorage, LegacyReviewOwnerLineage,
    MAX_LEGACY_REVIEW_OWNERS,
)
from ..domain.workflow_identity import canonical_workflow_repository, validate_workflow_identifier
from .review_execution_activation import (
    CompleteLegacySubmissionSettlementStorage, read_complete_legacy_review_generation_receipts,
    read_complete_legacy_review_outcome_receipts, read_complete_legacy_review_rounds,
    read_complete_legacy_review_runs, read_complete_legacy_review_submission_receipts,
    read_complete_legacy_review_submissions, read_legacy_review_owner_lineage,
    verify_complete_legacy_round_outcome_storage,
    verify_complete_legacy_submission_settlement_storage,
)
from .review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, ObservedLegacyExecutionSqlImportSources,
)
from .review_registration_membership_audit import observe_matched_review_registration_membership
from .sql_read_budget import MAX_SQL_READ_BYTES, SqlReadBudgetExceeded, bounded_sql_source_reads
from .workflow_rows import WorkflowAuthorityError


def observe_legacy_execution_sql_import_sources(
    db: sqlite3.Connection, repository: str, current_owner: str, *,
    configured_prior_owners: tuple[str, ...] = (), now: datetime,
    max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ObservedLegacyExecutionSqlImportSources | BlockedLegacyExecutionSqlImportSources:
    """Collect existing typed inventories without advancing their audit cursors.

    Callers retain the transaction and separately hold deployment barriers
    through activation. This observation alone grants no import authority.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("execution SQL observation requires an open transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("execution SQL observation requires a canonical repository")
    if (not isinstance(current_owner, str) or not current_owner
            or not isinstance(configured_prior_owners, tuple)
            or len(configured_prior_owners) >= MAX_LEGACY_REVIEW_OWNERS
            or any(not isinstance(owner, str) for owner in configured_prior_owners)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(max_source_bytes) is not int or not 1 <= max_source_bytes <= MAX_SQL_READ_BYTES):
        raise ValueError("execution SQL observation requires configured ownership, aware time and byte ceiling")
    owner_prefix = f"{repository}:review"
    validate_workflow_identifier(current_owner, "current review owner")
    if current_owner != owner_prefix and not current_owner.startswith(owner_prefix + ":"):
        raise ValueError("execution SQL observation owner differs from repository review role")
    for owner in configured_prior_owners:
        validate_workflow_identifier(owner, "configured prior review owner")

    try:
        with bounded_sql_source_reads(db, max_source_bytes):
            # The membership read rejects absent, incomplete, stale and blocked
            # registration or lifecycle censuses before the retained inventories.
            membership = observe_matched_review_registration_membership(db, repository, now=now)
            lineage = read_legacy_review_owner_lineage(
                db, repository, current_owner,
                configured_prior_owners=configured_prior_owners,
            )
            if not isinstance(lineage, LegacyReviewOwnerLineage):
                return BlockedLegacyExecutionSqlImportSources(lineage.reason)
            runs = read_complete_legacy_review_runs(db, lineage)
            if not isinstance(runs, CompleteLegacyReviewRuns):
                return BlockedLegacyExecutionSqlImportSources(runs.reason)
            commands = read_complete_legacy_review_submissions(db, repository)
            if not isinstance(commands, CompleteLegacyReviewSubmissions):
                return BlockedLegacyExecutionSqlImportSources(commands.reason)
            receipts = read_complete_legacy_review_submission_receipts(db, repository)
            if not isinstance(receipts, CompleteLegacyReviewSubmissionReceipts):
                return BlockedLegacyExecutionSqlImportSources(receipts.reason)
            rounds = read_complete_legacy_review_rounds(db, repository)
            if not isinstance(rounds, CompleteLegacyReviewRounds):
                return BlockedLegacyExecutionSqlImportSources(rounds.reason)
            outcomes = read_complete_legacy_review_outcome_receipts(db, repository)
            if not isinstance(outcomes, CompleteLegacyReviewOutcomeReceipts):
                return BlockedLegacyExecutionSqlImportSources(outcomes.reason)
            storage = verify_complete_legacy_round_outcome_storage(db, rounds, outcomes)
            if not isinstance(storage, CompleteLegacyRoundOutcomeStorage):
                return BlockedLegacyExecutionSqlImportSources(storage.reason)
            settlement = verify_complete_legacy_submission_settlement_storage(
                db, commands, receipts, storage,
            )
            if not isinstance(settlement, CompleteLegacySubmissionSettlementStorage):
                return BlockedLegacyExecutionSqlImportSources(settlement.reason)
            generation = read_complete_legacy_review_generation_receipts(db, repository)
            if not isinstance(generation, CompleteLegacyReviewGenerationReceipts):
                return BlockedLegacyExecutionSqlImportSources(generation.reason)
            return ObservedLegacyExecutionSqlImportSources(
                runs, settlement, generation, membership,
            )
    except (WorkflowAuthorityError, SqlReadBudgetExceeded) as exc:
        return BlockedLegacyExecutionSqlImportSources((str(exc) or "execution SQL source is unreadable")[:512])
