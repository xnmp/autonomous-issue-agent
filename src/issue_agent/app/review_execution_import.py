"""Configuration-bound composition for offline review execution import sources."""

from __future__ import annotations

import os
import sqlite3

from ..adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, HeldLegacyExecutionCounterSources,
    ObservedLegacyExecutionSqlImportSources, RevalidatedLegacyExecutionImportSources,
    verify_legacy_execution_import_sources,
)
from ..domain.workflow_identity import WorkflowWriter, canonical_workflow_repository
from .config import HarnessConfig, configured_prior_owner_ids, owner_identity_for_target


def verify_configured_legacy_execution_import_sources(
    cfg: HarnessConfig, db: sqlite3.Connection,
    counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
) -> RevalidatedLegacyExecutionImportSources | BlockedLegacyExecutionSqlImportSources:
    """Bind SQL source revalidation to the same owner lineage daemon boot adopts.

    The result remains observational. The import writer must hold the source
    context through commit and separately prove inherited debit, reset and
    session authority.
    """
    if not isinstance(cfg, HarnessConfig):
        raise ValueError("execution import requires deployment configuration")
    cfg.validate()
    repository = canonical_workflow_repository(cfg.repo)
    if cfg.repo != repository or not os.path.isabs(cfg.target_dir):
        raise ValueError("execution import requires a canonical repository and absolute checkout")
    writer = WorkflowWriter(
        repository, "review", owner_identity_for_target(repository, cfg.target_dir, "review"),
    )
    implicit_owner = f"{repository}:review"
    predecessors = tuple(dict.fromkeys(
        owner for owner in configured_prior_owner_ids(cfg, "review")
        if owner not in {writer.owner_id, implicit_owner}
    ))
    return verify_legacy_execution_import_sources(
        db, counters, observed, writer=writer, configured_prior_owners=predecessors,
    )
