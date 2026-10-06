"""Publication stays bound to the paid repair, independently of resolve state."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import ControlStoreError, Lease, ReservationRequest
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import BudgetScope, Money, PriceQuote, ReservationId, RunId
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowFamily, WorkflowKey, WorkflowWriter
from tests.adapters.test_agent_publish import Remote, _supervised_broker, broker, store
from tests.adapters.test_continuation_admission import active, install_secondary
from tests.fakes.review_workflow import ceiling_gate, held
from tests.fakes.workflow_authority import install_workflow


class RepairRemote(Remote):
    crash_after_body = False

    def run(self, argv, cwd):
        result = super().run(argv, cwd)
        if argv[:3] == ("gh", "pr", "edit") and self.crash_after_body:
            raise KeyboardInterrupt
        if argv[:3] == ("gh", "pr", "list"):
            rows = json.loads(result.stdout)
            return replace(result, stdout=json.dumps([{**row, "number": 100} for row in rows]))
        return result


@pytest.fixture
def repair(tmp_path):
    control = store(tmp_path)
    primary = replace(held(key=WorkflowKey("o/r", PullRequestRef(100))), gates=())
    install_workflow(control, primary)
    component = active(primary.revision.fence.key, WorkflowFamily.PR_REPAIR)
    aggregate = replace(primary, pr_repair=component)
    install_secondary(control, aggregate, component)
    selected = cc.select_pr_repair(aggregate)
    purchase = cc.PRRepairPurchase(
        cc.ContinuationCapture(selected.binding, selected.revision, "leg:repair"),
        allowance=cc.RepairAllowance.CI, ceiling=2, branch="fix/42-issue",
    )
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    requested = ReservationRequest(
        ReservationId("reservation:repair"), RunId("repair"),
        json.dumps({"repository": "o/r", "role": "fix", "kind": "FIX_CI",
                    "runtime": "codex",
                    "leg_id": "leg:repair", "number": 100, "issue": 42,
                    "cwd": str(tmp_path), "publication_branch": "fix/42-issue"}),
        BudgetScope("o/r", "daily", "USD", "2026-09-14", now,
                    now + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote("test", "test-card", "1", now, Money("USD", 1)),
        Money("USD", 100), "test-config", "o/r", kind="FIX_CI",
        owner_id="o/r:fix", workflow=selected.binding,
    )
    lease = Lease(requested.run_id, requested.dispatch_id, "o/r:fix", 123, 123, now, 456)
    assert control.reserve(replace(requested, continuations=(purchase,)), lease=lease).admitted
    remote = RepairRemote()
    return control, remote, _supervised_broker(control, remote, tmp_path, requested.run_id), selected


def test_admitted_repair_can_publish_its_existing_branch(repair, tmp_path):
    control, remote, publisher, _ = repair
    result = publisher.publish(("git", "push", "origin", "HEAD:fix/42-issue"), str(tmp_path))
    assert result.code == 0
    assert remote.remote == remote.head
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


def test_repair_can_update_its_existing_pr_body(repair, tmp_path):
    control, remote, publisher, _ = repair
    remote.remote = remote.head
    remote.pr = "https://example.test/pull/100"
    remote.pr_state = "OPEN"
    body = remote.pr_body + "\n\nUpdated evidence for the repair."
    assert publisher.publish(("gh", "pr", "edit", "100", "--body", body), str(tmp_path)).code == 0
    assert "Updated evidence for the repair." in remote.pr_body
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


@pytest.mark.parametrize("change", ["missing-run", "run", "owner", "branch", "cwd", "pr-target", "body-number"])
def test_repair_cannot_borrow_a_different_publication_context(repair, tmp_path, change):
    control, remote, publisher, _ = repair
    remote.remote = remote.head
    remote.pr = "https://example.test/pull/100"
    remote.pr_state = "OPEN"
    command = publisher._command(("gh", "pr", "edit", "100", "--body", remote.pr_body + "\nEvidence."), str(tmp_path))
    payload = dict(command.payload)
    if change == "missing-run":
        payload.pop("run_id")
    elif change == "run":
        payload["run_id"] = "other-run"
    elif change == "owner":
        command = replace(command, executor_identity="o/r:fix:other")
    elif change == "branch":
        payload["branch"] = "fix/43-other"
    elif change == "cwd":
        payload["cwd"] = str(tmp_path / "other")
    elif change == "pr-target":
        command = replace(command, target="pull-request-body:o/r:101")
    else:
        payload["number"] = 101
    with pytest.raises(ControlStoreError):
        control.enqueue_command(replace(command, payload=payload))
    assert remote.effects == []
    assert control.pending_commands() == ()


@pytest.mark.parametrize("change", ["generation", "epoch", "episode", "held"])
def test_queued_repair_cannot_publish_after_its_authority_changes(repair, tmp_path, change):
    control, remote, publisher, _ = repair
    command = publisher._command(("git", "push", "origin", "HEAD:fix/42-issue"), str(tmp_path))
    control.enqueue_command(command)
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot

    with control.transaction() as db:
        aggregate = load_review_workflow_row(db, command.workflow.fence.key)
        if change == "generation":
            after = replace(aggregate, revision=replace(aggregate.revision,
                fence=replace(aggregate.revision.fence, generation=1)))
        elif change == "epoch":
            db.execute("UPDATE workflow_authority_epochs SET epoch=epoch+1 WHERE family='pr_repair'")
            after = None
        elif change == "episode":
            after = replace(aggregate, pr_repair=replace(aggregate.pr_repair,
                identity=replace(aggregate.pr_repair.identity, episode_id="replacement")))
            db.execute("UPDATE workflow_components SET episode_id='replacement' WHERE family='pr_repair'")
        else:
            after = replace(aggregate, pr_repair=replace(aggregate.pr_repair, gates=(ceiling_gate(),)))
        if after is not None:
            persist_workflow_snapshot(db, after)
    assert broker(tmp_path, remote).drain_pending_commands() == 0
    assert remote.effects == []
    assert remote.remote == ""


def test_repair_authority_cannot_create_another_pull_request(repair, tmp_path):
    control, remote, publisher, _ = repair
    remote.remote = remote.head
    with pytest.raises(ControlStoreError, match="PR repair authority"):
        publisher.publish(("gh", "pr", "create", "--title", "Repair", "--body", remote.pr_body), str(tmp_path))
    assert remote.effects == []
    assert control.pending_commands() == ()


@pytest.mark.parametrize("operation", ["push", "body"])
@pytest.mark.parametrize("authority", ["current", "generation", "episode"])
def test_repair_crash_reconciles_proven_effect_without_publishing_twice(repair, tmp_path, operation, authority):
    control, remote, publisher, _ = repair
    if operation == "push":
        remote.crash_after_push = True
        argv = ("git", "push", "origin", "HEAD:fix/42-issue")
    else:
        remote.remote = remote.head
        remote.pr, remote.pr_state = "https://example.test/pull/100", "OPEN"
        remote.crash_after_body = True
        argv = ("gh", "pr", "edit", "100", "--body", remote.pr_body + "\nRepaired evidence.")
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(argv, str(tmp_path))
    assert len(remote.effects) == 1
    assert control.pending_commands()[0].state == "unknown_outcome"
    if authority != "current":
        _move_repair_authority(control, control.pending_commands()[0].command.workflow, authority)
    remote.crash_after_push = remote.crash_after_body = False
    assert broker(tmp_path, remote).drain_pending_commands() == 1
    assert len(remote.effects) == 1
    assert control.pending_commands() == ()


@pytest.mark.parametrize("operation", ["push", "body"])
def test_stale_repair_with_absent_effect_cannot_retry_publication(repair, tmp_path, operation):
    control, remote, publisher, _ = repair
    if operation == "push":
        argv = ("git", "push", "origin", "HEAD:fix/42-issue")
    else:
        remote.remote = remote.head
        remote.pr, remote.pr_state = "https://example.test/pull/100", "OPEN"
        argv = ("gh", "pr", "edit", "100", "--body", remote.pr_body + "\nRepaired evidence.")
    pending = control.enqueue_command(publisher._command(argv, str(tmp_path)))
    control.begin_command(pending.command.command_id)
    _move_repair_authority(control, pending.command.workflow, "generation")
    assert broker(tmp_path, remote).drain_pending_commands() == 0
    assert remote.effects == []
    assert control.pending_commands()[0].state in {"executing", "unknown_outcome"}


def _move_repair_authority(control, binding, change):
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    with control.transaction() as db:
        aggregate = load_review_workflow_row(db, binding.fence.key)
        if change == "generation":
            after = replace(aggregate, revision=replace(aggregate.revision,
                fence=replace(aggregate.revision.fence, generation=1)))
        else:
            after = replace(aggregate, pr_repair=replace(aggregate.pr_repair,
                identity=replace(aggregate.pr_repair.identity, episode_id="replacement")))
            db.execute("UPDATE workflow_components SET episode_id='replacement' WHERE family='pr_repair'")
        persist_workflow_snapshot(db, after)


def test_new_admission_fences_older_repair_in_same_episode(repair, tmp_path):
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    from tests.adapters.test_continuation_admission import money, receipt
    control, remote, publisher, selected = repair
    queued = control.enqueue_command(publisher._command(("git", "push", "origin", "HEAD:fix/42-issue"), str(tmp_path)))
    with control.transaction() as db:
        old_run = db.execute("SELECT * FROM runs WHERE run_id='repair'").fetchone()
        old = cc.select_pr_repair(load_review_workflow_row(db, selected.binding.fence.key))
    admission = receipt(control, RunId("repair"), WorkflowFamily.PR_REPAIR)
    control.settle_pr_repair_result(cc.PRRepairResult(admission, cc.PRRepairState(branch="fix/42-issue",
                                  actual_model=cc.RepairModel(admission, RuntimeName.CODEX, "test")),
                                  money(RunId("repair"))), WorkflowWriter("o/r", "fix", "o/r:fix"))
    with control.transaction() as db:
        old = cc.select_pr_repair(load_review_workflow_row(db, selected.binding.fence.key))
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    dispatch = {**json.loads(old_run["dispatch_id"]), "leg_id": "leg:newer"}
    request = ReservationRequest(
        ReservationId("reservation:newer"), RunId("newer"), json.dumps(dispatch),
        BudgetScope("o/r", "daily", "USD", "2026-09-14", now, now + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote("test", "test-card", "1", now, Money("USD", 1)), Money("USD", 100), "test-config", "o/r",
        kind="FIX_CI", owner_id="o/r:fix", workflow=old.binding,
        continuations=(cc.PRRepairPurchase(cc.ContinuationCapture(old.binding, old.revision, "leg:newer"),
                         allowance=cc.RepairAllowance.CI, ceiling=2, branch="fix/42-issue"),),
    )
    lease = Lease(request.run_id, request.dispatch_id, "o/r:fix", 123, 123, now, 456)
    assert control.reserve(request, lease=lease).admitted
    with control.transaction() as db:
        newer = cc.select_pr_repair(load_review_workflow_row(db, selected.binding.fence.key))
    assert newer.binding == old.binding
    assert broker(tmp_path, remote).drain_pending_commands() == 0
    assert remote.effects == []
    assert control.pending_commands()[0].command.command_id == queued.command.command_id
