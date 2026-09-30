"""The operator SQL observation is complete, bounded and read only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_import_observation import (
    observe_legacy_execution_sql_import_sources,
)
from issue_agent.adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, ObservedLegacyExecutionSqlImportSources,
)
from issue_agent.app.config import configured_prior_owner_ids, owner_identity_for_target
from issue_agent.domain.review_execution_activation import MAX_LEGACY_REVIEW_OWNERS
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.app.test_review_execution_activation import rig
from tests.adapters.test_review_execution_control import OWNER, rig as round_rig
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for
from tests.adapters.test_review_execution_import_sources import _insert_lifecycle_command


def _dump(store: SQLiteControlStore) -> tuple[str, ...]:
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def _collect(rig, *, now: datetime | None = None, max_source_bytes: int | None = None):
    cfg, control, _, _, _, _, _, _, _ = rig
    options = {} if max_source_bytes is None else {"max_source_bytes": max_source_bytes}
    with control.transaction() as db:
        return observe_legacy_execution_sql_import_sources(
            db, cfg.repo, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"),
            configured_prior_owners=configured_prior_owner_ids(cfg, "review"),
            now=now or datetime.now(timezone.utc), **options,
        )




def test_empty_observation_needs_no_retired_certificate(tmp_path: Path) -> None:
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    before = _dump(control)
    with control.transaction() as db:
        result = observe_legacy_execution_sql_import_sources(
            db, "o/r", "o/r:review:test", now=datetime.now(timezone.utc),
        )
    assert isinstance(result, ObservedLegacyExecutionSqlImportSources)
    assert _dump(control) == before




def test_complete_empty_sources_feed_configured_activation_without_writing(rig) -> None:
    cfg, control, _, _, files, links, _, _, activation = rig
    before = _dump(control)
    observed = _collect(rig)
    assert isinstance(observed, ObservedLegacyExecutionSqlImportSources)
    assert not observed.runs.runs
    assert not observed.settlement.commands.submissions
    plan = activation.plan(files, links, observed)
    assert tuple(item.pr for item in plan.candidates) == (9, 10)
    assert _dump(control) == before
    assert not activation.activate(plan, files, links, observed).idempotent
    with control.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM workflow_authority_epochs WHERE repository=? AND family='review_execution'",
            (cfg.repo,),
        ).fetchone()[0] == 1


def test_recorded_history_is_held_for_target_scoped_r1_assessment(round_rig) -> None:
    control, gateway, selection, _, plan, command = confirmed_copy(round_rig)
    workflow_for(control, gateway).record_review(command.command_id)
    key = selection.expected.fence.key
    certified_at = control.now()
    before = _dump(control)
    with control.transaction() as db:
        result = observe_legacy_execution_sql_import_sources(
            db, key.repository, OWNER, now=certified_at,
        )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "target-scoped R1" in result.reason
    assert _dump(control) == before


def test_configured_repoint_predecessors_keep_their_prior_repository(rig) -> None:
    cfg, control, *_ = rig
    repointed = replace(cfg, repointed_from_deployments=(("old/r", cfg.target_dir),))
    priors = configured_prior_owner_ids(repointed, "review")
    assert len(priors) == 2 and all(owner.startswith("old/r:review") for owner in priors)
    before = _dump(control)
    with control.transaction() as db:
        result = observe_legacy_execution_sql_import_sources(
            db, cfg.repo, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"),
            configured_prior_owners=priors, now=datetime.now(timezone.utc),
        )
    assert isinstance(result, ObservedLegacyExecutionSqlImportSources)
    assert result.runs.lineage.configured_prior_owners == priors
    assert set(priors) <= set(result.runs.lineage.owners)
    assert _dump(control) == before


def test_orphan_outcome_receipt_is_held_without_writing(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-outcome',?,?,?,?)",
            (cfg.repo, "orphan-round", datetime.now(timezone.utc).isoformat(), "{}"),
        )
    stale = _collect(rig)
    assert isinstance(stale, BlockedLegacyExecutionSqlImportSources)
    certified_at = datetime.now(timezone.utc)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "target-scoped R1" in result.reason or "outcome" in result.reason
    assert _dump(control) == before


def test_unregistered_submission_command_blocks_collection(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        _insert_lifecycle_command(db, "unregistered-import-command", state="pending")
        db.execute(
            "UPDATE lifecycle_commands SET repository=? WHERE command_id=?",
            (cfg.repo, "unregistered-import-command"),
        )
    certified_at = datetime.now(timezone.utc)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "target-scoped R1" in result.reason or "command" in result.reason
    assert _dump(control) == before


def test_oversized_retained_source_and_cumulative_ceiling_refuse(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-generation',?,?,?,?)",
            (cfg.repo, "oversized-generation", datetime.now(timezone.utc).isoformat(), "x" * (2**21 + 1)),
        )
    certified_at = datetime.now(timezone.utc)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "preflight" in result.reason or "bound" in result.reason or "oversized" in result.reason
    assert _dump(control) == before


def test_empty_sources_fit_small_budget_after_retired_census_removal(rig) -> None:
    # Empty sources no longer read retained membership certificates.
    limited = _collect(rig, max_source_bytes=64)
    assert isinstance(limited, ObservedLegacyExecutionSqlImportSources)
    assert not limited.runs.runs


@pytest.mark.parametrize(
    "repository,owner,prior,now,ceiling",
    [
        ("O/R", "o/r:review:test", (), datetime.now(timezone.utc), 1000),
        ("o/r", "other:review:test", (), datetime.now(timezone.utc), 1000),
        ("o/r", "o/r:review:test", ["prior"], datetime.now(timezone.utc), 1000),
        ("o/r", "o/r:review:test", (), datetime.now(), 1000),
        ("o/r", "o/r:review:test", (), datetime.now(timezone.utc), 0),
    ],
)
def test_invalid_public_scope_and_bounds_raise(rig, repository, owner, prior, now, ceiling) -> None:
    _, control, *_ = rig
    with control.transaction() as db:
        with pytest.raises(ValueError):
            observe_legacy_execution_sql_import_sources(
                db, repository, owner, configured_prior_owners=prior,
                now=now, max_source_bytes=ceiling,
            )


def test_excessive_prior_owner_tuple_is_rejected_before_source_reads(rig) -> None:
    cfg, control, *_ = rig
    priors = tuple(f"old/r:review:{index}" for index in range(MAX_LEGACY_REVIEW_OWNERS))
    with control.transaction() as db:
        with pytest.raises(ValueError, match="configured ownership"):
            observe_legacy_execution_sql_import_sources(
                db, cfg.repo, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"),
                configured_prior_owners=priors, now=datetime.now(timezone.utc),
            )
