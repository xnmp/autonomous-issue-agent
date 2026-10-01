"""The supported operator commands publish only isolated, reviewed plans."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from issue_agent.adapters.resolve_activation import file_digests
from issue_agent.adapters.workflow_rows import encode_workflow_episodes, insert_workflow_component
from issue_agent.app import cli
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey
from tests.app.test_resolve_activation_cli import deployment, dump, write_snapshot
from tests.app.test_review_execution_activation import Gateway


import sqlite3
from itertools import count

from issue_agent.adapters.control_store import AccountingDisposition
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_rows import load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import (
    ReviewExecutionOriginKind, ReviewRoundCancelled, ReviewRoundFailure,
)
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_rounds import FinalizedReviewRound
from issue_agent.domain.review_workflow import ObservedPullRequest, PullRequestState
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey, WorkflowWriter
from tests.app.test_review_execution_activation import rig
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_control import OWNER
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage


@pytest.fixture
def execution_deployment(deployment, monkeypatch):
    _, control, env = deployment
    monkeypatch.delenv("REVIEW_BOT", raising=False)
    env["REVIEW_BOT"] = "review-bot"
    write_snapshot(Path(env["STATE_DIR"]).parents[1], env)
    for key in ("REVIEW_STATE_DIR", "STEWARD_STATE_DIR"):
        Path(env[key]).mkdir(parents=True)
    authority = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   ("o/r", authority.family.value, 1, "a" * 64, "b" * 64, datetime.now(timezone.utc).isoformat()))
        for pr in (9, 10):
            key = WorkflowKey("o/r", PullRequestRef(pr))
            workflow = initial_review_workflow(key, authority)
            db.execute("INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)",
                       ("o/r", pr, encode_workflow_episodes(workflow)))
            insert_workflow_component(db, key, authority, workflow.episode.episode_id)
    gateway = Gateway()
    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway", lambda *args, **kwargs: gateway)
    review = Path(env["REVIEW_STATE_DIR"])
    (review / "pr-9.round").write_text("3")
    (review / "pr-11.round").write_text("4")
    return control, env, gateway


def run(capsys, *argv):
    args = cli.build_parser().parse_args(["review-execution-activation", *argv, "--name", "target"])
    code = args.func(args)
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out.strip() else None, output.err


def test_composed_inventory_shadow_and_original_plan_retry(execution_deployment, capsys, tmp_path, monkeypatch):
    control, env, gateway = execution_deployment
    review = Path(env["REVIEW_STATE_DIR"])
    before = dump(control), file_digests(tuple(str(p) for p in review.iterdir()))
    code, report, error = run(capsys, "shadow")
    assert code == 0, error
    assert report["execution_authority"] == "migration_required"
    targets = {item["pr"]: item for item in report["targets"]}
    assert set(targets) == {9, 10, 11}
    assert targets[9]["held"] and not targets[9]["migration_eligible"]
    assert targets[11]["held"] and not targets[11]["migration_eligible"]
    assert targets[10]["migration_eligible"] and not targets[10]["held"]
    assert targets[10]["failures"] == 0 and targets[10]["session"] == "NoReviewSession"
    assert targets[10]["remaining"] == 3 and targets[9]["remaining"] is None
    assert {item["pr"] for item in report["shadow"]} == set(targets)
    assert all(set(item["comparisons"]) == {"eligibility", "allowance", "session", "holds"} for item in report["shadow"])
    assert {item["pr"] for item in report["shadow"] if item["differences"]} == {9, 11}
    shadow = {item["pr"]: item for item in report["shadow"]}
    assert shadow[10]["comparisons"] == {"eligibility": "agrees", "allowance": "agrees_zero",
                                          "session": "agrees_none", "holds": "agrees_clear"}
    for pr in (9, 11):
        assert shadow[pr]["comparisons"]["eligibility"] == "held_pending_history"
        assert shadow[pr]["new"]["blockers"] and shadow[pr]["new"]["remaining"] is None
    assert (dump(control), file_digests(tuple(str(p) for p in review.iterdir()))) == before
    saved = tmp_path / "original-plan.json"
    saved.write_text(json.dumps(report))
    digest = report["plan"]["plan_digest"]
    assert run(capsys, "activate", "--confirm-plan-digest", digest)[0] == 2
    assert run(capsys, "activate", "--plan-file", str(saved), "--confirm-plan-digest", "0" * 64)[0] == 2
    assert dump(control) == before[0]
    code, activated, error = run(capsys, "activate", "--plan-file", str(saved), "--confirm-plan-digest", digest)
    assert code == 0 and activated["status"] == "activated", error
    gateway.unavailable = True
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: pytest.fail("exact retry read liveness"))
    (review / "pr-9.round").write_text("99")
    after = dump(control)
    code, retried, error = run(capsys, "activate", "--plan-file", str(saved), "--confirm-plan-digest", digest)
    assert code == 0 and retried["status"] == "already_active_with_this_plan", error
    assert retried["original_plan"] == report["original_plan"]
    assert dump(control) == after
    assert run(capsys, "inventory")[1]["execution_authority"] == "active"


def test_copied_rehearsal_preserves_original_database_and_files(execution_deployment, capsys, tmp_path):
    control, env, _ = execution_deployment
    review = Path(env["REVIEW_STATE_DIR"])
    originals = (control.path, control.authority_witness_path, *(str(p) for p in review.iterdir()))
    before = file_digests(originals), dump(control)
    code, report, error = run(capsys, "rehearse", "--copy-to", str(tmp_path / "execution-copy"))
    assert code == 0 and report["status"] == "rehearsed", error
    assert report["checks"] == {"exact_retry_inert": True, "different_plan_refused": True, "originals_unchanged": True}
    assert set(item["pr"] for item in report["targets"]) == {9, 10, 11}
    assert "issue-agent-control" in report["copy"]["control_path"]
    assert (file_digests(originals), dump(control)) == before


def _leave_committed_wal(path):
    import os
    import sqlite3
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("UPDATE workflows SET version=version+1")
    connection.commit()
    # Model a stopped/crashed writer whose committed WAL has not checkpointed.
    os._exit(0)


def test_rehearsal_preserves_original_with_uncheckpointed_wal(execution_deployment, capsys, tmp_path):
    import multiprocessing
    control, _, _ = execution_deployment
    child = multiprocessing.get_context("fork").Process(target=_leave_committed_wal, args=(control.path,))
    child.start()
    child.join(10)
    assert child.exitcode == 0
    originals = (control.path, control.authority_witness_path, control.path + "-wal")
    assert Path(control.path + "-wal").stat().st_size > 0
    before = file_digests(originals)
    code, report, error = run(capsys, "rehearse", "--copy-to", str(tmp_path / "wal-copy"))
    assert file_digests(originals) == before
    assert code == 0 and report["checks"]["originals_unchanged"], error


def test_rehearsal_refuses_every_source_overlap(execution_deployment, capsys, tmp_path):
    control, env, _ = execution_deployment
    before = dump(control)
    for destination in (Path(env["REVIEW_STATE_DIR"]) / "copy", Path(control.path).parent / "copy",
                        Path(env["TARGET_DIR"]) / "copy", tmp_path):
        code, _, error = run(capsys, "rehearse", "--copy-to", str(destination))
        assert code == 2 and "inside or contain" in error, (destination, error)
        assert not destination.name == "copy" or not destination.exists()
    assert dump(control) == before


def test_first_publication_refuses_watchers_and_changed_sources(execution_deployment, capsys, tmp_path, monkeypatch):
    control, env, _ = execution_deployment
    _, report, _ = run(capsys, "shadow")
    saved = tmp_path / "plan.json"
    saved.write_text(json.dumps(report))
    arguments = ("activate", "--plan-file", str(saved), "--confirm-plan-digest", report["plan"]["plan_digest"])
    before = dump(control)
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: ["review process 4242"])
    code, _, error = run(capsys, *arguments)
    assert code == 2 and "4242" in error
    assert dump(control) == before
    monkeypatch.setattr(cli, "_activation_liveness", lambda *args: [])
    Path(env["REVIEW_STATE_DIR"], "pr-10.round").write_text("1")
    code, _, error = run(capsys, *arguments)
    assert code == 2 and "changed" in error
    assert dump(control) == before


def test_shadow_reports_disabled_ceiling_as_unlimited(execution_deployment, capsys, tmp_path):
    _, env, _ = execution_deployment
    write_snapshot(tmp_path, {**env, "MAX_REVIEW_RETRIES": "0"})
    code, report, error = run(capsys, "shadow")
    assert code == 0, error
    targets = {item["pr"]: item for item in report["targets"]}
    assert targets[10]["allowance"] == "unlimited" and targets[10]["remaining"] is None
    assert targets[9]["allowance"] == "not_imported" and targets[9]["remaining"] is None


@pytest.mark.parametrize("blocker", ("workflow_history", "prior_review", "external_hold"))
def test_current_observation_blocker_is_stable_but_source_changes_refuse(execution_deployment, capsys, tmp_path, blocker):
    from dataclasses import replace
    from issue_agent.domain.labels import Label
    from issue_agent.domain.review_workflow import CompleteReviews, SubmittedReviewFact, SubmittedReviewState
    control, _, gateway = execution_deployment
    if blocker == "workflow_history":
        with control.transaction() as db:
            db.execute("UPDATE workflows SET version=1 WHERE target_number=10")
    elif blocker == "prior_review":
        gateway.reviews[10] = CompleteReviews((SubmittedReviewFact(
            "prior-review", "review-bot", SubmittedReviewState.APPROVED, "a" * 40, datetime.now(timezone.utc)),))
    else:
        gateway.views[10] = replace(gateway.views[10], labels=gateway.views[10].labels | {Label.NEEDS_BUDGET_REVIEW})
    _, report, error = run(capsys, "shadow")
    assert report["status"] == "migration_required", error
    target = next(item for item in report["targets"] if item["pr"] == 10)
    assert target["held"] and not target["migration_eligible"]
    _, again, _ = run(capsys, "shadow")
    assert again["plan"]["plan_digest"] == report["plan"]["plan_digest"]
    saved = tmp_path / "blocked-plan.json"
    saved.write_text(json.dumps(report))
    arguments = ("activate", "--plan-file", str(saved), "--confirm-plan-digest", report["plan"]["plan_digest"])
    if blocker == "prior_review":
        prior = gateway.reviews[10]
        gateway.reviews[10] = CompleteReviews((replace(
            prior.reviews[0], submitted_at=prior.reviews[0].submitted_at + timedelta(seconds=1)),))
        before = dump(control)
        code, _, error = run(capsys, *arguments)
        assert code == 2 and "changed" in error
        assert dump(control) == before
        gateway.reviews[10] = prior
    unchanged = gateway.views[10]
    gateway.views[10] = replace(unchanged, head_ref_oid="b" * 40)
    before = dump(control)
    code, _, error = run(capsys, *arguments)
    assert code == 2 and "changed" in error
    assert dump(control) == before
    gateway.views[10] = unchanged
    code, result, error = run(capsys, *arguments)
    assert code == 0 and result["status"] == "activated", error


@pytest.mark.parametrize("change", ("missing", "unknown", "digest", "candidate", "duplicate", "oversize", "symlink"))
def test_original_plan_malformed_input_refuses_without_writes(execution_deployment, capsys, tmp_path, change):
    control, _, _ = execution_deployment
    _, report, _ = run(capsys, "shadow")
    digest = report["plan"]["plan_digest"]
    plan = report["original_plan"]
    if change == "missing":
        del plan["planned_at"]
    elif change == "unknown":
        plan["authority_override"] = True
    elif change == "digest":
        plan["inventory_digest"] = "f" * 64
    elif change == "candidate":
        plan["candidates"][0] = plan["policy"]
    saved = tmp_path / "plan.json"
    saved.write_text(json.dumps(report))
    if change == "duplicate":
        saved.write_text('{"original_plan":{},"original_plan":{}}')
    elif change == "oversize":
        with saved.open("r+b") as target:
            target.truncate(64 * 1024 * 1024 + 1)
    elif change == "symlink":
        alias = tmp_path / "alias.json"
        alias.symlink_to(saved)
        saved = alias
    before = dump(control)
    code, result, error = run(capsys, "activate", "--plan-file", str(saved), "--confirm-plan-digest", digest)
    assert code == 2 and result is None and "refused" in error
    assert dump(control) == before


def test_imported_empty_origin_survives_paid_failure_and_later_abandonment(rig):
    cfg, store, gateway, _files_store, files, links, sql, _liveness, activation = rig
    sequence = count()
    producer = Producer("review-rehearsal", cfg.repo, "review", 123, "boot")

    def factory(payload, correlation):
        index = next(sequence)
        return EventEnvelope(payload, new_event_id(store.now()), index, store.now(), store.now(),
                             index, producer, correlation)

    store.bind_event_outbox(factory, lambda envelope: None)
    activation_plan = activation.plan(files, links, sql)
    assert not activation.activate(activation_plan, files, links, sql).idempotent

    key = WorkflowKey(cfg.repo, PullRequestRef(9))
    with store.transaction() as db:
        imported = load_review_workflow_row(db, key)
        origin_receipts = db.execute(
            "SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin' AND scope=?",
            (cfg.repo,),
        ).fetchone()[0]
    assert imported is not None
    assert imported.execution.origin.kind is ReviewExecutionOriginKind.IMPORTED
    assert origin_receipts == 2
    assert imported.execution.last_sequence == 0
    assert imported.execution.baseline.through_sequence == 0

    assert store.claim_dispatch(cfg.repo, "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims(cfg.repo, "review")[0].claim_key
    control = SQLiteReviewExecutionControl(store, WorkflowWriter(cfg.repo, "review", OWNER), gateway)

    def select(request_id):
        with store.transaction() as db:
            current = load_review_workflow_row(db, key)
        assert current is not None
        view = gateway.views[9]
        return ReviewPreflightSelection(
            request_id, current.revision, current.execution.binding, occurrence, -9,
            ObservedPullRequest(key, view.head_ref_oid, PullRequestState.OPEN,
                                view.labels, gateway.reviews[9], store.now()),
            Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5,
        )

    first = control.preflight(select("rehearsal:first"))
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    planned = control.register_stage(first.round.round_id, current.revision, stage(first))
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    control.seal_completed_planning(first.round.round_id, current.revision)
    paid = request(planned.children[0])
    assert store.reserve(paid).admitted
    register(store, paid)
    failure = returned(store, paid, result=AgentResult(
        True, status="error", exit_reason=ExitReason.AGENT_ERROR,
    ))
    assert store.settle_review_return(settlement(paid), failure).disposition is AccountingDisposition.AVAILABLE

    second = control.preflight(select("rehearsal:second"))
    assert (first.round.sequence, second.round.sequence) == (1, 2)
    assert store.release_dispatch_claim(cfg.repo, "review", -9, OWNER)
    control.seal_abandoned_planning(second.round.round_id)
    abandoned = control.finalize_abandoned_round(second.round.round_id)
    assert isinstance(abandoned, FinalizedReviewRound)
    assert isinstance(abandoned.outcome.disposition, ReviewRoundCancelled)
    with store.transaction() as db:
        before_first = load_review_workflow_row(db, key)
    assert before_first is not None
    assert before_first.execution.state.failures == imported.execution.state.failures

    failed = control.finalize_completed_round(first.round.round_id)
    assert isinstance(failed, FinalizedReviewRound)
    assert isinstance(failed.outcome.disposition, ReviewRoundFailure)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-outcome'").fetchone()[0] == 2
    assert after.execution.last_sequence == 2
    assert after.execution.state.failures == imported.execution.state.failures + 1
    assert store.snapshot(paid.scope).committed.micros == 12
    assert store.snapshot(paid.scope).reserved.micros == 0

    gateway.unavailable = True
    with sqlite3.connect(store.path) as db:
        before_retry = tuple(db.iterdump())
    assert activation.activate(activation_plan, files, links, sql).idempotent
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before_retry
