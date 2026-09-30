"""Offline configured activation rederives sources and commits one authority."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import issue_agent.adapters.review_execution_import_commit as commit_module
from issue_agent.adapters.control_path import control_store_path
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.resolve_activation import distinct_roots
from issue_agent.adapters.review_execution_activation import (
    CompleteLegacyExecutionRootFiles, CompleteLegacySubmissionSettlementStorage,
    read_complete_legacy_execution_root_files, read_complete_legacy_review_generation_receipts,
    read_complete_legacy_review_outcome_receipts, read_complete_legacy_review_rounds,
    read_complete_legacy_review_runs, read_complete_legacy_review_submission_receipts,
    read_complete_legacy_review_submissions, read_legacy_review_owner_lineage,
    verify_complete_legacy_round_outcome_storage, verify_complete_legacy_submission_settlement_storage,
)
from issue_agent.adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
from issue_agent.adapters.review_execution_import_sources import ObservedLegacyExecutionSqlImportSources
from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, insert_workflow_component, load_review_workflow_row,
)
from issue_agent.app.config import HarnessConfig, configured_prior_owner_ids, owner_identity_for_target
from issue_agent.app.review_execution_activation import (
    ConfiguredReviewExecutionActivation, ReviewExecutionActivationRefused,
)
from issue_agent.domain.events import ControlTransitionCommitted, EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.review_execution import CurrentReviewExecution
from issue_agent.domain.review_execution_activation import LegacyReviewOwnerLineage
from issue_agent.domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from issue_agent.domain.review_workflow import CompleteReviews, PartialReviews, SubmittedReviewFact, SubmittedReviewState
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey
from issue_agent.domain.world import PRView


class Gateway:
    repo = "o/r"

    def __init__(self):
        self.views = {pr: PRView(pr, head_ref_name=f"work/{pr}", head_ref_oid="a" * 40,
                                labels=frozenset({Label.AI_PR_OPEN})) for pr in (9, 10)}
        self.reviews = {pr: CompleteReviews(()) for pr in self.views}
        self.listed = (9, 10)
        self.unavailable = False

    def _gh_json(self, _args):
        if self.unavailable:
            raise AssertionError("exact retry must not consult GitHub")
        return [[{"number": pr, "state": "open"} for pr in self.listed]]

    def get_pr(self, pr, *, mergeability=False, checks=False):
        if self.unavailable:
            raise AssertionError("exact retry must not consult GitHub")
        return self.views[pr]

    def get_review_pull_request(self, pr):
        return self.get_pr(pr)

    def pull_request_reviews(self, pr):
        return self.reviews[pr]


def _sql_sources(cfg, control):
    now = datetime.now(timezone.utc) - timedelta(seconds=20)
    with control.transaction() as db:
        lineage = read_legacy_review_owner_lineage(
            db, cfg.repo, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"),
            configured_prior_owners=configured_prior_owner_ids(cfg, "review"),
        )
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        runs = read_complete_legacy_review_runs(db, lineage)
        commands = read_complete_legacy_review_submissions(db, cfg.repo)
        receipts = read_complete_legacy_review_submission_receipts(db, cfg.repo)
        rounds = read_complete_legacy_review_rounds(db, cfg.repo)
        outcomes = read_complete_legacy_review_outcome_receipts(db, cfg.repo)
        storage = verify_complete_legacy_round_outcome_storage(db, rounds, outcomes)
        settlement = verify_complete_legacy_submission_settlement_storage(db, commands, receipts, storage)
        generation = read_complete_legacy_review_generation_receipts(db, cfg.repo)
    assert isinstance(settlement, CompleteLegacySubmissionSettlementStorage)
    return ObservedLegacyExecutionSqlImportSources(runs, settlement, generation)


@pytest.fixture
def rig(tmp_path):
    paths = {name: tmp_path / name for name in ("checkout", "fix", "review", "steward")}
    for path in paths.values():
        path.mkdir()
    cfg = HarnessConfig(repo="o/r", target_dir=str(paths["checkout"]), state_dir=str(paths["fix"]),
                        review_state_dir=str(paths["review"]), steward_state_dir=str(paths["steward"]),
                        control_authority_root=str(tmp_path / "authority"), fix_bot="fixer")
    control = SQLiteControlStore(control_store_path(
        (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir), authority_root=cfg.control_authority_root,
    ))
    control.initialize()
    authority = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   (cfg.repo, authority.family.value, 1, "a" * 64, "b" * 64, datetime.now(timezone.utc).isoformat()))
        for pr in (9, 10):
            key = WorkflowKey(cfg.repo, PullRequestRef(pr))
            workflow = initial_review_workflow(key, authority)
            db.execute("INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)", (cfg.repo, pr, encode_workflow_episodes(workflow)))
            insert_workflow_component(db, key, authority, workflow.episode.episode_id)
    gateway = Gateway()
    store = Store(cfg.review_state_dir)
    files = read_complete_legacy_execution_root_files((("fix", cfg.state_dir), ("review", cfg.review_state_dir), ("steward", cfg.steward_state_dir)))
    links = read_complete_legacy_open_counter_links(control, gateway, cfg.repo)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    sql = _sql_sources(cfg, control)
    liveness = []
    activation = ConfiguredReviewExecutionActivation(cfg, control, gateway, store, liveness=lambda: tuple(liveness))
    return cfg, control, gateway, store, files, links, sql, liveness, activation


def _dump(control):
    with sqlite3.connect(control.path) as db:
        return tuple(db.iterdump())


def test_offline_activation_is_atomic_hot_readable_and_exact_retry_is_inert(rig):
    cfg, control, gateway, _store, files, links, sql, liveness, activation = rig
    before = _dump(control)
    plan = activation.plan(files, links, sql)
    assert _dump(control) == before
    published = []
    now = datetime.now(timezone.utc)
    producer = Producer("offline-rehearsal", cfg.repo, "review", 123, "boot")
    control.bind_event_outbox(lambda payload, correlation: EventEnvelope(
        payload, new_event_id(now), 0, now, now, 0, producer, correlation,
    ), published.append)
    result = activation.activate(plan, files, links, sql)
    assert result.plan == plan and not result.idempotent
    with control.transaction() as db:
        for pr in (9, 10):
            snapshot = load_review_workflow_row(db, WorkflowKey(cfg.repo, PullRequestRef(pr)))
            assert snapshot is not None and isinstance(snapshot.execution, CurrentReviewExecution)
            assert snapshot.execution.baseline.through_sequence == 0
        assert db.execute("SELECT count(*) FROM workflow_activation_imports WHERE family='review_execution'").fetchone()[0] == 2
    assert len(published) == 1
    payload = published[0].payload
    assert isinstance(payload, ControlTransitionCommitted)
    assert payload.entity_kind == "workflow_activation" and payload.attributes["imported"] == 2
    gateway.unavailable = True
    liveness.append("watcher is now running")
    Path(cfg.review_state_dir, "pr-9.round").write_text("99")
    after = _dump(control)
    retry = activation.activate(plan, files, links, sql)
    assert retry.idempotent and retry.plan == plan
    assert _dump(control) == after and len(published) == 1


@pytest.mark.parametrize("change", ("file", "head", "listing", "hold", "review", "partial", "workflow", "active_run", "foreign_authority", "liveness"))
def test_changed_sources_or_nonquiescence_refuse_without_activation(rig, change):
    cfg, control, gateway, _store, files, links, sql, liveness, activation = rig
    plan = activation.plan(files, links, sql)
    if change == "file":
        Path(cfg.review_state_dir, "pr-9.round").write_text("1")
    elif change == "head":
        gateway.views[10] = replace(gateway.views[10], head_ref_oid="b" * 40)
    elif change == "listing":
        gateway.listed = (9,)
    elif change == "hold":
        gateway.views[10] = replace(gateway.views[10], labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_BUDGET_REVIEW}))
    elif change == "review":
        gateway.reviews[10] = CompleteReviews((SubmittedReviewFact("review-1", cfg.review_bot, SubmittedReviewState.APPROVED,
                                                               "a" * 40, datetime.now(timezone.utc)),))
    elif change == "partial":
        gateway.reviews[10] = PartialReviews("network failure")
    elif change == "workflow":
        with control.transaction() as db:
            db.execute("UPDATE workflows SET version=1 WHERE target_number=10")
    elif change == "active_run":
        with control.transaction() as db:
            db.execute("INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version) VALUES (?,?,?,?,?,?)",
                       ("active-run", "dispatch", "REVIEW", "o/r:review", "running", "cfg"))
    elif change == "foreign_authority":
        plan = replace(plan, inventory_digest="f" * 64)
    else:
        liveness.append("review process exists")
    before = _dump(control)
    with pytest.raises((ReviewExecutionActivationRefused, WorkflowAuthorityError)):
        activation.activate(plan, files, links, sql)
    assert _dump(control) == before


def test_final_liveness_recheck_refuses_after_remote_reads(rig):
    cfg, control, gateway, store, files, links, sql, _liveness, initial = rig
    plan = initial.plan(files, links, sql)
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        return () if calls == 1 else ("late watcher",)

    activation = ConfiguredReviewExecutionActivation(cfg, control, gateway, store, liveness=probe)
    before = _dump(control)
    with pytest.raises(ReviewExecutionActivationRefused, match="late watcher"):
        activation.activate(plan, files, links, sql)
    assert _dump(control) == before


def test_file_changed_during_remote_reads_refuses_at_acceptance(rig, monkeypatch):
    cfg, control, gateway, _store, files, links, sql, _liveness, activation = rig
    plan = activation.plan(files, links, sql)
    read = gateway.pull_request_reviews

    def changed(pr):
        Path(cfg.review_state_dir, "pr-10.round").write_text("2")
        return read(pr)

    monkeypatch.setattr(gateway, "pull_request_reviews", changed)
    before = _dump(control)
    with pytest.raises(ReviewExecutionActivationRefused, match="files changed"):
        activation.activate(plan, files, links, sql)
    assert _dump(control) == before


@pytest.mark.parametrize("sources_disappear", (False, True))
def test_concurrent_exact_winner_is_replayed_without_duplicate_authority(rig, sources_disappear):
    _cfg, control, gateway, store, files, links, sql, _liveness, winner = rig
    cfg = rig[0]
    plan = winner.plan(files, links, sql)
    committed_dump = None

    def probe():
        nonlocal committed_dump
        if committed_dump is None:
            assert not winner.activate(plan, files, links, sql).idempotent
            committed_dump = _dump(control)
            gateway.unavailable = sources_disappear
        return ()

    contender = ConfiguredReviewExecutionActivation(cfg, control, gateway, store, liveness=probe)
    result = contender.activate(plan, files, links, sql)
    assert result.idempotent
    assert committed_dump is not None and _dump(control) == committed_dump


@pytest.mark.parametrize("evidence", (None, [], ("",), ("x" * 513,), ("running",) * 4097))
def test_malformed_liveness_evidence_cannot_authorize_activation(rig, evidence):
    cfg, control, gateway, store, files, links, sql, _liveness, initial = rig
    plan = initial.plan(files, links, sql)
    activation = ConfiguredReviewExecutionActivation(cfg, control, gateway, store, liveness=lambda: evidence)
    before = _dump(control)
    with pytest.raises(ReviewExecutionActivationRefused, match="malformed"):
        activation.activate(plan, files, links, sql)
    assert _dump(control) == before


def test_second_pr_failure_rolls_back_entire_composed_activation(rig, monkeypatch):
    _cfg, control, _gateway, _store, files, links, sql, _liveness, activation = rig
    plan = activation.plan(files, links, sql)
    insert = commit_module.insert_imported_review_execution_origin
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second origin failure")
        return insert(*args, **kwargs)

    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", fail_second)
    before = _dump(control)
    with pytest.raises(RuntimeError, match="injected second origin"):
        activation.activate(plan, files, links, sql)
    assert _dump(control) == before
    monkeypatch.setattr(commit_module, "insert_imported_review_execution_origin", insert)
    assert not activation.activate(plan, files, links, sql).idempotent


def test_configuration_cannot_substitute_sql_or_review_authority(rig, tmp_path):
    cfg, control, gateway, store, *_rest = rig
    wrong = SQLiteControlStore(str(tmp_path / "other.db"))
    wrong.initialize()
    with pytest.raises(ValueError, match="authority differs"):
        ConfiguredReviewExecutionActivation(cfg, wrong, gateway, store, liveness=lambda: ())
    with pytest.raises(ValueError, match="authority differs"):
        ConfiguredReviewExecutionActivation(cfg, control, gateway, Store(str(tmp_path / "other-review")), liveness=lambda: ())


@pytest.mark.parametrize("deduplicate", (False, True))
def test_shared_role_root_accepts_the_complete_reviewed_representation(rig, deduplicate):
    cfg, control, gateway, store, _files, links, sql, _liveness, _activation = rig
    cfg = replace(cfg, state_dir=cfg.review_state_dir)
    roots = (("fix", cfg.state_dir), ("review", cfg.review_state_dir), ("steward", cfg.steward_state_dir))
    files = read_complete_legacy_execution_root_files(distinct_roots(roots) if deduplicate else roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    activation = ConfiguredReviewExecutionActivation(cfg, control, gateway, store, liveness=lambda: ())
    plan = activation.plan(files, links, sql)
    assert not activation.activate(plan, files, links, sql).idempotent
    gateway.unavailable = True
    assert activation.activate(plan, files, links, sql).idempotent
