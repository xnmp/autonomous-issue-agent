"""Explicit isolated workflow authority fixtures, never a production importer."""

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import workflow_scope
from issue_agent.domain.workflow import OpenGate, RearmingGate, ResolveEpisode, WorkflowSnapshot
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)


def resolve_snapshot(number=1, *, generation=2, version=0):
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(number)), generation), version),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1), ResolveEpisode(f"episode-{number}"),
    )


def install_workflow(store, snapshot):
    assert snapshot.intent is None, "fixture only seeds initial episodes"
    scope = workflow_scope(snapshot.revision.fence.key)
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)",
            (scope[0], snapshot.authority.family.value, snapshot.authority.epoch,
             "test-source", "test-inventory", "2026-09-14T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
            (*scope, snapshot.revision.fence.generation, snapshot.revision.version,
             encode_workflow((snapshot.authority, snapshot.episode))),
        )
        for gate in snapshot.gates:
            opened = gate.gate if isinstance(gate, RearmingGate) else gate
            db.execute(
                "INSERT INTO workflow_gates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (opened.gate_id, *scope, opened.opened_generation, type(opened.cause).__name__,
                 "open" if isinstance(gate, OpenGate) else "rearming", encode_workflow(gate)),
            )
