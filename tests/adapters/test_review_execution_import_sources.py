"""Activation-source scope keeps remote-effect barriers over complete attribution."""

from tests.domain.builders import observed_pr

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.deployment_authority import deployment_effect_authority
from issue_agent.adapters.review_execution_activation import (
    CompleteLegacyExecutionRootFiles,
    read_complete_legacy_execution_root_files, read_complete_legacy_review_generation_receipts,
    read_complete_legacy_review_outcome_receipts, read_complete_legacy_review_rounds,
    read_complete_legacy_review_runs, read_complete_legacy_review_submission_receipts,
    read_complete_legacy_review_submissions, read_legacy_review_owner_lineage,
)
from issue_agent.adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.review_execution_import_sources import (
    BlockedLegacyExecutionCounterSources, BlockedLegacyExecutionSqlImportSources,
    HeldLegacyExecutionCounterSources, ObservedLegacyExecutionSqlImportSources,
    RevalidatedLegacyExecutionImportSources, held_legacy_execution_counter_sources,
    verify_legacy_execution_import_sources,
)
from issue_agent.domain.review_execution_activation import (
    CompleteLegacyReviewGenerationReceipts, CompleteLegacyReviewRuns, LegacyReviewOwnerLineage,
)
from issue_agent.domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from issue_agent.domain.review_execution_death_import import (
    CompleteLegacyReviewDeathAttribution, attribute_complete_legacy_review_deaths,
)
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.world import IssueView, PRView
from tests.adapters.test_review_execution_control import rig
from tests.adapters.test_review_productive_settlement import confirmed_copy

def _insert_lifecycle_command(db, command_id, state="pending"):
    db.execute(
        "INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,kind,target,"
        "payload_json,payload_digest,state,created_at,updated_at) "
        "VALUES (?,?,?,'submit_review','pr:1','{}',?,?,"
        "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00')",
        (command_id, command_id, command_id, hashlib.sha256(b"{}").hexdigest(), state),
    )


HEAD = "a" * 40
WRITER = WorkflowWriter("o/r", "review", "o/r:review:test")


class Gateway:
    repo = "o/r"

    def __init__(self):
        self.views = {
            9: observed_pr(9, head_ref_name="fix/7-work", head_ref_oid=HEAD),
            10: observed_pr(10, head_ref_name="work/topic", head_ref_oid=HEAD),
        }

    def _gh_json(self, _args):
        return [[{"number": number, "state": "open"} for number in sorted(self.views)]]

    def get_review_pull_request(self, number):
        return self.views[number]

    def get_review_linked_issue(self, number):
        return IssueView(number)

    def review_issue_blockers(self, _number):
        return ()


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    (root / "issue-7.infra-deaths").write_text("2")
    (root / "issue--10.infra-deaths").write_text("3")
    roots = (("review", str(root)),)
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    gateway = Gateway()
    files = read_complete_legacy_execution_root_files(roots)
    links = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    return control, gateway, roots, files, links


def test_complete_counter_sources_keep_all_effect_barriers_through_sql_scope(sources):
    control, gateway, roots, files, links = sources
    with control._connect() as db:
        before = tuple(db.iterdump())
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert {(item.counter.source.key, item.pr) for item in held.mapping.assignments} == {(7, 9), (-10, 10)}
        assert not held.mapping.unassigned
        assert len(held.source_digest) == 64
        for role in ("fix", "review"):
            with pytest.raises(BlockingIOError):
                with deployment_effect_authority(control.path, "o/r", role, blocking=False):
                    pass
        with control.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0
    for role in ("fix", "review"):
        with deployment_effect_authority(control.path, "o/r", role, blocking=False):
            pass
    with control._connect() as db:
        assert tuple(db.iterdump()) == before


