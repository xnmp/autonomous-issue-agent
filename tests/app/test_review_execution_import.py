"""Configured import source checks use daemon boot's owner ancestry."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_activation import (
    CompleteLegacyExecutionRootFiles,
    read_complete_legacy_execution_root_files, read_complete_legacy_review_generation_receipts,
    read_complete_legacy_review_outcome_receipts, read_complete_legacy_review_rounds,
    read_complete_legacy_review_runs, read_complete_legacy_review_submission_receipts,
    read_complete_legacy_review_submissions, read_legacy_review_owner_lineage,
)
from issue_agent.adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
from issue_agent.adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, HeldLegacyExecutionCounterSources,
    ObservedLegacyExecutionSqlImportSources, RevalidatedLegacyExecutionImportSources,
    held_legacy_execution_counter_sources,
)
from issue_agent.adapters.review_execution_workflow_census import BlockedRetainedReviewWorkflows
from issue_agent.adapters.workflow_rows import encode_workflow_episodes, insert_workflow_component
from issue_agent.app.config import (
    HarnessConfig, configured_prior_owner_ids, owner_identity_for_target,
)
from issue_agent.app.review_execution_import import (
    BlockedReviewImportObservationSet, ObservedReviewExecutionImportCandidates,
    assess_configured_review_execution_import_candidates, plan_configured_review_execution_import,
    classify_configured_legacy_review_import_history,
    verify_configured_legacy_execution_import_sources,
)
from issue_agent.domain.review_execution_activation import LegacyReviewOwnerLineage
from issue_agent.domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from issue_agent.domain.review_execution_history_import import (
    ReviewImportBlockerKind, ReviewImportHistoryBlocked, ReviewImportNoHistoryObserved,
)
from issue_agent.domain.review_execution_import_plan import (
    BlockedZeroHistoryReviewImport, EligibleZeroHistoryReviewImport,
    ReviewExecutionImportPlan, ZeroHistoryImportBlockerKind,
)
from issue_agent.domain.resolve_activation import DeploymentInventory
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState, RecordedPullRequest
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey
from issue_agent.domain.world import PRView


class EmptyGateway:
    repo = "o/r"

    def _gh_json(self, _args):
        return [[]]

    def get_review_pull_request(self, _number):
        raise AssertionError("the complete empty PR listing must not request a PR")


class OnePRGateway:
    repo = "o/r"

    def _gh_json(self, _args):
        return [[{"number": 9, "state": "open"}]]

    def get_review_pull_request(self, number):
        assert number == 9
        return PRView(9, head_ref_name="work/topic", head_ref_oid="a" * 40)


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
    roots = (("fix", cfg.state_dir), ("review", cfg.review_state_dir),
             ("steward", cfg.steward_state_dir))
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
    now = datetime.now(timezone.utc) - timedelta(seconds=20)
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
        generation = read_complete_legacy_review_generation_receipts(db, cfg.repo)
    return ObservedLegacyExecutionSqlImportSources(runs, commands, receipts, rounds, outcomes, generation)


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
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)
            assert isinstance(result, RevalidatedLegacyExecutionImportSources)
            assert result.sql.runs.lineage.configured_prior_owners == (prior_owner,)
            assert result.deaths.unassigned_deaths[0].pr == 9


def test_configured_history_classifies_empty_pr_from_held_complete_sources(sources):
    cfg, control, _gateway, roots, files, _links = sources
    gateway = OnePRGateway()
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = classify_configured_legacy_review_import_history(cfg, db, held, observed)
            verified = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)

    assert len(result) == 1
    assert isinstance(result[0], ReviewImportNoHistoryObserved)
    assert result[0].pr == 9
    assert isinstance(verified, RevalidatedLegacyExecutionImportSources)
    assert result[0].source_digest == verified.source_digest


def test_configured_history_blocks_real_legacy_round_file(sources):
    cfg, control, _gateway, roots, _files, _links = sources
    gateway = OnePRGateway()
    (Path(cfg.review_state_dir) / "pr-9.round").write_text("2")
    files = read_complete_legacy_execution_root_files(roots)
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = classify_configured_legacy_review_import_history(cfg, db, held, observed)

    assert len(result) == 1
    assert isinstance(result[0], ReviewImportHistoryBlocked)
    assert result[0].pr == 9
    assert [blocker.kind for blocker in result[0].blockers] == [ReviewImportBlockerKind.ORIGIN]


def _record_review_workflow(control, pr):
    key = WorkflowKey("o/r", PullRequestRef(pr))
    snapshot = initial_review_workflow(key, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1))
    with control.transaction() as db:
        if pr == 9:
            db.execute(
                "INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                ("o/r", WorkflowFamily.REVIEW_REJECTIONS.value, 1,
                 "a" * 64, "b" * 64, "2026-09-30T00:00:00+00:00"),
            )
        db.execute(
            "INSERT INTO workflows VALUES (?,?,?,?,?,?)",
            ("o/r", "pr", pr, 0, 0, encode_workflow_episodes(snapshot)),
        )
        insert_workflow_component(db, key, snapshot.authority, snapshot.episode.episode_id)
    return key


def test_configured_candidate_joins_every_retained_row_to_current_open_pr(sources):
    cfg, control, _gateway, roots, files, _links = sources
    gateway = OnePRGateway()
    key = _record_review_workflow(control, 9)
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    observed = _sql_sources(cfg, control, include_prior=True)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    recorded = RecordedPullRequest(ObservedPullRequest(
        key, "a" * 40, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
        CompleteReviews(()), datetime.now(timezone.utc),
    ))

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = assess_configured_review_execution_import_candidates(
                cfg, db, held, observed, (recorded,),
            )

    assert isinstance(result, ObservedReviewExecutionImportCandidates)
    assert result.source_digest is not None
    assert len(result.candidates) == 1
    assert isinstance(result.candidates[0], EligibleZeroHistoryReviewImport)
    assert result.candidates[0].pr == 9
    plan = plan_configured_review_execution_import(result, DeploymentInventory("o/r"), 1)
    assert isinstance(plan, ReviewExecutionImportPlan)
    assert plan.source_digest == result.source_digest
    assert plan.decisions_at(datetime.now(timezone.utc))[0].checkpoint.state.binding.rejection == result.candidates[0].workflow.binding
    assert isinstance(plan_configured_review_execution_import(
        result, DeploymentInventory("o/r", active_runs=("run-1",)), 1,
    ), BlockedReviewImportObservationSet)


def test_configured_candidate_blocks_terminal_retained_workflow(sources):
    cfg, control, _gateway, roots, files, _links = sources
    gateway = OnePRGateway()
    key = _record_review_workflow(control, 9)
    _record_review_workflow(control, 10)
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    observed = _sql_sources(cfg, control, include_prior=True)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    recorded = RecordedPullRequest(ObservedPullRequest(
        key, "a" * 40, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
        CompleteReviews(()), datetime.now(timezone.utc),
    ))

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = assess_configured_review_execution_import_candidates(
                cfg, db, held, observed, (recorded,),
            )

    assert isinstance(result, ObservedReviewExecutionImportCandidates)
    clean, terminal = result.candidates
    assert clean.pr == 9 and isinstance(clean, EligibleZeroHistoryReviewImport)
    assert terminal.pr == 10 and isinstance(terminal, ReviewImportHistoryBlocked)
    assert terminal.blockers[0].sources[0].identity == "pr:10"
    plan = plan_configured_review_execution_import(result, DeploymentInventory("o/r"), 1)
    assert isinstance(plan, ReviewExecutionImportPlan)
    assert plan.candidates == result.candidates
    from issue_agent.domain.review_execution_migration import BlockedReviewExecutionMigration
    decisions = plan.decisions_at(datetime.now(timezone.utc))
    assert len(decisions) == 2 and isinstance(decisions[1], BlockedReviewExecutionMigration)


def test_configured_candidate_keeps_missing_remote_read_as_a_pr_blocker(sources):
    cfg, control, _gateway, roots, files, _links = sources
    gateway = OnePRGateway()
    _record_review_workflow(control, 9)
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    observed = _sql_sources(cfg, control, include_prior=True)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = assess_configured_review_execution_import_candidates(
                cfg, db, held, observed, (),
            )

    assert isinstance(result, ObservedReviewExecutionImportCandidates)
    assert len(result.source_digest) == 64
    blocked, = result.candidates
    assert isinstance(blocked, ReviewImportHistoryBlocked)
    assert blocked.blockers[0].sources[0].kind == ZeroHistoryImportBlockerKind.REMOTE_UNAVAILABLE.value
    plan = plan_configured_review_execution_import(result, DeploymentInventory("o/r"), 1)
    assert isinstance(plan, ReviewExecutionImportPlan)
    from issue_agent.domain.review_execution_migration import BlockedReviewExecutionMigration
    decision, = plan.decisions_at(datetime.now(timezone.utc))
    assert isinstance(decision, BlockedReviewExecutionMigration) and decision.history == blocked


def test_configured_import_refuses_a_substituted_legacy_root(sources, tmp_path):
    cfg, control, gateway, _roots, _files, links = sources
    substitute = tmp_path / "substituted-review-state"
    substitute.mkdir()
    roots = (("review", str(substitute)),)
    files = read_complete_legacy_execution_root_files(roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=str(substitute),
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)

    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "root" in result.reason


def test_configured_import_accepts_shared_fix_and_review_directory(sources):
    cfg, control, gateway, _roots, _files, links = sources
    cfg = replace(cfg, state_dir=cfg.review_state_dir)
    roots = (("fix", cfg.state_dir), ("review", cfg.review_state_dir),
             ("steward", cfg.steward_state_dir))
    files = read_complete_legacy_execution_root_files(roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)

    assert isinstance(result, RevalidatedLegacyExecutionImportSources)


def test_configured_import_requires_explicit_historical_root_declaration(sources, tmp_path):
    cfg, control, gateway, roots, _files, links = sources
    old = tmp_path / "old-review-state"
    old.mkdir()
    (old / "issue--9.infra-deaths").write_text("4")
    complete_roots = (*roots, (f"extra:{old.resolve()}", str(old)))
    files = read_complete_legacy_execution_root_files(complete_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, complete_roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [(item.source.root, item.source.key) for item in held.roots.values.counters] == [
            (f"extra:{old.resolve()}", -9),
        ]
        assert held.mapping.files.counters == ()
        with control.transaction() as db:
            missing = verify_configured_legacy_execution_import_sources(cfg, db, held, observed)
            declared = verify_configured_legacy_execution_import_sources(
                cfg, db, held, observed, extra_roots=(str(old),),
            )

    assert isinstance(missing, BlockedLegacyExecutionSqlImportSources)
    assert "root" in missing.reason
    assert isinstance(declared, RevalidatedLegacyExecutionImportSources)


def test_configured_import_refuses_a_false_other_role_exemption(sources, tmp_path):
    cfg, control, gateway, roots, _files, links = sources
    old = tmp_path / "old-review-state"
    old.mkdir()
    complete_roots = (*roots, (f"extra:{old.resolve()}", str(old)))
    files = read_complete_legacy_execution_root_files(complete_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    observed = _sql_sources(cfg, control, include_prior=True)

    with held_legacy_execution_counter_sources(
        control, gateway, cfg.repo, complete_roots, files, links,
        review_state_dir=cfg.review_state_dir,
        other_role_state_dirs=(cfg.state_dir, str(old)),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_configured_legacy_execution_import_sources(
                cfg, db, held, observed, extra_roots=(str(old),),
            )

    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "root" in result.reason


def test_configured_import_refuses_an_inventory_omitting_prior_owner(sources):
    cfg, control, gateway, roots, files, links = sources
    _record_prior_run(cfg, control)
    observed = _sql_sources(cfg, control, include_prior=False)
    assert not observed.runs.runs
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
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
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
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
    with held_legacy_execution_counter_sources(control, gateway, cfg.repo, roots, files, links,
                                               review_state_dir=cfg.review_state_dir,
                                               other_role_state_dirs=(cfg.state_dir, cfg.steward_state_dir)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            with pytest.raises(ValueError):
                verify_configured_legacy_execution_import_sources(changed, db, held, observed)
