"""Configured import source checks use daemon boot's owner ancestry."""

from dataclasses import replace
import json

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_activation import (
    CompleteLegacyExecutionRootFiles, CompleteLegacySubmissionSettlementStorage,
    read_complete_legacy_execution_root_files, read_complete_legacy_review_generation_receipts,
    read_complete_legacy_review_outcome_receipts, read_complete_legacy_review_rounds,
    read_complete_legacy_review_runs, read_complete_legacy_review_submission_receipts,
    read_complete_legacy_review_submissions, read_legacy_review_owner_lineage,
    verify_complete_legacy_round_outcome_storage, verify_complete_legacy_submission_settlement_storage,
)
from issue_agent.adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
from issue_agent.adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, HeldLegacyExecutionCounterSources,
    ObservedLegacyExecutionSqlImportSources, RevalidatedLegacyExecutionImportSources,
    held_legacy_execution_counter_sources,
)
from issue_agent.app.config import (
    HarnessConfig, configured_prior_owner_ids, owner_identity_for_target,
)
from issue_agent.app.review_execution_import import verify_configured_legacy_execution_import_sources
from issue_agent.domain.review_execution_activation import LegacyReviewOwnerLineage
from issue_agent.domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks


class EmptyGateway:
    repo = "o/r"

    def _gh_json(self, _args):
        return [[]]

    def get_review_pull_request(self, _number):
        raise AssertionError("the complete empty PR listing must not request a PR")


@pytest.fixture
def sources(tmp_path):
    prior = tmp_path / "prior-checkout"
    current = tmp_path / "current-checkout"
    root = tmp_path / "review-state"
    for path in (prior, current, root):
        path.mkdir()
    cfg = HarnessConfig(
        repo="o/r", target_dir=str(current), state_dir=str(tmp_path / "fix-state"),
        review_state_dir=str(root), steward_state_dir=str(tmp_path / "steward-state"),
        fix_bot="fixer", repointed_from_deployments=(("o/r", str(prior)),),
    )
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    gateway = EmptyGateway()
    roots = (("review", str(root)),)
    files = read_complete_legacy_execution_root_files(roots)
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    return cfg, control, gateway, roots, files, links


def _sql_sources(cfg, control, *, include_prior):
    prior_owner = owner_identity_for_target(
        cfg.repo, cfg.repointed_from_deployments[0][1], "review",
    )
    current_owner = owner_identity_for_target(cfg.repo, cfg.target_dir, "review")
    with control.transaction() as db:
        lineage = read_legacy_review_owner_lineage(
            db, cfg.repo, current_owner,
            configured_prior_owners=(prior_owner,) if include_prior else (),
        )
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        runs = read_complete_legacy_review_runs(db, lineage)
        commands = read_complete_legacy_review_submissions(db, cfg.repo)
        receipts = read_complete_legacy_review_submission_receipts(db, cfg.repo)
        rounds = read_complete_legacy_review_rounds(db, cfg.repo)
        outcomes = read_complete_legacy_review_outcome_receipts(db, cfg.repo)
        round_storage = verify_complete_legacy_round_outcome_storage(db, rounds, outcomes)
        settlement = verify_complete_legacy_submission_settlement_storage(
            db, commands, receipts, round_storage,
        )
        generation = read_complete_legacy_review_generation_receipts(db, cfg.repo)
        assert isinstance(settlement, CompleteLegacySubmissionSettlementStorage)
    return ObservedLegacyExecutionSqlImportSources(runs, settlement, generation)


def _record_prior_run(cfg, control):
    prior_owner = owner_identity_for_target(
        cfg.repo, cfg.repointed_from_deployments[0][1], "review",
    )
    with control.transaction() as db:
        db.execute(
            "INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version) "
            "VALUES (?,?,?,?,?,?)",
            ("prior-run", json.dumps({
                "repository": "o/r", "role": "review", "kind": "REVIEW", "number": 9,
                "breaker_key": -9, "issue": None,
            }), "REVIEW", prior_owner, "abandoned", "cfg"),
        )


def test_configured_import_revalidates_the_full_boot_owner_lineage(sources):
    cfg, control, gateway, roots, files, links = sources
    prior_owner = owner_identity_for_target(
        cfg.repo, cfg.repointed_from_deployments[0][1], "review",
    )
    assert configured_prior_owner_ids(cfg, "review") == ("o/r:review", prior_owner)
    _record_prior_run(cfg, control)
    observed = _sql_sources(cfg, control, include_prior=True)
    assert len(observed.runs.runs) == 1
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)
            assert isinstance(result, RevalidatedLegacyExecutionImportSources)
            assert result.sql == observed
            assert [run.source.run_id for run in result.deaths.unassigned_deaths] == ["prior-run"]


def test_configured_import_refuses_an_inventory_omitting_prior_owner(sources):
    cfg, control, gateway, roots, files, links = sources
    _record_prior_run(cfg, control)
    observed = _sql_sources(cfg, control, include_prior=False)
    assert not observed.runs.runs
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "configuration" in result.reason


def test_configured_import_refuses_an_inventory_omitting_repo_alias_owners(sources):
    cfg, control, gateway, roots, files, links = sources
    observed = _sql_sources(cfg, control, include_prior=True)
    alias_cfg = replace(cfg, repo_input="O/R")
    assert "O/R:review" in configured_prior_owner_ids(alias_cfg, "review")
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(alias_cfg, db, held, observed)
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "configuration" in result.reason


@pytest.mark.parametrize("change", (
    {"repointed_from_deployments": (("foreign/repo", "/tmp/foreign"),)},
    {"target_dir": "relative-checkout"},
    {"repo": "bad/?"},
))
def test_configured_import_refuses_invalid_deployment_identity(sources, change):
    cfg, control, gateway, roots, files, links = sources
    observed = _sql_sources(cfg, control, include_prior=True)
    changed = replace(cfg, **change)
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            with pytest.raises(ValueError):
                verify_configured_legacy_execution_import_sources(changed, db, held, observed)