def test_nonreview_root_counter_for_an_open_pr_is_retained_without_review_credit(sources, tmp_path):
    control, gateway, roots, _files, links = sources
    fix_root = tmp_path / "fix"
    fix_root.mkdir()
    (fix_root / "issue--9.infra-deaths").write_text("5")
    mixed_roots = (("fix", str(fix_root)), *roots)
    files = read_complete_legacy_execution_root_files(mixed_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", mixed_roots, files, links,
                                               review_state_dir=roots[0][1],
                                               other_role_state_dirs=(str(fix_root),)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [(item.counter.source.key, item.pr) for item in held.mapping.assignments] == [(-10, 10), (7, 9)]
        assert [(item.source.root, item.source.key) for item in held.roots.values.counters] == [
            ("review", -10), ("fix", -9), ("review", 7),
        ]


def test_nonreview_root_session_for_an_open_pr_stays_outside_review_selection(sources, tmp_path):
    control, gateway, roots, _files, links = sources
    fix_root = tmp_path / "fix"
    fix_root.mkdir()
    (fix_root / "pr-9.session").write_text("legacy-session")
    mixed_roots = (("fix", str(fix_root)), *roots)
    files = read_complete_legacy_execution_root_files(mixed_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", mixed_roots, files, links,
                                               review_state_dir=roots[0][1],
                                               other_role_state_dirs=(str(fix_root),)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [item.source.root for item in held.roots.values.prs] == ["fix"]
        assert held.mapping.files.prs == ()


def test_missing_review_root_blocks_even_when_no_counter_files_exist(sources, tmp_path):
    control, gateway, _roots, _files, links = sources
    fix_root = tmp_path / "fix"
    fix_root.mkdir()
    roots = (("fix", str(fix_root)),)
    files = read_complete_legacy_execution_root_files(roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links,
                                               review_state_dir=str(tmp_path / "missing-review")) as held:
        assert isinstance(held, BlockedLegacyExecutionCounterSources)
        assert "review root" in held.reason


def test_nonreview_root_counter_without_an_open_key_remains_in_full_audit(sources, tmp_path):
    control, gateway, roots, _files, links = sources
    fix_root = tmp_path / "fix"
    fix_root.mkdir()
    (fix_root / "issue--11.infra-deaths").write_text("5")
    mixed_roots = (("fix", str(fix_root)), *roots)
    files = read_complete_legacy_execution_root_files(mixed_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", mixed_roots, files, links,
                                               review_state_dir=roots[0][1],
                                               other_role_state_dirs=(str(fix_root),)) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [(item.counter.source.key, item.pr) for item in held.mapping.assignments] == [(-10, 10), (7, 9)]
        assert held.mapping.unassigned == ()
        assert [(item.source.root, item.source.key) for item in held.roots.values.counters] == [
            ("fix", -11), ("review", -10), ("review", 7),
        ]


@pytest.mark.parametrize("filename, contents", (
    ("issue--9.infra-deaths", "5"),
    ("issue-7.infra-deaths", "5"),
    ("pr-9.session", "claude:old-session"),
    ("pr-9.round", "2"),
))
def test_historical_counters_require_provenance_and_pr_markers_remain_captured(sources, tmp_path, filename, contents):
    control, gateway, roots, _files, links = sources
    old = tmp_path / "old-review"
    old.mkdir()
    (old / filename).write_text(contents)
    complete_roots = (*roots, (f"extra:{old.resolve()}", str(old)))
    files = read_complete_legacy_execution_root_files(complete_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(
        control, gateway, "o/r", complete_roots, files, links, review_state_dir=roots[0][1],
    ) as held:
        if filename.startswith('issue-'):
            assert isinstance(held, BlockedLegacyExecutionCounterSources)
            assert "historical" in held.reason
        else:
            assert isinstance(held, HeldLegacyExecutionCounterSources)
            assert any(item.source.root == f'extra:{old.resolve()}' for item in held.roots.values.prs)
            assert not any(item.source.root == f'extra:{old.resolve()}' for item in held.mapping.files.prs)


def test_historical_root_without_an_open_review_key_is_retained(sources, tmp_path):
    control, gateway, roots, _files, links = sources
    old = tmp_path / "old-review"
    old.mkdir()
    (old / "issue--11.infra-deaths").write_text("5")
    complete_roots = (*roots, (f"extra:{old.resolve()}", str(old)))
    files = read_complete_legacy_execution_root_files(complete_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(
        control, gateway, "o/r", complete_roots, files, links, review_state_dir=roots[0][1],
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [(item.source.root, item.source.key) for item in held.roots.values.counters
                if item.source.root.startswith("extra:")] == [(f"extra:{old.resolve()}", -11)]


def test_arbitrary_foreign_root_label_cannot_hide_an_open_counter(sources, tmp_path):
    control, gateway, roots, _files, links = sources
    old = tmp_path / "old-review"
    old.mkdir()
    (old / "issue--9.infra-deaths").write_text("5")
    complete_roots = (*roots, ("old-review", str(old)))
    files = read_complete_legacy_execution_root_files(complete_roots)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)

    with held_legacy_execution_counter_sources(
        control, gateway, "o/r", complete_roots, files, links, review_state_dir=roots[0][1],
    ) as held:
        assert isinstance(held, BlockedLegacyExecutionCounterSources)
        assert "historical" in held.reason


def test_review_root_alias_can_use_the_first_physical_root_label(sources):
    control, gateway, roots, _files, links = sources
    review_dir = roots[0][1]
    aliases = (("fix", review_dir), ("review", review_dir))
    files = read_complete_legacy_execution_root_files(aliases)
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert {item.source.root for item in files.values.counters} == {"fix"}

    with held_legacy_execution_counter_sources(control, gateway, "o/r", aliases, files, links,
                                               review_state_dir=review_dir) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        assert [(item.counter.source.key, item.pr) for item in held.mapping.assignments] == [(-10, 10), (7, 9)]


@pytest.mark.parametrize("change", ("file", "link", "repository", "ambiguous_issue"))
def test_counter_source_scope_blocks_changed_or_ambiguous_evidence(sources, change):
    control, gateway, roots, files, links = sources
    if change == "file":
        from pathlib import Path
        (Path(roots[0][1]) / "issue-7.infra-deaths").write_text("4")
    elif change == "link":
        gateway.views[9] = replace(gateway.views[9], head_ref_oid="b" * 40)
    elif change == "repository":
        gateway.repo = "another/repo"
    else:
        gateway.views[10] = replace(gateway.views[10], head_ref_name="fix/7-other")
        links = read_complete_legacy_open_counter_links(control, gateway, "o/r")
        assert isinstance(links, CompleteLegacyOpenCounterLinks)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, BlockedLegacyExecutionCounterSources)
        assert held.reason
        with pytest.raises(BlockingIOError):
            with deployment_effect_authority(control.path, "o/r", "review", blocking=False):
                pass
    with deployment_effect_authority(control.path, "o/r", "review", blocking=False):
        pass


def _observed_sql_sources(control):
    now = datetime.now(timezone.utc) - timedelta(seconds=20)
    with control.transaction() as db:
        lineage = read_legacy_review_owner_lineage(db, "o/r", "o/r:review:test")
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        runs = read_complete_legacy_review_runs(db, lineage)
        commands = read_complete_legacy_review_submissions(db, "o/r")
        receipts = read_complete_legacy_review_submission_receipts(db, "o/r")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        outcomes = read_complete_legacy_review_outcome_receipts(db, "o/r")
        generation = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(runs, CompleteLegacyReviewRuns)
        assert isinstance(generation, CompleteLegacyReviewGenerationReceipts)
    return ObservedLegacyExecutionSqlImportSources(runs, commands, receipts, rounds, outcomes, generation)


def _record_abandoned_run(control, run_id: str, owner: str, dispatch_id: str):
    with control.transaction() as db:
        db.execute(
            "INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, dispatch_id, "REVIEW", owner, "abandoned", "cfg"),
        )


def _dispatch(pr: int, key: int) -> str:
    return json.dumps({
        "repository": "o/r", "role": "review", "kind": "REVIEW", "number": pr,
        "breaker_key": key, "issue": key if key > 0 else None,
    })


def test_sql_import_sources_revalidate_in_the_barrier_held_acceptance_transaction(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            before = db.total_changes
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
            assert isinstance(result, RevalidatedLegacyExecutionImportSources)
            assert db.total_changes == before
            assert result.sql == observed
            assert [(debit.pr, debit.count) for debit in result.deaths.debits] == [(9, 2), (10, 3)]
            assert len(result.source_digest) == 64
            with pytest.raises(BlockingIOError):
                with deployment_effect_authority(control.path, "o/r", "review", blocking=False):
                    pass


def test_sql_import_sources_match_one_real_registered_command(rig, tmp_path):
    control, _, _, _, _, command = confirmed_copy(rig)
    root = tmp_path / "review-state"
    root.mkdir()
    roots = (("review", str(root)),)
    files = read_complete_legacy_execution_root_files(roots)
    gateway = Gateway()
    links = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(files, CompleteLegacyExecutionRootFiles)
    assert isinstance(links, CompleteLegacyOpenCounterLinks)
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(
        control, gateway, "o/r", roots, files, links, review_state_dir=str(root),
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
    assert isinstance(result, RevalidatedLegacyExecutionImportSources)
    assert dict(result.sql.commands.submissions[0].command_fields)["command_id"] == command.command_id


def test_sql_import_sources_block_a_new_unregistered_review_command(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with control.transaction() as db:
        _insert_lifecycle_command(db, "unregistered-import-command", state="pending")
        db.execute(
            "UPDATE lifecycle_commands SET repository='o/r' "
            "WHERE command_id='unregistered-import-command'",
        )
    with held_legacy_execution_counter_sources(
        control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1],
    ) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "changed" in result.reason






def test_sql_import_source_result_carries_current_and_predecessor_deaths_separately(sources):
    control, gateway, roots, files, links = sources
    _record_abandoned_run(control, "current-run", WRITER.owner_id, _dispatch(9, 7))
    _record_abandoned_run(control, "prior-run", "o/r:review", _dispatch(9, -9))
    observed = _observed_sql_sources(control)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
            assert isinstance(result, RevalidatedLegacyExecutionImportSources)
            debit = next(item for item in result.deaths.debits if item.pr == 9)
            assert [item.source.run_id for item in debit.current_owner_runs] == ["current-run"]
            assert [item.source.run_id for item in debit.predecessor_runs] == ["prior-run"]
    assert [run.run_id for run in observed.runs.runs] == ["current-run", "prior-run"]


def test_sql_import_source_verification_blocks_unknown_abandoned_run(sources):
    control, gateway, roots, files, links = sources
    _record_abandoned_run(control, "opaque-run", WRITER.owner_id, "opaque")
    observed = _observed_sql_sources(control)

    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )

    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "opaque-run is unattributed" in result.reason


@pytest.mark.parametrize("change", ("lineage", "settlement", "generation"))
def test_sql_import_sources_block_changed_caller_held_inventories(sources, change):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with control.transaction() as db:
        if change == "lineage":
            db.execute("INSERT INTO retired_owners VALUES (?,?,?,?,?)",
                       ("o/r:review:prior", "o/r:review:test", "o/r", "review", "2026-09-28T00:00:00+00:00"))
        else:
            namespace = ("review-round-submission" if change == "settlement"
                         else "review-execution-generation")
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (namespace, "o/r", "changed-source", "2026-09-28T00:00:00+00:00", "{}"))
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
            assert isinstance(result, BlockedLegacyExecutionSqlImportSources)


def test_sql_import_sources_require_one_repository_and_an_explicit_transaction(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with pytest.raises(ValueError, match="same-repository"):
        replace(observed, generation=CompleteLegacyReviewGenerationReceipts("other/repo", ()))
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control._connect() as db:
            with pytest.raises(ValueError, match="explicit SQL transaction"):
                verify_legacy_execution_import_sources(
                    db, held, observed, writer=WRITER, configured_prior_owners=(),
                )


def test_sql_import_sources_reject_expired_barrier_lease(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
    with control.transaction() as db:
        result = verify_legacy_execution_import_sources(
            db, held, observed, writer=WRITER, configured_prior_owners=(),
        )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "barriers" in result.reason
    with pytest.raises(ValueError, match="verified same-repository"):
        RevalidatedLegacyExecutionImportSources(held, observed, None, object())


def test_sql_import_sources_reject_forged_counter_and_result_evidence(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        forged = HeldLegacyExecutionCounterSources(
            held.roots, held.mapping, held.review_state_dir, held.other_role_state_dirs, held._scope,
        )
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, forged, observed, writer=WRITER, configured_prior_owners=(),
            )
        assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
        with pytest.raises(ValueError, match="verified same-repository"):
            RevalidatedLegacyExecutionImportSources(held, observed, None, object())


def test_sql_import_sources_do_not_reactivate_an_escaped_scope(sources):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as old:
        assert isinstance(old, HeldLegacyExecutionCounterSources)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as current:
        assert isinstance(current, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, old, observed, writer=WRITER, configured_prior_owners=(),
            )
        assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
        assert "barriers" in result.reason


def test_sql_import_sources_reject_a_different_control_database(sources, tmp_path):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    other = SQLiteControlStore(str(tmp_path / "other-control.db"))
    other.initialize()
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with other.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=WRITER, configured_prior_owners=(),
            )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "store differs" in result.reason


@pytest.mark.parametrize("writer,priors", (
    (WorkflowWriter("o/r", "review", "o/r:review:another"), ()),
    (WRITER, ("o/r:review:prior",)),
    (WorkflowWriter("another/repo", "review", "another/repo:review:test"), ()),
))
def test_sql_import_sources_reject_owner_configuration_mismatch(sources, writer, priors):
    control, gateway, roots, files, links = sources
    observed = _observed_sql_sources(control)
    with held_legacy_execution_counter_sources(control, gateway, "o/r", roots, files, links, review_state_dir=roots[0][1]) as held:
        assert isinstance(held, HeldLegacyExecutionCounterSources)
        with control.transaction() as db:
            result = verify_legacy_execution_import_sources(
                db, held, observed, writer=writer, configured_prior_owners=priors,
            )
    assert isinstance(result, BlockedLegacyExecutionSqlImportSources)
    assert "configuration" in result.reason
