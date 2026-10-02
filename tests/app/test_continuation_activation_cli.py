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
    Store(str(state)).clear_issue(8)
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
