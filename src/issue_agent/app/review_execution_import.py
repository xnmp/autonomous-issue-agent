"""Configuration-bound composition for offline review execution import sources."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from ..adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, HeldLegacyExecutionCounterSources,
    ObservedLegacyExecutionSqlImportSources, RevalidatedLegacyExecutionImportSources,
    verify_legacy_execution_import_sources,
)
from ..adapters.resolve_activation import distinct_roots
from ..domain.workflow_identity import WorkflowWriter, canonical_workflow_repository
from .config import HarnessConfig, configured_prior_owner_ids, owner_identity_for_target


def verify_configured_legacy_execution_import_sources(
    cfg: HarnessConfig, db: sqlite3.Connection,
    counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
    *, now: datetime | None = None, extra_roots: tuple[str, ...] = (),
) -> RevalidatedLegacyExecutionImportSources | BlockedLegacyExecutionSqlImportSources:
    """Bind SQL source revalidation to the same owner lineage daemon boot adopts.

    The result remains observational. The import writer must bind declared
    historical roots to its reviewed plan, hold the source context through
    commit, and separately prove inherited debit, reset and session authority.
    """
    if not isinstance(cfg, HarnessConfig):
        raise ValueError("execution import requires deployment configuration")
    if not isinstance(counters, HeldLegacyExecutionCounterSources):
        raise ValueError("execution import requires held typed file sources")
    cfg.validate()
    repository = canonical_workflow_repository(cfg.repo)
    if cfg.repo != repository or not os.path.isabs(cfg.target_dir):
        raise ValueError("execution import requires a canonical repository and absolute checkout")
    configured_roots = tuple((role, path) for role, path in (
        ("fix", cfg.state_dir), ("review", cfg.review_state_dir), ("steward", cfg.steward_state_dir),
    ) if path)
    if not cfg.review_state_dir or any(not os.path.isabs(path) for _, path in configured_roots):
        raise ValueError("execution import requires absolute configured state directories")
    if (not isinstance(extra_roots, tuple)
            or any(not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path)
                   for path in extra_roots)):
        raise ValueError("execution import historical roots require existing absolute directories")
    configured_roots += tuple((f"extra:{os.path.realpath(path)}", path) for path in extra_roots)
    expected_roots = tuple((label, os.path.abspath(path), os.path.realpath(path))
                           for label, path in configured_roots)
    distinct_expected_roots = tuple((label, os.path.abspath(path), os.path.realpath(path))
                                    for label, path in distinct_roots(configured_roots))
    observed_roots = tuple((root.label, root.configured_path, root.resolved_path)
                           for root in counters.roots.roots)
    expected_other_role_dirs = tuple(os.path.abspath(path) for path in
                                     (cfg.state_dir, cfg.steward_state_dir) if path)
    if (counters.review_state_dir != os.path.abspath(cfg.review_state_dir)
            or counters.other_role_state_dirs != expected_other_role_dirs
            or observed_roots not in (expected_roots, distinct_expected_roots)):
        return BlockedLegacyExecutionSqlImportSources("execution import roots differ from deployment configuration")
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
        now=now,
    )
