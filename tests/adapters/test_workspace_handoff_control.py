"""Durable SQLite authority for resolve episode workspace handoffs."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from issue_agent.adapters.control_store import workflow_episode_key
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.resolve_workspace import (
    NoPriorResolveAdmission, PriorResolveAdmission,
)
from issue_agent.domain.workflow import PendingRearm, ResolveAdmission, ResolveStage
from issue_agent.domain.workflow_policy import request_resolve_rearm
from tests.adapters.test_workflow_control import (
    ACTOR, NOW, adapter, commit, control, enrich, held, observation, paid_result,
    resolve_snapshot,
)
from tests.fakes.workflow_authority import install_workflow
from tests.fakes.workflow_workspace import workspace_ack, workspace_observation
from issue_agent.domain.workflow import NoLinkedPr, RequestRearm, KeepGuidance


def _observed_request(store, before, *, request_id="request:workspace"):
    workflow = adapter(store)
    with workflow.rearm_authority(before.revision) as authorized:
        assert authorized == before
        observed = workflow.observe_workspace(
            before.binding,
            workspace_observation(before.binding, NOW).base,
            f"fix/{before.revision.fence.key.target.number}-workspace",
        )
    gate = next(g for g in before.gates if getattr(g.cause, "episode_id", None) == before.episode.episode_id)
    return enrich(request_resolve_rearm(
        before,
        RequestRearm(request_id, before.revision, gate.gate_id, KeepGuidance()),
        ACTOR,
        observation(before),
        NoLinkedPr(before.revision.fence.key, NOW),
        now=NOW,
        active_attempts=(),
        workspace=observed,
    ))


def test_workspace_observation_grant_preparation_and_restart_replay(control):
    before = held()
    install_workflow(control, before)
    proposal = _observed_request(control, before)
    pending = commit(control, proposal).snapshot
    assert isinstance(pending.intent, PendingRearm)
    workflow = adapter(control)

    with workflow.workspace_handoff_authority(
        pending.intent.handoff.handoff_id, pending.revision,
    ) as existing:
        assert existing is None
        completed = workflow.complete_workspace_handoff(
            workspace_ack(pending.intent.handoff, NOW, performed_under=pending.binding)
        )

    restarted = adapter(type(control)(control.path, now=lambda: NOW))
    with restarted.workspace_handoff_authority(
        pending.intent.handoff.handoff_id, pending.revision,
    ) as recovered:
        assert recovered == completed
    replay = replace(completed, observed_at=NOW + timedelta(seconds=1))
    assert restarted.complete_workspace_handoff(replay) == completed
    with closing(control._connect()) as db:
        assert db.execute(
            "SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-workspace-completed'"
        ).fetchone()[0] == 1


def test_observation_records_exact_latest_admission_and_branch(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    admitted = paid_result(control, before, "prior-work", ResolveStage.RESOLVE)
    branch = f"fix/{before.revision.fence.key.target.number}-workspace"
    with control.transaction() as db:
        metadata = json.loads(db.execute(
            "SELECT dispatch_id FROM runs WHERE run_id=?", (str(admitted.attempt.run_id),),
        ).fetchone()[0])
        metadata["publication_branch"] = branch
        db.execute(
            "UPDATE runs SET dispatch_id=? WHERE run_id=?",
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")), str(admitted.attempt.run_id)),
        )

    observed = adapter(control).observe_workspace(
        before.binding, workspace_observation(before.binding, NOW).base, branch,
    )
    assert observed.predecessor == PriorResolveAdmission(
        admitted.attempt, observed.predecessor.admission_version,
    )
    with pytest.raises(WorkflowAuthorityError, match="publication provenance"):
        adapter(control).observe_workspace(
            before.binding, observed.base, f"fix/{before.revision.fence.key.target.number}-other",
        )


def test_missing_latest_pointer_is_not_treated_as_admission_absence(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    paid_result(control, before, "retained-work", ResolveStage.AUTHOR)
    with control.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='workflow-work-latest'"
        )
    with pytest.raises(WorkflowAuthorityError, match="pointer is missing"):
        adapter(control).observe_workspace(
            before.binding, workspace_observation(before.binding, NOW).base,
            f"fix/{before.revision.fence.key.target.number}-workspace",
        )


def test_future_admission_version_cannot_authorize_workspace_observation(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    paid_result(control, before, "future-work", ResolveStage.RESOLVE)
    current = adapter(control).load_workflow(before.revision.fence.key)
    with control.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-work-latest'"
        ).fetchone()
        admission = decode_workflow(row[0], ResolveAdmission)
        future = replace(
            admission,
            revision=replace(admission.revision, version=current.revision.version + 1),
        )
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace IN "
            "('workflow-work-latest','workflow-admission')",
            (encode_workflow(future),),
        )
    with pytest.raises(WorkflowAuthorityError, match="future workflow revision"):
        adapter(control).observe_workspace(
            current.binding, workspace_observation(current.binding, NOW).base,
            f"fix/{current.revision.fence.key.target.number}-workspace",
        )


@pytest.mark.parametrize("blocked_by", ["active", "lease", "return"])
def test_workspace_authority_requires_exact_target_quiescence(control, blocked_by):
    before = resolve_snapshot()
    install_workflow(control, before)
    store = adapter(control)
    if blocked_by == "active":
        control.start_run(
            "blocked-run", "dispatch", "RESOLVE", "o/r:fix:deployment", "config",
        )
    else:
        paid_result(control, before, "blocked-run", ResolveStage.RESOLVE)
        before = store.load_workflow(before.revision.fence.key)
        if blocked_by == "lease":
            with control.transaction() as db:
                db.execute(
                    "INSERT INTO leases(run_id,dispatch_id,owner_id,pid,pgid,started_at) "
                    "VALUES ('blocked-run','dispatch',?,1,1,?)",
                    (store.writer.owner_id, NOW.isoformat()),
                )
        else:
            with control.transaction() as db:
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                    "VALUES ('workflow-return','o/r','blocked-run',?,'{}')",
                    (NOW.isoformat(),),
                )
    if blocked_by == "active":
        with control.transaction() as db:
            db.execute(
                "UPDATE runs SET workflow_binding_json=?,workflow_episode_key=? WHERE run_id='blocked-run'",
                (encode_workflow(before.binding), workflow_episode_key(before.binding)),
            )
    with pytest.raises(WorkflowAuthorityError, match="requires drained|return finalized"):
        with store.rearm_authority(before.revision):
            pass


def test_handoff_refuses_stale_revision_and_changed_predecessor(control):
    before = held()
    install_workflow(control, before)
    pending = commit(control, _observed_request(control, before)).snapshot
    workflow = adapter(control)
    with pytest.raises(WorkflowAuthorityError, match="exact current pending"):
        with workflow.workspace_handoff_authority(
            pending.intent.handoff.handoff_id, before.revision,
        ):
            pass
    with control.transaction() as db:
        db.execute(
            "INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version,workflow_binding_json,workflow_episode_key) "
            "VALUES ('later','{}','RESOLVE',?,'completed','config',?,?)",
            (workflow.writer.owner_id, encode_workflow(pending.binding), workflow_episode_key(pending.binding)),
        )
    with pytest.raises(WorkflowAuthorityError, match="predecessor|pointer"):
        with workflow.workspace_handoff_authority(
            pending.intent.handoff.handoff_id, pending.revision,
        ):
            pass


def test_no_prior_observation_is_typed_only_after_indexed_absence(control):
    before = held()
    install_workflow(control, before)
    observed = adapter(control).observe_workspace(
        before.binding, workspace_observation(before.binding, NOW).base,
        f"fix/{before.revision.fence.key.target.number}-workspace",
    )
    assert observed.predecessor == NoPriorResolveAdmission(before.binding)
