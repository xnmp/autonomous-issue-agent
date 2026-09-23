"""Workspace acknowledgement is necessary, but not sufficient, for paid work."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.control_store import ControlStoreError
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_split import SplitContext, SplitSelection
from issue_agent.domain.workflow import (
    CancelRearm, ContinueGrantedRearm, NoLinkedPr, ResolveEpisode,
    ResolveResultKind, ResolveStage,
)
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, complete_resolve_rearm, continue_resolve_rearm,
)
from tests.adapters.test_workflow_admission import request as paid_request
from tests.adapters.test_workflow_control import (
    ACTOR, NOW, WRITER, adapter, command_row, commit, control, enrich, evidence,
    observation, paid_result, prepare_workspace, resolve_snapshot,
)
from tests.adapters.test_workspace_handoff_control import _observed_request
from tests.fakes.workflow_authority import install_workflow
from tests.fakes.workflow_workspace import workspace_ack


def pending_rearm(store, *, prior_stage=ResolveStage.RESOLVE):
    before = replace(resolve_snapshot(), episode=ResolveEpisode("old-episode", 2))
    install_workflow(store, before)
    returned = paid_result(store, before, "prior-work", prior_stage)
    exhausted = adapter(store).record_attempt(
        replace(returned, kind=ResolveResultKind.COUNTED_REQUEUE)
    ).snapshot
    return commit(store, _observed_request(store, exhausted)).snapshot


def complete(store, pending, acknowledgement):
    command = pending.intent.required_commands[0]
    store.begin_command(command)
    store.record_effect(EffectReceipt(command, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    evidence(store, pending)
    return commit(store, complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})),
        NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(command,),
        evidence_ids=("observation:1",), now=NOW, workspace=acknowledgement,
    )).snapshot


def test_projection_waits_without_attempts_until_persisted_workspace_ack(control):
    pending = pending_rearm(control)
    command = pending.intent.required_commands[0]
    diagnostics = []
    assert control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id,
                                     diagnose=diagnostics.append) == ()
    assert diagnostics == []
    with pytest.raises(ControlStoreError, match="workspace"):
        control.begin_command(command)
    assert command_row(control, command)["attempts"] == 0
    prepare_workspace(control, pending)
    assert tuple(item.command.command_id for item in control.runnable_commands(
        repository="o/r", executor_identity=WRITER.owner_id, diagnose=diagnostics.append,
    )) == (command,)


def test_acknowledged_workspace_does_not_admit_paid_work_before_remote_completion(control):
    pending = pending_rearm(control)
    prepare_workspace(control, pending)
    assert not control.reserve(paid_request("too-early", binding=pending.binding)).admitted


@pytest.mark.parametrize("prior_stage", [ResolveStage.RESOLVE, ResolveStage.AUTHOR])
@pytest.mark.parametrize("next_stage", ["RESOLVE", "AUTHOR"])
def test_completed_handoff_bridges_retained_predecessor_then_advances_normally(control, prior_stage, next_stage):
    pending = pending_rearm(control, prior_stage=prior_stage)
    with control.transaction() as db:
        old_pointer = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-work-latest'").fetchone()[0]
    acknowledgement = prepare_workspace(control, pending)
    completed = complete(control, pending, acknowledgement)
    with control.transaction() as db:
        assert db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-work-latest'").fetchone()[0] == old_pointer
    split = SplitContext(completed.intent.handoff.branch, SplitSelection("codex", "model")) if next_stage == "AUTHOR" else None
    first = paid_request("new-work", binding=completed.binding, stage=next_stage, split_input=split)
    assert not control.reserve(first).admitted
    assert control._reserve(first, workspace_verified=acknowledgement).admitted
    assert adapter(control).load_workflow(completed.revision.fence.key).revision.version == completed.revision.version + 1
    with control.transaction() as db:
        assert db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-work-latest'").fetchone()[0] != old_pointer
        assert db.execute("SELECT count(*) FROM runs WHERE run_id='prior-work'").fetchone()[0] == 1


@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_completed_handoff_without_matching_durable_ack_is_unavailable(control, corruption):
    pending = pending_rearm(control)
    completed = complete(control, pending, prepare_workspace(control, pending))
    with control.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-workspace-completed'")
        else:
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-workspace-completed'")
    with pytest.raises(WorkflowAuthorityError, match="acknowledgement"):
        adapter(control).load_workflow(completed.revision.fence.key)
    assert not control.reserve(paid_request("corrupt-authority", binding=completed.binding)).admitted


def test_ack_receipt_failure_rolls_back_and_remains_held(control):
    pending = pending_rearm(control)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER reject_ack BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='workflow-workspace-completed' "
                   "BEGIN SELECT RAISE(ABORT,'ack interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="ack interrupted"):
        prepare_workspace(control, pending)
    with adapter(control).workspace_handoff_authority(pending.intent.handoff.handoff_id, pending.revision) as ack:
        assert ack is None
    with pytest.raises(ControlStoreError, match="workspace"):
        control.begin_command(pending.intent.required_commands[0])


def test_cancel_continue_reuses_original_preparation_receipt_without_new_allowance(control):
    pending = pending_rearm(control)
    ack = prepare_workspace(control, pending)
    stopped = commit(control, cancel_resolve_rearm(
        pending, CancelRearm("cancel", pending.revision, pending.intent.request_id, "pause"), ACTOR,
    )).snapshot
    assert not control.reserve(paid_request("stopped", binding=stopped.binding)).admitted
    resumed = commit(control, enrich(continue_resolve_rearm(
        stopped, ContinueGrantedRearm("continue", stopped.revision, stopped.intent.request_id),
        ACTOR, observation(stopped), NoLinkedPr(stopped.revision.fence.key, NOW),
        now=NOW, active_attempts=(),
    ))).snapshot
    assert resumed.episode == pending.episode
    with adapter(control).workspace_handoff_authority(resumed.intent.handoff.handoff_id, resumed.revision) as retained:
        assert retained == ack
    assert adapter(control).complete_workspace_handoff(
        workspace_ack(resumed.intent.handoff, NOW, performed_under=resumed.binding)
    ) == ack
    completed = complete(control, resumed, ack)
    assert completed.episode == pending.episode
    first = paid_request("after-continue", binding=completed.binding)
    assert not control.reserve(first).admitted
    assert control._reserve(first, workspace_verified=ack).admitted
