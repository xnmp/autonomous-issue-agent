"""Explicit isolated workflow authority fixtures, never a production importer."""

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import insert_workflow_component, workflow_scope, encode_workflow_episodes
from issue_agent.domain.workflow import OpenGate, RearmingGate, ResolveEpisode, WorkflowSnapshot
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)
from dataclasses import replace
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.workflow_identity import ContinuationComponentIdentity


def with_issue_recovery(snapshot, *, state=None):
    component = cc.ActiveIssueRecovery(
        ContinuationComponentIdentity(snapshot.revision.fence.key, FamilyAuthority(WorkflowFamily.ISSUE_RECOVERY, 1), "fixture-issue-reply"),
        state or cc.IssueRecoveryState(),
    )
    return replace(snapshot, issue_recovery=component)


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
             encode_workflow_episodes(snapshot)),
        )
        insert_workflow_component(db, snapshot.revision.fence.key, snapshot.authority, snapshot.episode.episode_id)
        if isinstance(getattr(snapshot, "issue_recovery", None), cc.ActiveIssueRecovery):
            component = snapshot.issue_recovery
            db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)", (scope[0], "issue_recovery", 1, "test-source", "test-inventory", "2026-10-01T00:00:00Z"))
            insert_workflow_component(db, snapshot.revision.fence.key, component.identity.authority, component.identity.episode_id)
        for gate in snapshot.gates:
            opened = gate.gate if isinstance(gate, RearmingGate) else gate
            db.execute(
                "INSERT INTO workflow_gates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (opened.gate_id, *scope, opened.opened_generation, type(opened.cause).__name__,
                 "open" if isinstance(gate, OpenGate) else "rearming", encode_workflow(gate)),
            )


def seed_family_epoch(db, repository: str, family: WorkflowFamily, activated_at, *, epoch: int = 1) -> None:
    """A family's authority epoch and plan evidence, as the deleted offline
    activation left them.

    Seed it before a target's first observation, which then initializes the
    family's state through the live path.
    """
    import hashlib
    digest = lambda label: hashlib.sha256(f"fixture {family.value} {label}".encode()).hexdigest()  # noqa: E731
    db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
               (repository, family.value, epoch, digest("source"), digest("inventory"), activated_at.isoformat()))
    db.execute("INSERT INTO workflow_activation_evidence VALUES (?,?,?,?,?)",
               (repository, family.value, epoch, 0, digest("plan")))


def retained_issue_file(store, issue: int, suffix: str) -> str:
    """Observe retained fixture bytes without consulting runtime continuity."""
    from pathlib import Path
    if not hasattr(store, "issue_path"):
        field = {"session": "sessions", "model": "models", "branch": "branches",
                 "resumed-at": "resumed_at", "phase": "phases"}[suffix]
        return getattr(store, field, {}).get(issue, "")
    path = Path(store.issue_path(issue, suffix))
    return path.read_text() if path.exists() else ""


def seed_retained_issue_file(store, issue: int, suffix: str, value: str) -> None:
    """Seed stopped-import evidence, including deliberately contradictory files."""
    from pathlib import Path
    if not hasattr(store, "issue_path"):
        field = {"session": "sessions", "model": "models", "branch": "branches",
                 "resumed-at": "resumed_at", "phase": "phases"}[suffix]
        store.__dict__.setdefault(field, {})[issue] = value
        return
    path = Path(store.issue_path(issue, suffix))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
