"""Offline continuation publication through the existing configured CLI."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters.workflow_rows import load_issue_aggregate, load_review_workflow_row
from issue_agent.adapters.continuation_activation import read_continuation_roots, publish_continuation_import, verify_original_plan
from issue_agent.app import cli, continuation_activation
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.workflow import IssueWorkflowShell, WorkflowMigrationRequired
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowFamily, WorkflowKey
from tests.app.test_resolve_activation_cli import deployment, dump, workflow
from tests.fakes.review_workflow import held
from tests.fakes.workflow_authority import install_workflow


class EmptyRemote:
    def _gh_json(self, args):
        return [[]]


@pytest.fixture
def stopped(deployment, monkeypatch):
    original = continuation_activation.activation_scope
    def selected(*args, **kwargs):
        return replace(original(*args, **kwargs), gateway=EmptyRemote())
    monkeypatch.setattr(continuation_activation, "activation_scope", selected)
    return deployment


def command(capsys, family, *argv):
    args = cli.build_parser().parse_args([family + "-activation", *argv, "--name", "target"])
    code = args.func(args)
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out.strip() else None, output.err


def test_ci_remediation_shadow_and_copied_activation_keep_exact_imported_hints(stopped, capsys, tmp_path):
    from issue_agent.adapters.control_store import SQLiteControlStore
    state, control, _ = stopped
    install_workflow(control, held())
    (state / "pr-9.ci-rerun-head").write_text("head-1")
    (state / "pr-9.ci-cancel-head").write_text("head-1@2026-10-01T00:00:00Z")
    (state / "pr-9.update-branch-head").write_text("head-1:base-1")
    (state / "pr-9.ci-pending-escalated-head").write_text("head-1")
    before = dump(control)
    files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert [(item["family"], item["number"], item["eligible"]) for item in report["targets"]] == [("pr_repair", 9, True)]
    copy = tmp_path / "ci-remediation-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copy))
    assert code == 0 and all(result["checks"].values()), error
    copied = SQLiteControlStore(str(copy / "issue-agent-control" / Path(control.path).name))
    selected = copied.read_pr_repair(WorkflowKey("o/r", PullRequestRef(9)))
    rerun, cancel = selected.component.state.ci_rerun, selected.component.state.ci_cancel
    assert isinstance(rerun, cc.ImportedCIRemediation) and isinstance(cancel, cc.ImportedCIRemediation)
    assert (rerun.head, cancel.head, cancel.decided_at) == ("head-1", "head-1", "2026-10-01T00:00:00Z")
    assert rerun.import_identity != cancel.import_identity
    refresh = selected.component.state.branch_refresh
    escalation = selected.component.state.pending_ci_escalation
    assert isinstance(refresh, cc.ImportedBranchRefresh) and isinstance(escalation, cc.ImportedPendingCIEscalation)
    assert (refresh.head, refresh.base, escalation.head) == ("head-1", "base-1", "head-1")
    assert refresh.import_identity != escalation.import_identity
    assert dump(control) == before
    assert {path.name: path.read_bytes() for path in state.iterdir()} == files


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("executing", [False, True])
def test_legacy_unsettled_ci_command_refuses_hint_import_before_publication(stopped, capsys, cancel, executing):
    from issue_agent.adapters.effects import command_for_action
    from issue_agent.domain.actions import CancelWorkflowRuns, RerunFailedChecks
    state, control, _ = stopped
    install_workflow(control, held())
    suffix = "ci-cancel-head" if cancel else "ci-rerun-head"
    hint = "head-1@2026-10-01T00:00:00Z" if cancel else "head-1"
    (state / ("pr-9." + suffix)).write_text(hint)
    action = CancelWorkflowRuns(9, "head-1", (42,)) if cancel else RerunFailedChecks(9, "head-1", (42,))
    legacy_command = command_for_action(action, repository="o/r", executor_identity="o/r:fix")
    assert "ci_remediation" not in legacy_command.payload
    assert control.enqueue_command(legacy_command)
    if executing:
        control.begin_command(legacy_command.command_id)
    before = dump(control)
    code, _, error = command(capsys, "fix", "shadow")
    assert code == 2 and "pending work" in error
    assert dump(control) == before
    assert (state / ("pr-9." + suffix)).read_text() == hint


def test_ci_shadow_and_copied_activation_preserve_debt_and_unknown_hold(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / "issue-7.ci-retries").write_text("3")
    (state / "issue-8.ci-retries").write_text("malformed")
    original_sql = dump(control)
    original_files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert report["ci_shadow"] == [{"issue": 7, "legacy": 3, "imported": 3, "agrees": True}]
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copied = tmp_path / "ci-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
    assert code == 0 and all(result["checks"].values()), error
    with closing(sqlite3.connect(copied / "issue-agent-control" / Path(control.path).name)) as db:
        db.row_factory = sqlite3.Row
        imported = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7)))
        blocked = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8)))
    assert imported.issue_recovery.state.ci_failures == 3
    assert isinstance(blocked.issue_recovery, cc.BlockedIssueRecovery)
    assert dump(control) == original_sql
    assert {path.name: path.read_bytes() for path in state.iterdir()} == original_files


@pytest.mark.parametrize("advisory", ["ordinary", "malformed"])
def test_copied_cutover_keeps_advisory_context_without_importing_authority(stopped, capsys, tmp_path, advisory):
    from issue_agent.adapters.control_store import SQLiteControlStore
    state, control, _ = stopped
    (state / "issue-7.ci-retries").write_text("2")
    (state / "issue-8.ci-retries").write_text("unknown")
    history = '[{"head":"head-1","failed_checks":["tests"]}]' if advisory == "ordinary" else "not-json"
    marker = '{"sig":"observed-state","since":"2026-10-01T00:00:00Z"}' if advisory == "ordinary" else "[]"
    for number in (7, 8):
        (state / f"issue-{number}.ci-history").write_text(history)
        (state / f"issue-{number}.stall-sig").write_text(marker)
    before = dump(control)
    originals = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copy = tmp_path / "advisory-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copy))
    assert code == 0 and all(result["checks"].values()), error
    copied = SQLiteControlStore(str(copy / "issue-agent-control" / Path(control.path).name))
    selected = copied.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    blocked = copied.read_issue_recovery(WorkflowKey("o/r", IssueRef(8)))
    assert selected.component.state.ci_failures == 2
    assert selected.component.state.self_clears == selected.component.state.resumes == 0
    assert isinstance(blocked, cc.BlockedIssueRecovery)
    copied_state = next(path.parent for path in (copy / "state").glob("*/issue-7.ci-history"))
    assert {path.name: path.read_bytes() for path in copied_state.iterdir()} == originals
    assert dump(control) == before
    assert {path.name: path.read_bytes() for path in state.iterdir()} == originals


@pytest.mark.parametrize("numbers", [(9,), (9, 10), ()])
def test_review_shadow_retains_episode_and_requires_unique_current_linkage(stopped, capsys, tmp_path, monkeypatch, numbers):
    from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState
    from issue_agent.domain.world import PRView
    state, control, _ = stopped
    (state / "issue-7.review-retries").write_text("2026-10-01T00:00:00Z 2")
    for number in numbers:
        primary = held(key=WorkflowKey("o/r", PullRequestRef(number)))
        from issue_agent.domain.workflow import OpenGate
        gate = primary.gates[0]
        assert isinstance(gate, OpenGate)
        primary = replace(primary, gates=(replace(gate, gate_id=f"gate:review:{number}"),))
        install_workflow(control, primary)
    class LinkedRemote:
        def _gh_json(self, args):
            return [[{"number": number, "state": "open"} for number in numbers]]
    original = continuation_activation.activation_scope
    monkeypatch.setattr(continuation_activation, "activation_scope", lambda *args, **kwargs: replace(original(*args, **kwargs), gateway=LinkedRemote()))
    def read(gateway, key, now, **kwargs):
        view = PRView(key.target.number, f"fix/7-pr-{key.target.number}", "a"*40, closing_issue=7)
        return ObservedPullRequest(key, "a"*40, PullRequestState.OPEN, frozenset(), CompleteReviews(()), now), view
    monkeypatch.setattr(continuation_activation, "read_pull_request", read)
    before = dump(control)
    source = (state / "issue-7.review-retries").read_bytes()
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    target = next(t for t in report["targets"] if t["family"] == "issue_recovery")
    assert target["eligible"] == (len(numbers) == 1)
    if len(numbers) == 1:
        assert report["review_shadow"] == [{"issue": 7, "legacy": ["2026-10-01T00:00:00Z", 2], "imported": ["2026-10-01T00:00:00Z", 2], "agrees": True}]
        copied = tmp_path / "review-copy"
        code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
        assert code == 0 and all(result["checks"].values()), error
        with closing(sqlite3.connect(copied / "issue-agent-control" / Path(control.path).name)) as db:
            db.row_factory = sqlite3.Row
            imported = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7)))
        assert imported.issue_recovery.state.review_failures == cc.ReviewRetryState("2026-10-01T00:00:00Z", 2, PullRequestRef(9))
    else:
        assert target["blockers"] and report["review_shadow"] == []
    assert dump(control) == before
    assert (state / "issue-7.review-retries").read_bytes() == source


def test_pre_f4_literal_ci_plan_keeps_sql_retry_after_source_cleanup(stopped, capsys, monkeypatch):
    from issue_agent.adapters.continuation_activation import read_import_plan
    state, control, _ = stopped
    saved = Path(__file__).parents[1] / "fixtures" / "workflow_fix_import_pre_f4.json"
    report = json.loads(saved.read_text())
    original = read_import_plan(str(saved))
    assert original.plan_digest == report["plan_digest"]
    with control.transaction() as db:
        publish_continuation_import(db, original, now=datetime(2026, 10, 1, tzinfo=timezone.utc), control=control)
        selected = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7)))
    assert selected.issue_recovery.state.ci_failures == 3 and selected.issue_recovery.state.pending_ci is None
    (state / "issue-7.ci-retries").write_text("malformed retained edit")
    (state / "issue-7.ci-retries").unlink()
    before = dump(control)
    monkeypatch.setattr(continuation_activation, "evaluate", lambda *args: pytest.fail("retry inspected raw source"))
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: pytest.fail("retry inspected current work"))
    code, retry, error = command(capsys, "fix", "activate", "--plan-file", str(saved),
                                "--confirm-plan-digest", report["plan_digest"])
    assert code == 0 and retry["status"] == "already_active_with_this_plan", error
    assert dump(control) == before


def test_mixed_fix_issue_pr_publication_and_inert_sql_only_retry(stopped, capsys, tmp_path, monkeypatch):
    state, control, env = stopped
    install_workflow(control, held())
    (state / "issue-7.resumes").write_text("2")
    (state / "issue-8.session").write_text("codex:ambiguous")
    (state / "pr-9.resumed-at").write_text("2026-10-01T00:00:00Z")
    before = dump(control)
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert {(t["family"], t["number"], t["eligible"]) for t in report["targets"]} == {
        ("issue_recovery", 7, True), ("issue_recovery", 8, False), ("pr_repair", 9, True)}
    assert dump(control) == before
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(report))
    code, accepted, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", report["plan_digest"])
    assert code == 0, error
    assert accepted["status"] == "activated"
    with control._connect() as db:
        db.execute("BEGIN")
        issue = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7)))
        blocked = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8)))
        pr = load_review_workflow_row(db, WorkflowKey("o/r", PullRequestRef(9)))
    assert isinstance(issue, IssueWorkflowShell) and issue.issue_recovery.state.resumes == 2
    assert isinstance(blocked, IssueWorkflowShell) and isinstance(blocked.issue_recovery, cc.BlockedIssueRecovery)
    assert pr.pr_repair.state.consumed_reply_at == "2026-10-01T00:00:00Z"
    assert isinstance(workflow(control, env, 7), WorkflowMigrationRequired)
    from tests.adapters.test_continuation_admission import paid, purchase
    from issue_agent.adapters.store import Store
    selected = purchase(issue, reply="2026-10-02T00:00:00Z", ceiling=5)
    assert control.reserve_issue_recovery(paid("later", selected), selected).admitted
    Store(str(state))._clear_issue_advisory(8)
    assert (state / "issue-8.session").read_text() == "codex:ambiguous"
    with control._connect() as db:
        assert isinstance(load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8))).issue_recovery, cc.BlockedIssueRecovery)
    after = dump(control)
    monkeypatch.setattr(continuation_activation, "evaluate", lambda *args: pytest.fail("retry reread source corpus"))
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: pytest.fail("original retry checked live work"))
    code, retry, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", report["plan_digest"])
    assert code == 0 and retry["status"] == "already_active_with_this_plan", error
    assert dump(control) == after


def test_missing_pr_primary_refuses_before_any_publication(stopped, capsys):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    (state / "pr-9.resumed-at").write_text("2026-10-01T00:00:00Z")
    before = dump(control)
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 2 and report is None and "canonical rejection workflow prerequisite" in error
    assert dump(control) == before


def test_steward_rehearsal_leaves_source_and_primary_authority_unchanged(stopped, capsys, tmp_path):
    _, control, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True)
    (state / "triage-7.at").write_text("2026-10-01T00:00:00Z")
    (state / "groom-7.at").write_text("2026-10-01T00:00:00Z")
    (state / "groom-7.content").write_text("a" * 64)
    (state / "issue-8.steward-stalls-groom").write_text("malformed")
    (state / "events-v2.jsonl").write_text("retained source feed\n")
    before = dump(control)
    originals = {p.name: p.read_bytes() for p in state.iterdir()}
    code, result, error = command(capsys, "steward", "rehearse", "--copy-to", str(tmp_path / "copy"))
    assert code == 0, error
    assert result["status"] == "rehearsed" and all(result["checks"].values())
    assert {(t["number"], t["eligible"]) for t in result["targets"]} == {(7, True), (8, False)}
    assert dump(control) == before
    assert {p.name: p.read_bytes() for p in state.iterdir()} == originals
    with closing(sqlite3.connect(tmp_path / "copy" / "issue-agent-control" / Path(control.path).name)) as db:
        assert db.execute("SELECT count(*) FROM workflow_components WHERE family='resolve'").fetchone()[0] == 0


@pytest.mark.parametrize("value", ["", "-1", "1" * 1000, "1" * 70000])
def test_malformed_or_huge_field_blocks_only_its_selected_target(stopped, capsys, value):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("2")
    (state / "issue-8.resumes").write_text(value)
    before = dump(control)
    code, result, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert [(t["number"], t["eligible"]) for t in result["targets"]] == [(7, True), (8, False)]
    assert result["targets"][1]["blockers"]
    assert dump(control) == before


def test_duplicate_root_alias_refuses_scope_without_writes(stopped, capsys):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    before = dump(control)
    code, _, error = command(capsys, "fix", "shadow", "--legacy-state-dir", str(state))
    assert code == 2 and "duplicate" in error
    assert dump(control) == before


def test_duplicate_target_owner_is_retained_block_while_sibling_is_eligible(stopped, capsys, tmp_path):
    state, control, _ = stopped
    older = tmp_path / "older"
    older.mkdir()
    (state / "issue-7.resumes").write_text("1")
    (older / "issue-7.resumes").write_text("1")
    (state / "issue-8.resumes").write_text("2")
    before = dump(control)
    code, result, error = command(capsys, "fix", "shadow", "--legacy-state-dir", str(older))
    assert code == 0, error
    assert [(t["number"], t["eligible"]) for t in result["targets"]] == [(7, False), (8, True)]
    assert "owner" in result["targets"][0]["blockers"][0]
    assert dump(control) == before


def test_changed_source_and_changed_confirmed_digest_refuse_before_writes(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    code, original, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(original))
    before = dump(control)
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", "f" * 64)
    assert code == 2 and "digest" in error
    (state / "issue-7.resumes").write_text("2")
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])
    assert code == 2 and "sources changed" in error
    assert dump(control) == before


def test_pending_paid_work_and_live_writer_refuse_first_publication(stopped, capsys, tmp_path, monkeypatch):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    code, original, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(original))
    before = dump(control)
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: ["fix heartbeat live"])
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])
    assert code == 2 and "watchers may be running" in error
    assert dump(control) == before
    from tests.adapters.test_workflow_admission import request
    assert control.reserve(replace(request("pending"), workflow=None)).admitted
    pending = dump(control)
    code, _, error = command(capsys, "fix", "shadow")
    assert code == 2 and "pending work" in error
    assert dump(control) == pending


def test_caught_second_component_failure_rolls_back_all_publication(stopped, monkeypatch):
    from issue_agent.adapters import continuation_activation as publication
    state, control, env = stopped
    (state / "issue-7.resumes").write_text("1")
    (state / "issue-8.resumes").write_text("2")
    scope = continuation_activation.activation_scope("target", harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    original = publication.insert_workflow_component
    def second_fails(db, key, authority, episode):
        if key.target.number == 8:
            raise ValueError("second component failure")
        return original(db, key, authority, episode)
    monkeypatch.setattr(publication, "insert_workflow_component", second_fails)
    with control.transaction() as db:
        with pytest.raises(ValueError, match="second component failure"):
            publication.publish_continuation_import(db, plan, now=datetime.now(timezone.utc), control=control)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at) VALUES ('test','o/r','outer-committed','now')")
    with control._connect() as db:
        assert db.execute("SELECT count(*) FROM workflow_authority_epochs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workflow_activation_imports").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workflow_components").fetchone()[0] == 0
        assert db.execute("SELECT key FROM dedupe_keys").fetchone()[0] == "outer-committed"


@pytest.mark.parametrize("closing,branches,legacy_session", [(7, ("fix/8-other",), False), (7, ("fix/7-one", "fix/7-two"), False), (7, ("fix/7-one",), True)])
def test_ambiguous_mapping_or_unsupported_handoff_blocks_affected_pr(stopped, capsys, monkeypatch, closing, branches, legacy_session):
    from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState
    from issue_agent.domain.world import PRView
    state, control, _ = stopped
    numbers = tuple(9 + i for i in range(len(branches)))
    for number in numbers:
        primary = held(key=WorkflowKey("o/r", PullRequestRef(number)))
        from issue_agent.domain.workflow import OpenGate
        gate = primary.gates[0]
        assert isinstance(gate, OpenGate)
        primary = replace(primary, gates=(replace(gate, gate_id=f"gate:review:{number}"),))
        install_workflow(control, primary)
    if legacy_session:
        (state / "issue-7.session").write_text("codex:unknown-origin")
    class LinkedRemote:
        def _gh_json(self, args):
            return [[{"number": number, "state": "open"} for number in numbers]]
    original = continuation_activation.activation_scope
    monkeypatch.setattr(continuation_activation, "activation_scope", lambda *args, **kwargs: replace(original(*args, **kwargs), gateway=LinkedRemote()))
    def read(gateway, key, now, **kwargs):
        view = PRView(key.target.number, branches[numbers.index(key.target.number)], "a" * 40, closing_issue=closing)
        observed = ObservedPullRequest(key, "a" * 40, PullRequestState.OPEN, frozenset(), CompleteReviews(()), now)
        return observed, view
    monkeypatch.setattr(continuation_activation, "read_pull_request", read)
    before = dump(control)
    code, result, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    prs = [target for target in result["targets"] if target["family"] == "pr_repair"]
    assert len(prs) == len(numbers) and all(not target["eligible"] and target["blockers"] for target in prs)
    assert dump(control) == before


def test_rehearsal_includes_committed_wal_without_changing_source(stopped, capsys, tmp_path):
    _, control, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True)
    with closing(sqlite3.connect(control.path)) as retained:
        retained.execute("PRAGMA wal_autocheckpoint=0")
        retained.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at) VALUES ('test','o/r','in-wal','now')")
        retained.commit()
        main = Path(control.path).read_bytes()
        wal = Path(control.path + "-wal").read_bytes()
        code, result, error = command(capsys, "steward", "rehearse", "--copy-to", str(tmp_path / "wal-copy"))
        assert code == 0 and result["status"] == "rehearsed", error
        assert Path(control.path).read_bytes() == main
        assert Path(control.path + "-wal").read_bytes() == wal
        with closing(sqlite3.connect(tmp_path / "wal-copy" / "issue-agent-control" / Path(control.path).name)) as copy:
            assert copy.execute("SELECT key FROM dedupe_keys WHERE namespace='test'").fetchone()[0] == "in-wal"


def test_same_revision_authority_change_refuses_selected_publication(stopped):
    from issue_agent.adapters.continuation_activation import publish_continuation_import
    from issue_agent.adapters.resolve_activation import ResolveActivationRefused
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
    from tests.fakes.workflow_authority import resolve_snapshot
    state, control, _ = stopped
    primary = resolve_snapshot(number=7)
    install_workflow(control, primary)
    (state / "issue-7.resumes").write_text("1")
    scope = continuation_activation.activation_scope("target", harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    changed = replace(primary, episode=replace(primary.episode, failures=2))
    with control.transaction() as db:
        persist_workflow_snapshot(db, changed)
    before = dump(control)
    with control.transaction() as db:
        with pytest.raises(ResolveActivationRefused, match="authority content changed"):
            publish_continuation_import(db, plan, now=datetime.now(timezone.utc), control=control)
    assert dump(control) == before


def test_local_source_change_after_remote_recheck_refuses_publication(stopped, capsys, tmp_path, monkeypatch):
    state, control, _ = stopped
    source = state / "issue-7.resumes"
    source.write_text("1")
    code, original, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(original))
    before = dump(control)
    evaluate = continuation_activation.evaluate
    def changed_after_read(*args):
        plan = evaluate(*args)
        source.write_text("2")
        return plan
    monkeypatch.setattr(continuation_activation, "evaluate", changed_after_read)
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])
    assert code == 2 and "sources changed before publication" in error
    assert dump(control) == before


def test_deleted_selected_decision_refuses_retry(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    code, original, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(original))
    assert command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])[0] == 0
    with control.transaction() as db:
        db.execute("DELETE FROM workflow_activation_imports WHERE target_number=7")
    before = dump(control)
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])
    assert code == 2 and "selected decision" in error
    assert dump(control) == before


def test_changed_original_plan_cannot_reuse_accepted_source_digests(stopped, capsys, tmp_path):
    from issue_agent.adapters.continuation_activation import read_import_plan
    state, control, _ = stopped
    (state / "issue-7.resumes").write_text("1")
    code, original, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    saved = tmp_path / "original.json"
    saved.write_text(json.dumps(original))
    assert command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", original["plan_digest"])[0] == 0
    plan = read_import_plan(str(saved))
    changed = replace(plan, targets=(replace(plan.targets[0], state=cc.IssueRecoveryState(resumes=9)),))
    saved.write_text(json.dumps(continuation_activation.report(changed)))
    before = dump(control)
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", changed.plan_digest)
    assert code == 2 and "original plan conflicts" in error
    assert dump(control) == before


@pytest.mark.parametrize("bad", [None, "null", "{}", "x" * (1024 * 1024)])
def test_malformed_or_huge_saved_plan_refuses_without_writes(stopped, capsys, tmp_path, bad):
    _, control, _ = stopped
    saved = tmp_path / "malformed.json"
    saved.write_text(json.dumps({"original_plan": bad}))
    before = dump(control)
    code, _, error = command(capsys, "fix", "activate", "--plan-file", str(saved), "--confirm-plan-digest", "f" * 64)
    assert code == 2 and "activation refused" in error
    assert dump(control) == before


def test_noncanonical_selected_sql_owner_is_not_treated_as_absence(stopped, capsys):
    state, control, _ = stopped
    with control.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (' O/R ','issue',7,0,0,'{}')")
    (state / "issue-7.resumes").write_text("1")
    before = dump(control)
    code, _, error = command(capsys, "fix", "shadow")
    assert code == 2 and "noncanonical source ownership" in error
    assert dump(control) == before


def test_alias_fields_block(stopped):
    state, control, _ = stopped
    (state / 'issue--9.infra-deaths').write_text('6')
    (state / 'pr-9.infra-deaths').write_text('0')
    records, digest = read_continuation_roots('o/r', (('fix', str(state)),), (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR))
    from issue_agent.domain.continuation_import import classify_record
    value = classify_record(records[0])
    assert isinstance(value, tuple), 'contradictory two-source field must block, not choose last entry'


def test_retry_rejects_missing_selected_component_index(stopped):
    state, control, _ = stopped
    (state / 'issue-7.resumes').write_text('3')
    scope = continuation_activation.activation_scope('target', harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    with control.transaction() as db:
        publish_continuation_import(db, plan, control=control, now=datetime.now(timezone.utc))
    with control.transaction() as db:
        db.execute("DELETE FROM workflow_components WHERE repository='o/r' AND target_number=7 AND family='issue_recovery'")
        with pytest.raises(ValueError):
            verify_original_plan(db, plan)


def test_review_root_conflicting_fix_fact_blocks(stopped):
    from pathlib import Path
    state, control, env = stopped
    review = Path(env['REVIEW_STATE_DIR'])
    review.mkdir()
    (state / 'issue-7.resumes').write_text('2')
    (review / 'issue-7.resumes').write_text('garbage')
    scope = continuation_activation.activation_scope('target', harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    assert plan.targets[0].blockers, 'fix-only field in another owner root must not disappear'


def test_alias_discarded_source_changes_are_observed(stopped):
    state, _, _ = stopped
    first = state / 'issue--9.infra-deaths'
    second = state / 'pr-9.infra-deaths'
    first.write_text('6')
    second.write_text('0')
    roots = (('fix', str(state)),)
    families = (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR)
    _, before = read_continuation_roots('o/r', roots, families)
    first.write_text('7')
    _, first_changed = read_continuation_roots('o/r', roots, families)
    first.write_text('6')
    second.write_text('1')
    _, second_changed = read_continuation_roots('o/r', roots, families)
    assert first_changed != before and second_changed != before


def test_public_activation_publishes_outbox(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / 'issue-7.resumes').write_text('1')
    code, report, _ = command(capsys, 'fix', 'shadow')
    saved = tmp_path / 'original.json'
    saved.write_text(json.dumps(report))
    code, result, err = command(capsys, 'fix', 'activate', '--plan-file', str(saved), '--confirm-plan-digest', report['plan_digest'])
    assert code == 0, err
    with control._connect() as db:
        outbox = db.execute('SELECT count(*) FROM event_outbox').fetchone()[0]
    assert outbox == 1
    before = dump(control)
    code, retry, err = command(capsys, 'fix', 'activate', '--plan-file', str(saved), '--confirm-plan-digest', report['plan_digest'])
    assert code == 0 and retry['status'] == 'already_active_with_this_plan', err
    assert dump(control) == before


def test_liveness_does_not_run_under_writer_lock(stopped, tmp_path):
    state, control, _ = stopped
    (state / 'issue-7.resumes').write_text('1')
    scope = continuation_activation.activation_scope('target', harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    saved = tmp_path / 'original.json'
    saved.write_text(json.dumps(continuation_activation.report(plan)))
    locked = []
    def liveness():
        with sqlite3.connect(control.path, timeout=0) as probe:
            try:
                probe.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                locked.append(str(error))
            else:
                probe.rollback()
        return ()
    continuation_activation.activate(scope, confirm_plan_digest=plan.plan_digest, plan_file=str(saved), liveness=liveness)
    assert not locked


def test_same_plan_winner_after_initial_check_is_exact_retry(stopped, tmp_path, monkeypatch):
    from contextlib import contextmanager
    import json
    state, control, _ = stopped
    (state / 'issue-7.resumes').write_text('1')
    scope = continuation_activation.activation_scope('target', harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    saved = tmp_path / 'original.json'
    saved.write_text(json.dumps(continuation_activation.report(plan)))
    first = True
    @contextmanager
    def barrier(*args):
        nonlocal first
        if first:
            first = False
            with control.transaction() as db:
                publish_continuation_import(db, plan, control=control, now=datetime.now(timezone.utc))
        yield
    monkeypatch.setattr(continuation_activation, 'deployment_effect_barrier', barrier)
    result = continuation_activation.activate(scope, confirm_plan_digest=plan.plan_digest, plan_file=str(saved), liveness=lambda: ())
    assert result['status'] == 'already_active_with_this_plan'


def test_retry_wrong_target_payload_is_typed_refusal(stopped):
    state, control, _ = stopped
    install_workflow(control, held())
    (state / 'issue-7.resumes').write_text('3')
    scope = continuation_activation.activation_scope('target', harness_root=cli.HARNESS_ROOT)
    plan = continuation_activation.evaluate(scope, control)
    with control.transaction() as db:
        publish_continuation_import(db, plan, control=control, now=datetime.now(timezone.utc))
    with control.transaction() as db:
        payload = db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr' AND target_number=9").fetchone()[0]
        db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='issue' AND target_number=7", (payload,))
        from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
        with pytest.raises((WorkflowAuthorityError, ValueError)):
            verify_original_plan(db, plan)


@pytest.mark.parametrize('failure', ['factory', 'outbox'])
def test_event_failure_rolls_back_publication(stopped, capsys, tmp_path, monkeypatch, failure):
    from issue_agent.adapters.event_log import EventLog
    state, control, _ = stopped
    (state / 'issue-7.resumes').write_text('1')
    _, report, _ = command(capsys, 'fix', 'shadow')
    saved = tmp_path / 'original.json'
    saved.write_text(json.dumps(report))
    if failure == 'factory':
        def refused(*args, **kwargs):
            raise ValueError('factory unavailable')
        monkeypatch.setattr(EventLog, 'prepare', refused)
    else:
        with control.transaction() as db:
            db.execute("CREATE TRIGGER refuse_event BEFORE INSERT ON event_outbox BEGIN SELECT RAISE(ABORT,'event rejected'); END")
    before = dump(control)
    code, result, err = command(capsys, 'fix', 'activate', '--plan-file', str(saved), '--confirm-plan-digest', report['plan_digest'])
    assert code == 2, (result, err)
    assert dump(control) == before



def test_issue_reply_shadow_preserves_timestamp_count_and_owned_branch(stopped, capsys):
    state, control, _ = stopped
    (state / "issue-7.resumed-at").write_text("2026-10-01T00:00:00Z\n")
    (state / "issue-7.resumes").write_text("4")
    (state / "issue-7.branch").write_text("fix/7-reply")
    (state / "issue-8.resumed-at").write_text("ambiguous")
    before = dump(control)
    code, result, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert result["issue_reply_shadow"] == [{"issue": 7, "legacy": ["2026-10-01T00:00:00Z", 4, "fix/7-reply"], "imported": ["2026-10-01T00:00:00Z", 4, "fix/7-reply"], "agrees": True}]
    assert next(target for target in result["targets"] if target["number"] == 8)["eligible"] is False
    assert dump(control) == before


def test_triage_shadow_preserves_baseline_and_stalls_and_blocks_unknown_history(stopped, capsys):
    _, control, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "triage-7.at").write_text("2026-10-01T00:00:00Z\n")
    (state / "issue-7.steward-stalls-triage").write_text("2")
    (state / "triage-8.at").write_text("unknown")
    before = dump(control)
    code, result, error = command(capsys, "steward", "shadow")
    assert code == 0, error
    assert result["triage_shadow"] == [{"issue":7, "legacy":["2026-10-01T00:00:00Z",2],
                                       "imported":["2026-10-01T00:00:00Z",2], "agrees":True}]
    assert next(target for target in result["targets"] if target["number"] == 8)["eligible"] is False
    assert dump(control) == before


def test_groom_shadow_matches_supported_content_and_blocks_ambiguous_source(stopped, capsys):
    _, control, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "groom-7.at").write_text("2026-10-01T00:00:00Z\n")
    (state / "groom-7.content").write_text("a" * 64 + "\n")
    (state / "issue-7.steward-stalls-groom").write_text("2")
    (state / "groom-8.content").write_text("a" * 64)
    before = dump(control)
    code, result, error = command(capsys, "steward", "shadow")
    assert code == 0, error
    expected = ["2026-10-01T00:00:00Z", "a" * 64, 2]
    assert result["groom_shadow"] == [{"issue":7, "legacy":expected, "imported":expected, "agrees":True}]
    assert next(target for target in result["targets"] if target["number"] == 8)["eligible"] is False
    assert dump(control) == before


def test_oversized_groom_file_stays_visibly_blocked(stopped, capsys):
    _, control, env = stopped
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "groom-7.at").write_text("2026-10-01T00:00:00Z")
    (state / "groom-7.content").write_text("a" * 65537)
    before = dump(control)
    code, result, error = command(capsys, "steward", "shadow")
    assert code == 0, error
    target = next(target for target in result["targets"] if target["number"] == 7)
    assert target["eligible"] is False
    assert any("groom-content" in blocker for blocker in target["blockers"])
    assert result["groom_shadow"] == [] and dump(control) == before


def test_pre_t2_literal_plan_retains_digest_and_sql_only_retry_after_cleanup(stopped, capsys, monkeypatch):
    from issue_agent.adapters.continuation_activation import read_import_plan
    _, control, env = stopped
    saved = Path(__file__).parents[1] / "fixtures" / "workflow_steward_import_pre_t2.json"
    report = json.loads(saved.read_text())
    original = read_import_plan(str(saved))
    assert original.plan_digest == report["plan_digest"]
    with control.transaction() as db:
        publish_continuation_import(db, original, now=datetime(2026,10,1,tzinfo=timezone.utc), control=control)
    state = Path(env["STEWARD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "triage-7.at").write_text("2026-10-01T00:00:00Z")
    (state / "triage-7.at").unlink()
    before = dump(control)
    monkeypatch.setattr(continuation_activation, "evaluate", lambda *args: pytest.fail("retry inspected raw source"))
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: pytest.fail("retry inspected current work"))
    code, retry, error = command(capsys, "steward", "activate", "--plan-file", str(saved),
                                 "--confirm-plan-digest", report["plan_digest"])
    assert code == 0 and retry["status"] == "already_active_with_this_plan", error
    assert dump(control) == before


def test_pre_f6_literal_rebase_plan_keeps_sql_retry_after_source_cleanup(stopped, capsys, monkeypatch):
    from issue_agent.adapters.continuation_activation import read_import_plan
    state, control, _ = stopped
    saved = Path(__file__).parents[1] / "fixtures" / "workflow_fix_import_pre_f6.json"
    report = json.loads(saved.read_text())
    original = read_import_plan(str(saved))
    assert original.plan_digest == report["plan_digest"]
    with control.transaction() as db:
        publish_continuation_import(db, original, now=datetime(2026, 10, 1, tzinfo=timezone.utc), control=control)
        selected = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7)))
    assert selected.issue_recovery.state.rebase_failures == 4 and selected.issue_recovery.state.pending_rebase is None
    (state / "issue-7.rebase-retries").write_text("malformed retained edit")
    (state / "issue-7.rebase-retries").unlink()
    before = dump(control)
    monkeypatch.setattr(continuation_activation, "evaluate", lambda *args: pytest.fail("retry inspected raw source"))
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: pytest.fail("retry inspected current work"))
    code, retry, error = command(capsys, "fix", "activate", "--plan-file", str(saved),
                                "--confirm-plan-digest", report["plan_digest"])
    assert code == 0 and retry["status"] == "already_active_with_this_plan", error
    assert dump(control) == before



def test_pre_f2_literal_pr_plan_keeps_wire_and_sql_retry_after_raw_cleanup(stopped, capsys, monkeypatch):
    from issue_agent.adapters.continuation_activation import read_import_plan
    from issue_agent.adapters.workflow_codec import encode_workflow
    state, control, _ = stopped
    saved = Path(__file__).parents[1] / "fixtures" / "workflow_fix_import_pre_f2.json"
    report = json.loads(saved.read_text())
    original = read_import_plan(str(saved))
    assert original.plan_digest == report["plan_digest"]
    assert encode_workflow(original) == report["original_plan"]
    install_workflow(control, held(key=WorkflowKey("o/r", PullRequestRef(100))))
    with control.transaction() as db:
        publish_continuation_import(db, original, now=datetime(2026, 10, 2, tzinfo=timezone.utc), control=control)
        selected = load_review_workflow_row(db, WorkflowKey("o/r", PullRequestRef(100)))
    assert selected.pr_repair.state.consumed_reply_at == "2026-10-01T00:00:00Z" and selected.pr_repair.state.blank_reply is None
    (state / "pr-100.resumed-at").write_text("malformed retained edit")
    (state / "pr-100.resumed-at").unlink()
    before = dump(control)
    monkeypatch.setattr(continuation_activation, "evaluate", lambda *a: pytest.fail("retry inspected raw source"))
    monkeypatch.setattr(cli, "_activation_liveness", lambda *a: pytest.fail("retry inspected current work"))
    code, retry, error = command(capsys, "fix", "activate", "--plan-file", str(saved),
                                "--confirm-plan-digest", report["plan_digest"])
    assert code == 0 and retry["status"] == "already_active_with_this_plan", error
    assert dump(control) == before


def test_rebase_shadow_and_copy_preserve_debt_without_raw_runtime_fallback(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / "issue-7.rebase-retries").write_text("3")
    (state / "issue-8.rebase-retries").write_text("malformed")
    before = dump(control)
    raw = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert report["rebase_shadow"] == [{"issue": 7, "legacy": 3, "imported": 3, "agrees": True}]
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copied = tmp_path / "rebase-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
    assert code == 0 and all(result["checks"].values()), error
    with closing(sqlite3.connect(copied / "issue-agent-control" / Path(control.path).name)) as db:
        db.row_factory = sqlite3.Row
        assert load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(7))).issue_recovery.state.rebase_failures == 3
        assert isinstance(load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8))).issue_recovery, cc.BlockedIssueRecovery)
    assert dump(control) == before and {path.name: path.read_bytes() for path in state.iterdir()} == raw


@pytest.mark.parametrize("kind", ["ROUTE", "RESOLVE", "RESUME", "AUTHOR", "IMPLEMENT", "ADJUDICATE", "PR_RESUME", "FIX_CI", "ADDRESS_REVIEW", "REBASE"])
def test_stopped_fix_import_blocks_attributed_pre_f7_paid_history(stopped, capsys, kind):
    from issue_agent.domain.contracts import RunId
    from issue_agent.adapters.control_store import workflow_episode_key
    from issue_agent.adapters.workflow_codec import encode_workflow
    from tests.fakes.workflow_authority import resolve_snapshot
    state, control, _ = stopped
    base = resolve_snapshot(7)
    install_workflow(control, base)
    (state / "issue-7.infra-deaths").write_text("0")
    control.start_run(RunId("legacy-paid"), json.dumps({"repository":"o/r", "role":"fix", "issue":7}),
        kind, "o/r:fix", "legacy", state="abandoned")
    with control.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=?,workflow_episode_key=? WHERE run_id='legacy-paid'",
            (encode_workflow(base.binding), workflow_episode_key(base.binding)))
    before = dump(control)
    code, result, error = command(capsys, "fix", "inventory")
    assert code == 0, error
    target = next(t for t in result["targets"] if t["family"] == "issue_recovery" and t["number"] == 7)
    assert not target["eligible"]
    assert any("prior paid recovery history" in reason for reason in target["blockers"])
    assert dump(control) == before


def test_stopped_fix_import_refuses_unassigned_pre_f7_death_without_zero_inference(stopped, capsys):
    from issue_agent.domain.contracts import RunId
    state, control, _ = stopped
    (state / "issue-7.infra-deaths").write_text("0")
    control.start_run(RunId("unassigned-death"), "{", "PR_RESUME", "o/r:fix", "legacy", state="abandoned")
    before = dump(control)
    code, result, error = command(capsys, "fix", "inventory")
    assert code != 0 and result is None and "unassigned prior" in error
    assert dump(control) == before


def test_machine_shadow_and_copy_preserve_selected_hold_and_refuse_only_bad_target(stopped, capsys, tmp_path):
    state, control, _ = stopped
    payload = dict(target="PR", number=11, pr=11, blocker="checks", blocked_head="head")
    (state / "issue-7.machine-block").write_text(json.dumps(payload))
    (state / "issue-8.machine-block").write_text('{"target":"PR"}')
    original_sql = dump(control)
    original_files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    fact = [7, "PR", 11, 11, "checks", "head"]
    assert report["machine_shadow"] == [{"issue": 7, "legacy": fact, "imported": fact, "agrees": True}]
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copied = tmp_path / "machine-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
    assert code == 0 and all(result["checks"].values()), error
    from issue_agent.adapters.control_store import SQLiteControlStore
    reopened = SQLiteControlStore(str(copied / "issue-agent-control" / Path(control.path).name))
    selected = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    assert selected.component.state.machine_block.blocked_head == "head"
    assert selected.component.state.machine_block.pr == 11
    with reopened.transaction() as db:
        blocked = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8)))
    assert isinstance(blocked.issue_recovery, cc.BlockedIssueRecovery)
    assert dump(control) == original_sql
    assert {path.name: path.read_bytes() for path in state.iterdir()} == original_files


def test_budget_shadow_and_copy_preserve_selected_cap_facts_and_refuse_only_bad_target(stopped, capsys, tmp_path):
    state, control, _ = stopped
    payload = dict(target="PR", number=11, reservation_micros=2_000_000,
                   scope="issue:7", cap_micros=5_000_000, old_headroom_micros=250_000, currency="USD")
    (state / "issue-7.budget-block").write_text(json.dumps(payload))
    (state / "issue-8.budget-block").write_text('{"target":"PR"}')
    original_sql = dump(control)
    original_files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    fact = [7, "PR", 11, 2_000_000, "issue:7", 5_000_000, 250_000, "USD"]
    assert report["budget_shadow"] == [{"issue": 7, "legacy": fact, "imported": fact, "agrees": True}]
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copied = tmp_path / "budget-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
    assert code == 0 and all(result["checks"].values()), error
    from issue_agent.adapters.control_store import SQLiteControlStore
    reopened = SQLiteControlStore(str(copied / "issue-agent-control" / Path(control.path).name))
    block = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(7))).component.state.budget_block
    assert (block.reservation_micros, block.scope, block.cap_micros, block.old_headroom_micros, block.currency) == (
        2_000_000, "issue:7", 5_000_000, 250_000, "USD")
    with reopened.transaction() as db:
        blocked = load_issue_aggregate(db, WorkflowKey("o/r", IssueRef(8)))
    assert isinstance(blocked.issue_recovery, cc.BlockedIssueRecovery)
    assert dump(control) == original_sql
    assert {path.name: path.read_bytes() for path in state.iterdir()} == original_files


def test_self_clear_shadow_and_copy_preserve_one_exhausted_shared_allowance(stopped, capsys, tmp_path):
    state, control, _ = stopped
    (state / "issue-7.self-clears").write_text("3")
    (state / "issue-7.self-clear-ceiling").write_text("1")
    (state / "issue-8.self-clear-ceiling").write_text("1")
    original_sql = dump(control)
    original_files = {path.name: path.read_bytes() for path in state.iterdir()}
    code, report, error = command(capsys, "fix", "shadow")
    assert code == 0, error
    assert report["self_clear_shadow"] == [{"issue": 7, "legacy": [3, True], "imported": [3, True], "agrees": True}]
    assert [(target["number"], target["eligible"]) for target in report["targets"]] == [(7, True), (8, False)]
    copied = tmp_path / "self-clear-copy"
    code, result, error = command(capsys, "fix", "rehearse", "--copy-to", str(copied))
    assert code == 0 and all(result["checks"].values()), error
    from issue_agent.adapters.control_store import SQLiteControlStore
    reopened = SQLiteControlStore(str(copied / "issue-agent-control" / Path(control.path).name))
    selected = reopened.read_issue_recovery(WorkflowKey("o/r", IssueRef(7)))
    assert (selected.component.state.self_clears, selected.component.state.self_clear_ceiling_reported) == (3, True)
    assert dump(control) == original_sql
    assert {path.name: path.read_bytes() for path in state.iterdir()} == original_files


def retained_execution(control, *, index=101, kind='RESOLVE', number=7, issue=7, role='fix'):
    from tests.adapters.test_control_store import _request
    request = replace(_request(index, maximum=1, cap=100), kind=kind, owner_id=f'o/r:{role}:legacy',
        dispatch_id=json.dumps(dict(repository='o/r',role=role,kind=kind,number=number,
                                    issue=issue,breaker_key=issue if issue is not None else -number)))
    assert control.reserve(request).admitted
    assert control.complete_run(request.run_id,state='abandoned',outcome='dead_lease')
    assert control.release(request.run_id,'test stopped unknown-capacity disposition').disposition.value=='available'
    return request


def accepted_import(stopped,capsys,tmp_path,family='fix'):
    if family=='steward':Path(stopped[2]['STEWARD_STATE_DIR']).mkdir(parents=True,exist_ok=True)
    code,report,error=command(capsys,family,'shadow')
    assert code==0,error
    saved=tmp_path/(family+'-retained-plan.json');saved.write_text(json.dumps(report))
    code,accepted,error=command(capsys,family,'activate','--plan-file',str(saved),'--confirm-plan-digest',report['plan_digest'])
    assert code==0 and accepted['status']=='activated',error
    return report,saved


@pytest.mark.parametrize('family,role,kind,component',[('fix','fix','RESOLVE','issue_recovery'),
                                                     ('steward','steward','TRIAGE','steward')])
def test_identified_retained_history_holds_only_its_target_and_imported_unspent_sibling_can_run(stopped,capsys,tmp_path,family,role,kind,component):
    state,control,_=stopped
    retained_execution(control,kind=kind,role=role)
    root=state if family=='fix' else Path(stopped[2]['STEWARD_STATE_DIR'])
    root.mkdir(parents=True,exist_ok=True)
    (root/('issue-8.resumes' if family=='fix' else 'triage-8.at')).write_text('0' if family=='fix' else '2026-10-01T00:00:00Z')
    report,_=accepted_import(stopped,capsys,tmp_path,family)
    targets={t['number']:t for t in report['targets']}
    assert not targets[7]['eligible'] and targets[8]['eligible']
    assert any('historical executions' in b for b in targets[7]['blockers'])
    with control.transaction() as db:
        old=load_issue_aggregate(db,WorkflowKey('o/r',IssueRef(7)))
        fresh=load_issue_aggregate(db,WorkflowKey('o/r',IssueRef(8)))
    assert isinstance(getattr(old,component),cc.BlockedIssueRecovery if family=='fix' else cc.BlockedSteward)
    if family=='fix':
        from tests.adapters.test_continuation_admission import paid,purchase
        # The continuation is allowed without inventing a primary resolve episode.
        assert isinstance(fresh,IssueWorkflowShell)
        selected=purchase(fresh,reply='2026-10-02T00:00:00Z',ceiling=5)
        assert control.reserve_issue_recovery(paid('fresh-retained-sibling',selected),selected).admitted


@pytest.mark.parametrize('change',['alter','add','remove'])
def test_history_changed_after_plan_refuses_atomic_publication(stopped,capsys,change):
    from issue_agent.adapters.resolve_activation import ResolveActivationRefused
    state,control,_=stopped
    request=retained_execution(control)
    scope=continuation_activation.activation_scope('target',harness_root=cli.HARNESS_ROOT)
    plan=continuation_activation.evaluate(scope,control)
    if change=='add':retained_execution(control,index=102,number=8,issue=8)
    else:
        with control.transaction() as db:
            if change=='alter':db.execute("UPDATE runs SET config_version='changed' WHERE run_id=?",(request.run_id,))
            else:
                for table in ('reservation_recovery','reservation_limits','budget_reservations','runs'):
                    db.execute('DELETE FROM '+table+' WHERE run_id=?',(request.run_id,))
    before=dump(control)
    with pytest.raises(ResolveActivationRefused,match='inventory'):
        with control.transaction() as db:
            publish_continuation_import(db,plan,now=datetime.now(timezone.utc),control=control)
    assert dump(control)==before


def remote_retained_pr(monkeypatch,state):
    from issue_agent.domain.review_workflow import CompleteReviews,ObservedPullRequest,PullRequestState
    from issue_agent.domain.world import PRView
    def read(gateway,key,now,**kwargs):
        if state=='unreadable':return None,None
        observed=ObservedPullRequest(key,'a'*40,PullRequestState(state),frozenset(),CompleteReviews(()),now)
        return observed,PRView(key.target.number,'fix/7-retained','a'*40,closing_issue=7)
    monkeypatch.setattr(continuation_activation,'read_pull_request',read)


@pytest.mark.parametrize('state',['closed','merged'])
def test_closed_retained_pr_is_deferred_without_primary_then_discovery_preserves_hold(stopped,capsys,tmp_path,monkeypatch,state):
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.labels import Label
    from tests.fakes.persistent_github import PersistentGitHub
    from issue_agent.domain.world import PRView
    from issue_agent.domain.workflow import WorkflowCommitted
    from issue_agent.adapters.control_store import SQLiteControlStore
    _,control,_=stopped
    retained_execution(control,kind='FIX_CI',number=9,issue=7)
    remote_retained_pr(monkeypatch,state)
    report,saved=accepted_import(stopped,capsys,tmp_path)
    assert report['targets']==[dict(family='pr_repair',number=9,eligible=False,blockers=report['targets'][0]['blockers'])]
    key=WorkflowKey('o/r',PullRequestRef(9))
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM workflows WHERE repository='o/r' AND target_kind='pr' AND target_number=9").fetchone()[0]==0
        from issue_agent.adapters.continuation_activation import deferred_pr_repair
        assert isinstance(deferred_pr_repair(db,key),cc.BlockedPRRepair)
    restarted=SQLiteControlStore(control.path)
    code,retry,error=command(capsys,'fix','activate','--plan-file',str(saved),'--confirm-plan-digest',report['plan_digest'])
    assert code==0 and retry['status']=='already_active_with_this_plan',error
    with restarted.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_rejections',1,'source','inventory',?)",(datetime.now(timezone.utc).isoformat(),))
    remote=PersistentGitHub(tmp_path/'retained-remote.sqlite')
    remote.seed_pr(PRView(9,head_ref_oid='a'*40,labels=frozenset({Label.AI_PR_OPEN})))
    review=SQLiteReviewWorkflowControl(restarted,WorkflowWriter('o/r','review','o/r:review:test'),gateway=remote,
                                      ceiling=3,trigger_label=Label.AI_PR_OPEN,rule=CountingRule('review-bot[bot]'))
    discovered=review.ensure_observed_workflow(key)
    assert isinstance(discovered,WorkflowCommitted)
    assert isinstance(discovered.snapshot.pr_repair,cc.BlockedPRRepair)
    assert review.ensure_observed_workflow(key).snapshot.pr_repair==discovered.snapshot.pr_repair


@pytest.mark.parametrize('state',['open','unreadable'])
def test_missing_primary_retained_pr_cannot_defer_without_proven_closed_state(stopped,capsys,monkeypatch,state):
    _,control,_=stopped
    retained_execution(control,kind='FIX_CI',number=9,issue=7)
    remote_retained_pr(monkeypatch,state)
    before=dump(control)
    code,report,error=command(capsys,'fix','shadow')
    assert code==2 and report is None and 'verified closed or merged' in error
    assert dump(control)==before


def test_genuinely_new_issue_after_activation_gets_active_secondary_and_paid_steward(stopped,capsys,tmp_path):
    from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
    from issue_agent.domain.workflow import ObservedIssue
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.world import IssueView
    from tests.fakes.workflow_authority import resolve_snapshot
    from tests.adapters.test_workflow_admission import request
    state,control,_=stopped
    retained_execution(control)
    retained_execution(control,index=102,kind='TRIAGE',role='steward')
    install_workflow(control,resolve_snapshot(1))
    accepted_import(stopped,capsys,tmp_path,'fix')
    accepted_import(stopped,capsys,tmp_path,'steward')
    key=WorkflowKey('o/r',IssueRef(99))
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM workflows WHERE target_kind='issue' AND target_number=99").fetchone()[0]==0
    assert not list(state.glob('issue-99.*'))
    workflow_control=SQLiteWorkflowControl(control,WorkflowWriter('o/r','fix','o/r:fix:test'),resolve_ceiling=3)
    discovered=workflow_control.ensure_observed_workflow(ObservedIssue(key,IssueView(99,updated_at='2026-10-01T00:00:00Z'),control.now())).snapshot
    assert isinstance(discovered.issue_recovery,cc.ActiveIssueRecovery)
    assert isinstance(discovered.steward,cc.ActiveSteward)
    selected=cc.select_steward(discovered)
    purchase=cc.StewardPurchase(cc.ContinuationCapture(selected.binding,selected.revision,'leg:new-steward'),
                               cc.StewardLane.TRIAGE,control.now().isoformat(),2)
    requested=replace(request('new-steward'),workflow=selected.binding,owner_id='o/r:steward:test',kind='TRIAGE',
        dispatch_id=json.dumps(dict(repository='o/r',role='steward',kind='TRIAGE',issue=99,number=99,leg_id='leg:new-steward')))
    assert control.reserve_steward(requested,purchase).admitted
    with control.transaction() as db:
        old=load_issue_aggregate(db,WorkflowKey('o/r',IssueRef(7)))
        assert isinstance(old.issue_recovery,cc.BlockedIssueRecovery) and isinstance(old.steward,cc.BlockedSteward)


def test_genuinely_new_pr_after_activation_gets_paid_repair_without_clearing_old_hold(stopped,capsys,tmp_path):
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.labels import Label
    from issue_agent.domain.world import PRView
    from tests.fakes.persistent_github import PersistentGitHub
    from tests.adapters.test_workflow_admission import request
    _,control,_=stopped
    retained_execution(control)
    accepted_import(stopped,capsys,tmp_path)
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_rejections',1,'source','inventory',?)",(control.now().isoformat(),))
    remote=PersistentGitHub(tmp_path/'new-pr-remote.sqlite')
    remote.seed_pr(PRView(99,head_ref_oid='a'*40,labels=frozenset({Label.AI_PR_OPEN})))
    review=SQLiteReviewWorkflowControl(control,WorkflowWriter('o/r','review','o/r:review:test'),gateway=remote,
                                      ceiling=3,trigger_label=Label.AI_PR_OPEN,rule=CountingRule('review-bot[bot]'))
    discovered=review.ensure_observed_workflow(WorkflowKey('o/r',PullRequestRef(99))).snapshot
    assert isinstance(discovered.pr_repair,cc.ActivePRRepair)
    selected=cc.select_pr_repair(discovered)
    purchase=cc.PRRepairPurchase(cc.ContinuationCapture(selected.binding,selected.revision,'leg:new-repair'),
                                allowance=cc.RepairAllowance.INFRA,ceiling=2)
    requested=replace(request('new-repair'),workflow=selected.binding,
        dispatch_id=json.dumps(dict(repository='o/r',role='fix',kind='FIX_CI',issue=None,number=99,leg_id='leg:new-repair')))
    assert control.reserve_pr_repair(requested,purchase).admitted
    with control.transaction() as db:
        assert isinstance(load_issue_aggregate(db,WorkflowKey('o/r',IssueRef(7))).issue_recovery,cc.BlockedIssueRecovery)


def test_changed_publisher_deferred_decision_refuses_primary_discovery(stopped,capsys,tmp_path,monkeypatch):
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.adapters.workflow_codec import decode_workflow,encode_workflow
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.labels import Label
    from issue_agent.domain.world import PRView
    from tests.fakes.persistent_github import PersistentGitHub
    _,control,_=stopped
    retained_execution(control,kind='FIX_CI',number=9,issue=7)
    remote_retained_pr(monkeypatch,'closed')
    accepted_import(stopped,capsys,tmp_path)
    with control.transaction() as db:
        payload=db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-deferred-pr'").fetchone()[0]
        receipt=decode_workflow(payload,tuple)
        changed=replace(receipt[3],blockers=(cc.ContinuationImportBlocker('changed','unverified correction'),))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='continuation-deferred-pr'",
                   (encode_workflow((*receipt[:3],changed,receipt[4])),))
        db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_rejections',1,'source','inventory',?)",(control.now().isoformat(),))
    remote=PersistentGitHub(tmp_path/'mutated-deferred-remote.sqlite')
    remote.seed_pr(PRView(9,head_ref_oid='a'*40))
    review=SQLiteReviewWorkflowControl(control,WorkflowWriter('o/r','review','o/r:review:test'),gateway=remote,
                                      ceiling=3,trigger_label=Label.AI_PR_OPEN,rule=CountingRule('review-bot[bot]'))
    with pytest.raises(WorkflowAuthorityError,match='deferred PR repair import is unreadable'):
        review.ensure_observed_workflow(WorkflowKey('o/r',PullRequestRef(9)))
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM workflows WHERE target_kind='pr' AND target_number=9").fetchone()[0]==0


def test_steward_first_discovery_paid_state_survives_fix_promotion_and_repeat_observation(stopped,capsys,tmp_path,monkeypatch):
    from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    from issue_agent.domain.workflow import ObservedIssue,WorkflowAbsent
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.world import IssueView
    from tests.fakes.workflow_authority import resolve_snapshot
    from tests.adapters.test_workflow_admission import request
    from tests.adapters.test_continuation_admission import money,receipt
    _,control,_=stopped
    retained_execution(control,kind='TRIAGE',role='steward')
    accepted_import(stopped,capsys,tmp_path,'steward')
    key=WorkflowKey('o/r',IssueRef(99));observation=ObservedIssue(key,IssueView(99,updated_at='2026-10-01T00:00:00Z'),control.now())
    writer=WorkflowWriter('o/r','steward','o/r:steward:test')
    selected=control.ensure_steward_observed_issue(observation,writer=writer)
    assert isinstance(selected,cc.SelectedSteward)
    with control.transaction() as db:assert isinstance(load_issue_aggregate(db,key),IssueWorkflowShell)
    purchase=cc.StewardPurchase(cc.ContinuationCapture(selected.binding,selected.revision,'leg:standalone-steward'),
                               cc.StewardLane.TRIAGE,control.now().isoformat(),2)
    requested=replace(request('standalone-steward'),workflow=selected.binding,owner_id=writer.owner_id,kind='TRIAGE',
        dispatch_id=json.dumps(dict(repository='o/r',role='steward',kind='TRIAGE',issue=99,number=99,leg_id='leg:standalone-steward')))
    assert control.reserve_steward(requested,purchase).admitted
    returned=cc.StewardState(triage=cc.StewardLaneState(stalls=1))
    assert control.settle_steward(money(requested.run_id),receipt(control,requested.run_id,WorkflowFamily.STEWARD),returned).disposition.value=='available'
    measured=control.read_steward(key)
    assert measured.component.state.triage.stalls==1
    assert control.ensure_steward_observed_issue(observation,writer=writer)==measured
    install_workflow(control,resolve_snapshot(1))
    fix=SQLiteWorkflowControl(control,WorkflowWriter('o/r','fix','o/r:fix:test'),resolve_ceiling=3)
    assert isinstance(fix.load_workflow(key),WorkflowAbsent)
    discovery=[];original_transition=control._transition
    def capture_transition(db,entity_kind,entity_id,state,detail=None,**kwargs):
        if state=='discovered':discovery.append(detail)
        return original_transition(db,entity_kind,entity_id,state,detail,**kwargs)
    monkeypatch.setattr(control,'_transition',capture_transition)
    promoted=fix.ensure_observed_workflow(ObservedIssue(key,IssueView(99,updated_at='2026-10-01T00:00:00Z'),control.now())).snapshot
    assert promoted.steward==measured.component
    assert any(detail['generation']==promoted.revision.fence.generation and detail['version']==promoted.revision.version for detail in discovery)
    assert fix.ensure_observed_workflow(ObservedIssue(key,IssueView(99,updated_at='2026-10-01T00:00:00Z'),control.now())).snapshot==promoted
    oldkey=WorkflowKey('o/r',IssueRef(7));before=dump(control)
    with pytest.raises(WorkflowAuthorityError,match='historical issue migration hold'):
        fix.ensure_observed_workflow(ObservedIssue(oldkey,IssueView(7,updated_at='2026-10-01T00:00:00Z'),control.now()))
    assert dump(control)==before


@pytest.mark.parametrize('bad',['stale','future','foreign_writer','wrong_role','missing_evidence','malformed_evidence'])
def test_steward_first_discovery_requires_fresh_scope_and_uncorrupted_activation(stopped,capsys,tmp_path,bad):
    from datetime import timedelta
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    from issue_agent.domain.workflow import ObservedIssue
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.world import IssueView
    _,control,_=stopped
    accepted_import(stopped,capsys,tmp_path,'steward')
    writer=WorkflowWriter('o/r','steward','o/r:steward:test')
    now=control.now()
    if bad=='stale':now-=timedelta(seconds=301)
    if bad=='future':now+=timedelta(seconds=1)
    if bad=='foreign_writer':writer=WorkflowWriter('x/y','steward','x/y:steward:test')
    if bad=='wrong_role':writer=WorkflowWriter('o/r','fix','o/r:fix:test')
    if bad in {'missing_evidence','malformed_evidence'}:
        with control.transaction() as db:
            if bad=='missing_evidence':db.execute("DELETE FROM workflow_activation_evidence WHERE family='steward'")
            else:db.execute("UPDATE workflow_activation_evidence SET plan_digest=? WHERE family='steward'",('z'*64,))
    before=dump(control)
    with pytest.raises((ValueError,WorkflowAuthorityError)):
        control.ensure_steward_observed_issue(ObservedIssue(WorkflowKey('o/r',IssueRef(99)),IssueView(99,updated_at='2026-10-01T00:00:00Z'),now),writer=writer)
    assert dump(control)==before


def test_steward_observation_expiring_under_owner_lock_cannot_create_target(stopped,capsys,tmp_path,monkeypatch):
    from datetime import timedelta
    from issue_agent.domain.workflow import ObservedIssue
    from issue_agent.domain.workflow_identity import WorkflowWriter
    from issue_agent.domain.world import IssueView
    _,control,_=stopped
    accepted_import(stopped,capsys,tmp_path,'steward')
    now=control.now();before=dump(control);original=control._assert_owner_active
    def delayed(db,owner):
        original(db,owner)
        monkeypatch.setattr(control,'_now',lambda:now+timedelta(seconds=301))
    monkeypatch.setattr(control,'_assert_owner_active',delayed)
    with pytest.raises(ValueError,match='became stale'):
        control.ensure_steward_observed_issue(ObservedIssue(WorkflowKey('o/r',IssueRef(99)),IssueView(99,updated_at='2026-10-01T00:00:00Z'),now),writer=WorkflowWriter('o/r','steward','o/r:steward:test'))
    assert dump(control)==before


@pytest.mark.parametrize("previous_inactive",[False,True])
def test_deferred_fix_hold_survives_real_execution_import_primary_creation_and_saved_retry(stopped,capsys,tmp_path,monkeypatch,previous_inactive):
    from issue_agent.adapters.review_execution_import_commit import commit_review_execution_import
    from issue_agent.adapters.continuation_activation import deferred_pr_repair
    from issue_agent.domain.review_execution_history_import import ReviewImportHistoryBlocked,ReviewImportBlocker,ReviewImportBlockerKind,ReviewImportSource
    from issue_agent.domain.review_execution_import_plan import ReviewExecutionImportPlan
    from issue_agent.domain.review_activation import OpenPullRequestsListed
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    _,control,_=stopped
    retained_execution(control,kind='FIX_CI',number=9,issue=7)
    remote_retained_pr(monkeypatch,'closed')
    report,saved=accepted_import(stopped,capsys,tmp_path)
    key=WorkflowKey('o/r',PullRequestRef(9));now=control.now()
    with control.transaction() as db:
        original=deferred_pr_repair(db,key)
        db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_rejections',1,'source','inventory',?)",(now.isoformat(),))
        history=ReviewImportHistoryBlocked(9,(ReviewImportBlocker(ReviewImportBlockerKind.ORIGIN,(ReviewImportSource('session','review:pr:9','f'*64),)),))
        plan=ReviewExecutionImportPlan('o/r',OpenPullRequestsListed((9,)),1,'b'*64,'c'*64,'d'*64,(history,),now)
        commit_review_execution_import(db,plan,now=now)
        installed=load_review_workflow_row(db,key)
        assert installed.pr_repair==original
        if previous_inactive:
            from issue_agent.adapters.workflow_rows import encode_workflow_episodes
            db.execute("UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='pr' AND target_number=9",(encode_workflow_episodes(replace(installed,pr_repair=cc.InactivePRRepair())),))
    if previous_inactive:
        from tests.fakes.persistent_github import PersistentGitHub
        from issue_agent.domain.world import PRView
        from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
        from issue_agent.domain.workflow_identity import WorkflowWriter
        from issue_agent.domain.review_rejection import CountingRule
        from issue_agent.domain.labels import Label
        remote=PersistentGitHub(tmp_path/'previous-inactive.sqlite')
        remote.seed_pr(PRView(9,head_ref_oid='a'*40,labels=frozenset({Label.AI_PR_OPEN})))
        observer=SQLiteReviewWorkflowControl(control,WorkflowWriter('o/r','review','o/r:review:test'),gateway=remote,ceiling=3,trigger_label=Label.AI_PR_OPEN,rule=CountingRule('review-bot[bot]'))
        recovered=observer.ensure_observed_workflow(key).snapshot
        assert recovered.pr_repair==original
        assert recovered.execution==installed.execution and recovered.revision.fence==installed.revision.fence
        assert recovered.revision.version==installed.revision.version+1
        assert observer.ensure_observed_workflow(key).snapshot==recovered
    code,retry,error=command(capsys,'fix','activate','--plan-file',str(saved),'--confirm-plan-digest',report['plan_digest'])
    assert code==0 and retry['status']=='already_active_with_this_plan',error
    from issue_agent.domain.review_preflight import require_review_execution_eligibility
    with pytest.raises(ValueError,match="historical PR repair migration is blocked"):
        require_review_execution_eligibility(installed,"a"*40,3)


def test_actual_rejection_and_eligible_execution_imports_retain_deferred_repair_hold(stopped,capsys,tmp_path,monkeypatch):
    from issue_agent.adapters.review_activation import SQLiteReviewActivation,read_legacy_review_state
    from issue_agent.domain.review_activation import plan_review_activation,OpenPullRequestsListed,ReviewActivationPlan
    from issue_agent.domain.review_workflow import ObservedPullRequest,CompleteReviews,PullRequestState,RecordedPullRequest
    from issue_agent.domain.labels import Label
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    from issue_agent.adapters.continuation_activation import deferred_pr_repair
    from issue_agent.domain.review_execution_history_import import ReviewImportNoHistoryObserved
    from issue_agent.domain.review_execution_import_plan import screen_zero_history_review_import,EligibleZeroHistoryReviewImport,ReviewExecutionImportPlan
    from issue_agent.domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
    from issue_agent.adapters.review_execution_import_commit import commit_review_execution_import
    from issue_agent.domain.review_preflight import require_review_execution_eligibility
    from issue_agent.domain.review_execution import CurrentReviewExecution
    _,control,_=stopped
    retained_execution(control,kind='FIX_CI',number=9,issue=7);remote_retained_pr(monkeypatch,'closed')
    report,saved=accepted_import(stopped,capsys,tmp_path)
    activation=SQLiteReviewActivation(control);inventory,recorded=activation.inventory('o/r')
    root=tmp_path/'empty-review';root.mkdir();(root/'pr-9.operator-guidance').write_text('Preserve the existing migration hold.');key=WorkflowKey('o/r',PullRequestRef(9));now=control.now()
    observed=ObservedPullRequest(key,'a'*40,PullRequestState.OPEN,frozenset({Label.AI_PR_OPEN}),CompleteReviews(()),now)
    plan=plan_review_activation('o/r',read_legacy_review_state(str(root)),(observed,),OpenPullRequestsListed((9,)),inventory,ceiling=3,review_bot='bot',trigger_label=Label.AI_PR_OPEN,recorded=recorded)
    assert isinstance(plan,ReviewActivationPlan)
    assert tuple(target.pr for target in plan.imports)==(9,)
    activation.activate(plan,revalidate=lambda:plan)
    with control.transaction() as db:
        snapshot=load_review_workflow_row(db,key);blocked=deferred_pr_repair(db,key)
        assert snapshot.pr_repair==blocked
        eligible=screen_zero_history_review_import(ReviewImportNoHistoryObserved(9,'b'*64),snapshot,RecordedPullRequest(observed),LegacyOpenPRCounterLink(9,'a'*40,'fix/9',None,None),'bot',now=now)
        assert isinstance(eligible,EligibleZeroHistoryReviewImport)
        execution_plan=ReviewExecutionImportPlan('o/r',OpenPullRequestsListed((9,)),1,'b'*64,'c'*64,'d'*64,(eligible,),now)
        installed=commit_review_execution_import(db,execution_plan,now=now)[0]
        assert isinstance(installed.execution,CurrentReviewExecution) and installed.pr_repair==blocked
    with pytest.raises(ValueError,match='historical PR repair migration is blocked'):
        require_review_execution_eligibility(installed,'a'*40,3)
    code,retry,error=command(capsys,'fix','activate','--plan-file',str(saved),'--confirm-plan-digest',report['plan_digest'])
    assert code==0 and retry['status']=='already_active_with_this_plan',error
