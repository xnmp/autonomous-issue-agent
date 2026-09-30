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
from issue_agent.adapters.review_lifecycle_archive_audit import advance_review_lifecycle_archive_audit
from issue_agent.adapters.review_registration_membership_audit import (
    advance_review_registration_membership_audit,
    observe_matched_review_registration_membership,
)
from issue_agent.app.config import configured_prior_owner_ids, owner_identity_for_target
from issue_agent.domain.review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from issue_agent.domain.review_execution_activation import MAX_LEGACY_REVIEW_OWNERS
from issue_agent.domain.review_registration_membership_audit import CompletedReviewRegistrationMembership
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.app.test_review_execution_activation import rig
from tests.adapters.test_review_execution_control import OWNER, rig as round_rig
from tests.adapters.test_review_execution_checkpoint_commit import _copy_source
from tests.adapters.test_review_archive_source_versions import _insert_lifecycle_command


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


def _complete_census(
    control: SQLiteControlStore, repository: str, *, require_match: bool = True,
) -> datetime:
    start = datetime.now(timezone.utc)
    for step in range(64):
        with control.transaction() as db:
            lifecycle = advance_review_lifecycle_archive_audit(
                db, repository, now=start + timedelta(seconds=step),
            )
            registration = advance_review_registration_membership_audit(
                db, repository, now=start + timedelta(seconds=step),
            )
            matched = False
            if (isinstance(registration, CompletedReviewRegistrationMembership)
                    and isinstance(lifecycle, CompletedReviewLifecycleArchiveAudit)):
                try:
                    observe_matched_review_registration_membership(
                        db, repository, now=start + timedelta(seconds=step),
                    )
                    matched = True
                except WorkflowAuthorityError:
                    pass
        if matched or not require_match and (
            isinstance(registration, CompletedReviewRegistrationMembership)
            and isinstance(lifecycle, CompletedReviewLifecycleArchiveAudit)
        ):
            return start + timedelta(seconds=step)
    raise AssertionError("source census did not complete")


def test_observation_requires_current_completed_censuses(tmp_path: Path) -> None:
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    before = _dump(control)
    with control.transaction() as db:
        result = observe_legacy_execution_sql_import_sources(
            db, "o/r", "o/r:review:test", now=datetime.now(timezone.utc),
        )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "census" in result.reason or "membership" in result.reason
    assert _dump(control) == before


def test_overdue_completed_census_cannot_supply_sql_observation(rig) -> None:
    result = _collect(rig, now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "overdue" in result.reason


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


def test_complete_nonempty_round_sources_remain_observations(round_rig) -> None:
    control, key, _, _ = _copy_source(round_rig)
    certified_at = _complete_census(control, key.repository)
    before = _dump(control)
    with control.transaction() as db:
        result = observe_legacy_execution_sql_import_sources(
            db, key.repository, OWNER, now=certified_at,
        )
    assert isinstance(result, ObservedLegacyExecutionSqlImportSources)
    assert result.settlement.round_storage.rounds.rounds
    assert result.settlement.commands.submissions
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


def test_orphan_outcome_receipt_invalidates_census_then_blocks_collection(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-outcome',?,?,?,?)",
            (cfg.repo, "orphan-round", datetime.now(timezone.utc).isoformat(), "{}"),
        )
    stale = _collect(rig)
    assert isinstance(stale, BlockedLegacyExecutionSqlImportSources)
    certified_at = _complete_census(control, cfg.repo)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "outcome" in result.reason or "round" in result.reason
    assert _dump(control) == before


def test_unregistered_submission_command_blocks_collection(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        _insert_lifecycle_command(db, "unregistered-import-command", state="pending")
        db.execute(
            "UPDATE lifecycle_commands SET repository=? WHERE command_id=?",
            (cfg.repo, "unregistered-import-command"),
        )
    certified_at = _complete_census(control, cfg.repo, require_match=False)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "registration" in result.reason or "command" in result.reason
    assert _dump(control) == before


def test_oversized_retained_source_and_cumulative_ceiling_refuse(rig) -> None:
    cfg, control, *_ = rig
    with control.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-generation',?,?,?,?)",
            (cfg.repo, "oversized-generation", datetime.now(timezone.utc).isoformat(), "x" * (2**21 + 1)),
        )
    certified_at = _complete_census(control, cfg.repo)
    before = _dump(control)
    result = _collect(rig, now=certified_at)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "preflight" in result.reason or "bound" in result.reason or "oversized" in result.reason
    assert _dump(control) == before


def test_cumulative_source_ceiling_refuses_even_small_complete_sources(rig) -> None:
    # Even a complete empty corpus cannot be read through an arbitrarily
    # smaller requested ceiling; the meter includes repeated audit reads.
    limited = _collect(rig, max_source_bytes=64)
    assert isinstance(limited, BlockedLegacyExecutionSqlImportSources)
    assert "bound" in limited.reason


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
