"""Barrier-held legacy file and PR-link sources for review execution import."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
import sqlite3

from ..domain.review_execution_activation import (
    BlockedLegacyReviewGenerationReceipts, BlockedLegacyReviewRuns,
    BlockedLegacySubmissionSettlementStorage, CompleteLegacyReviewGenerationReceipts,
    CompleteLegacyReviewRuns,
)
from ..domain.review_execution_counter_mapping import (
    BlockedLegacyCounterMapping, BlockedLegacyOpenCounterLinks, CompleteLegacyCounterMapping,
    CompleteLegacyOpenCounterLinks, map_legacy_execution_counters,
)
from ..domain.review_execution_death_import import (
    BlockedLegacyReviewDeathAttribution, CompleteLegacyReviewDeathAttribution,
    attribute_complete_legacy_review_deaths,
)
from ..domain.workflow_identity import WorkflowWriter, canonical_workflow_repository, validate_workflow_text
from .control_store import SQLiteControlStore
from .deployment_authority import deployment_effect_barrier
from .resolve_activation import LegacyRoots
from .review_execution_activation import (
    BlockedLegacyExecutionFileValues, CompleteLegacyExecutionRootFiles,
    CompleteLegacySubmissionSettlementStorage, revalidate_complete_legacy_execution_file_values,
    revalidate_complete_legacy_review_generation_receipts, revalidate_complete_legacy_review_runs,
    verify_complete_legacy_submission_settlement_storage,
)
from .review_execution_counter_links import revalidate_complete_legacy_open_counter_links

_ROLES = ("fix", "review", "steward")
_CURRENT_SCOPE: ContextVar[object | None] = ContextVar("legacy_execution_import_scope", default=None)
_VERIFICATION_MINT = object()


@dataclass(frozen=True)
class HeldLegacyExecutionCounterSources:
    """Complete file and PR-link observations rechecked under held barriers."""

    roots: CompleteLegacyExecutionRootFiles
    mapping: CompleteLegacyCounterMapping
    _scope: object = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.roots, CompleteLegacyExecutionRootFiles)
                or not isinstance(self.mapping, CompleteLegacyCounterMapping)
                or self.roots.values != self.mapping.files):
            raise ValueError("held counter sources require the same complete file values")

    @property
    def source_digest(self) -> str:
        source = (self.roots.source_digest, self.mapping.source_digest)
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


_OPEN_SCOPES: dict[object, tuple[str, HeldLegacyExecutionCounterSources]] = {}


def _held_control_path(counters: HeldLegacyExecutionCounterSources) -> str | None:
    if _CURRENT_SCOPE.get() is not counters._scope:
        return None
    entry = _OPEN_SCOPES.get(counters._scope)
    return entry[0] if entry is not None and entry[1] is counters else None


@dataclass(frozen=True)
class BlockedLegacyExecutionCounterSources:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked execution counter source")


@contextmanager
def held_legacy_execution_counter_sources(
    control: SQLiteControlStore, gateway: object, repository: str, roots: LegacyRoots,
    observed_files: CompleteLegacyExecutionRootFiles,
    observed_links: CompleteLegacyOpenCounterLinks,
) -> Iterator[HeldLegacyExecutionCounterSources | BlockedLegacyExecutionCounterSources]:
    """Recheck files and PR linkage while excluding all three role effects.

    A future import writer must keep this context open through its SQL commit.
    This result proves source identity and attribution, not historical debit or
    reset authority.
    """
    if not isinstance(control, SQLiteControlStore):
        raise ValueError("execution counter sources require a control store")
    repo = canonical_workflow_repository(repository)
    control.require_initialized()
    with ExitStack() as barriers:
        for role in _ROLES:
            barriers.enter_context(deployment_effect_barrier(control.path, repo, role))
        files = revalidate_complete_legacy_execution_file_values(roots, observed_files)
        if isinstance(files, BlockedLegacyExecutionFileValues):
            yield BlockedLegacyExecutionCounterSources(files.reason)
            return
        links = revalidate_complete_legacy_open_counter_links(control, gateway, repo, observed_links)
        if isinstance(links, BlockedLegacyOpenCounterLinks):
            yield BlockedLegacyExecutionCounterSources(links.reason)
            return
        mapping = map_legacy_execution_counters(files.values, links)
        if isinstance(mapping, BlockedLegacyCounterMapping):
            yield BlockedLegacyExecutionCounterSources(mapping.reason)
            return
        scope = object()
        scope_reset = _CURRENT_SCOPE.set(scope)
        held = HeldLegacyExecutionCounterSources(files, mapping, scope)
        _OPEN_SCOPES[scope] = (control.path, held)
        try:
            yield held
        finally:
            _OPEN_SCOPES.pop(scope, None)
            _CURRENT_SCOPE.reset(scope_reset)


@dataclass(frozen=True)
class ObservedLegacyExecutionSqlImportSources:
    """Caller-held complete SQL inventories; no activation authority yet."""

    runs: CompleteLegacyReviewRuns
    settlement: CompleteLegacySubmissionSettlementStorage
    generation: CompleteLegacyReviewGenerationReceipts

    def __post_init__(self) -> None:
        if (not isinstance(self.runs, CompleteLegacyReviewRuns)
                or not isinstance(self.settlement, CompleteLegacySubmissionSettlementStorage)
                or not isinstance(self.generation, CompleteLegacyReviewGenerationReceipts)
                or len({self.runs.lineage.repository, self.settlement.repository,
                         self.generation.repository}) != 1):
            raise ValueError("SQL import sources require complete same-repository inventories")

    @property
    def repository(self) -> str:
        return self.runs.lineage.repository

    @property
    def source_digest(self) -> str:
        source = (self.runs.source_digest, self.settlement.source_digest,
                  self.generation.source_digest)
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RevalidatedLegacyExecutionImportSources:
    """Selected inventories match; caller ownership still needs deployment provenance."""

    counters: HeldLegacyExecutionCounterSources
    sql: ObservedLegacyExecutionSqlImportSources
    deaths: CompleteLegacyReviewDeathAttribution
    _mint: object = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.counters, HeldLegacyExecutionCounterSources)
                or not isinstance(self.sql, ObservedLegacyExecutionSqlImportSources)
                or not isinstance(self.deaths, CompleteLegacyReviewDeathAttribution)
                or self.counters.mapping.links.repository != self.sql.repository
                or self.deaths.runs != self.sql.runs
                or self.deaths.mapping != self.counters.mapping
                or _held_control_path(self.counters) is None or self._mint is not _VERIFICATION_MINT):
            raise ValueError("execution import sources require verified same-repository evidence")

    @property
    def source_digest(self) -> str:
        source = (self.counters.source_digest, self.sql.source_digest, self.deaths.source_digest)
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyExecutionSqlImportSources:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked execution SQL import source")


def verify_legacy_execution_import_sources(
    db: sqlite3.Connection, counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
    *, writer: WorkflowWriter, configured_prior_owners: tuple[str, ...],
) -> RevalidatedLegacyExecutionImportSources | BlockedLegacyExecutionSqlImportSources:
    """Recheck all retained SQL sets inside the caller's acceptance transaction.

    The caller must keep `held_legacy_execution_counter_sources` open through
    commit. Owner inputs are caller supplied and still need deployment-config
    provenance at the production writer. This verifies matching source sets,
    not inherited debit/reset or imported session authority.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("execution import source verification requires an explicit SQL transaction")
    if not isinstance(counters, HeldLegacyExecutionCounterSources) or not isinstance(
        observed, ObservedLegacyExecutionSqlImportSources,
    ):
        raise ValueError("execution import source verification requires complete typed observations")
    if (not isinstance(writer, WorkflowWriter) or writer.role != "review"
            or not isinstance(configured_prior_owners, tuple)
            or any(not isinstance(owner, str) for owner in configured_prior_owners)):
        raise ValueError("execution import source verification requires configured review ownership")
    control_path = _held_control_path(counters)
    if control_path is None:
        return BlockedLegacyExecutionSqlImportSources("execution import source barriers are no longer held")
    try:
        main = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
        same_store = bool(main) and os.path.samefile(main, control_path)
    except OSError:
        same_store = False
    if not same_store:
        return BlockedLegacyExecutionSqlImportSources("execution import SQL store differs from barrier-held control store")
    if counters.mapping.links.repository != observed.repository:
        return BlockedLegacyExecutionSqlImportSources("execution import source repositories disagree")
    lineage = observed.runs.lineage
    if (writer.repository != observed.repository or writer.owner_id != lineage.current_owner
            or configured_prior_owners != lineage.configured_prior_owners):
        return BlockedLegacyExecutionSqlImportSources("execution import owner lineage differs from configuration")
    runs = revalidate_complete_legacy_review_runs(db, observed.runs)
    if isinstance(runs, BlockedLegacyReviewRuns):
        return BlockedLegacyExecutionSqlImportSources(runs.reason)
    settlement = verify_complete_legacy_submission_settlement_storage(
        db, observed.settlement.commands, observed.settlement.receipts,
        observed.settlement.round_storage,
    )
    if isinstance(settlement, BlockedLegacySubmissionSettlementStorage):
        return BlockedLegacyExecutionSqlImportSources(settlement.reason)
    generation = revalidate_complete_legacy_review_generation_receipts(db, observed.generation)
    if isinstance(generation, BlockedLegacyReviewGenerationReceipts):
        return BlockedLegacyExecutionSqlImportSources(generation.reason)
    current = ObservedLegacyExecutionSqlImportSources(runs, settlement, generation)
    if current != observed or current.source_digest != observed.source_digest:
        return BlockedLegacyExecutionSqlImportSources("execution SQL sources changed after inventory")
    deaths = attribute_complete_legacy_review_deaths(runs, counters.mapping)
    if isinstance(deaths, BlockedLegacyReviewDeathAttribution):
        return BlockedLegacyExecutionSqlImportSources(deaths.reason)
    if _held_control_path(counters) is None:
        return BlockedLegacyExecutionSqlImportSources("execution import source barriers are no longer held")
    return RevalidatedLegacyExecutionImportSources(counters, current, deaths, _VERIFICATION_MINT)
