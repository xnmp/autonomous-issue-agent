"""Isolated review/PR-repair workflow authority fixtures, never live authority."""

from tests.domain.builders import observed_pr

import dataclasses
import tempfile
from pathlib import Path

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery


def install_pr_repair_authority(gateway, store):
    from issue_agent.domain.continuation_components import InactivePRRepair
    from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowFamily, WorkflowKey
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, workflow_scope
    from tests.fakes.review_workflow import held
    from tests.adapters.test_continuation_admission import active, install_secondary
    for number in gateway.prs:
        key = WorkflowKey("o/r", PullRequestRef(number))
        with store._workflow_db.transaction() as db:
            exists = db.execute("SELECT 1 FROM workflows WHERE repository=? AND target_kind=? AND target_number=?",
                                workflow_scope(key)).fetchone()
            primary = load_review_workflow_row(db, key) if exists is not None else None
        if primary is None:
            primary = dataclasses.replace(held(key=key), gates=())
            install_workflow(store._workflow_db, primary)
        elif not isinstance(primary.pr_repair, InactivePRRepair):
            continue
        component = active(key, WorkflowFamily.PR_REPAIR)
        install_secondary(store._workflow_db, dataclasses.replace(primary, pr_repair=component), component)


def pr_repair_state(store, pr=100):
    from issue_agent.domain.workflow_identity import PullRequestRef
    return store._workflow_authority.read_pr_repair(WorkflowKey("o/r", PullRequestRef(pr))).component.state


def issue_recovery_state(store, issue=5):
    selected = store._workflow_authority.read_issue_recovery(WorkflowKey("o/r", IssueRef(issue)))
    return selected.component.state


def review_retry_pair(store, issue=5):
    debt = issue_recovery_state(store, issue).review_failures
    return debt.review_at, debt.count


def seed_review_retry(store, at, count, issue=5):
    from issue_agent.domain.continuation_components import ReviewRetryState
    from issue_agent.adapters.workflow_rows import load_issue_aggregate, persist_workflow_snapshot
    with store._workflow_db.transaction() as db:
        key = WorkflowKey("o/r", IssueRef(issue))
        current = load_issue_aggregate(db, key)
        state = dataclasses.replace(current.issue_recovery.state, review_failures=ReviewRetryState(at, count))
        persist_workflow_snapshot(db, dataclasses.replace(current, issue_recovery=dataclasses.replace(current.issue_recovery, state=state)))


def attach_workflow_authority(
    gw, store, *, resolve_ceiling=20, pr_repair=False
):
    # Every gateway instance models a fresh authoritative observation. Some
    # tests deliberately reuse one control store across several instances, so
    # normalization cannot be coupled to first-time database initialization.
    for number, issue in tuple(gw.issues.items()):
        if not issue.updated_at:
            gw.issues[number] = dataclasses.replace(
                issue, updated_at=f"fixture-revision-{number}"
            )
    if not hasattr(store, "_workflow_db"):
        for number in getattr(gw, "_pr_for_issue", {}).values():
            gw.prs.setdefault(number, observed_pr(number, head_ref_oid="head-sha"))
        object.__setattr__(store, "_workflow_dir", tempfile.TemporaryDirectory(prefix="workflow-test-"))
        object.__setattr__(store, "_workflow_db", SQLiteControlStore(str(Path(store._workflow_dir.name) / "control.sqlite")))
        store._workflow_db.initialize()
        seeded = getattr(store, "_workflow_seed", None)
        from issue_agent.domain.refs import issue_from_branch
        numbers = set(gw.issues) | {p.closing_issue for p in gw.prs.values() if p.closing_issue}
        numbers |= {number for p in gw.prs.values() if (number := issue_from_branch(p.head_ref_name)) is not None}
        numbers.add(5)
        for number in numbers:
            primary = seeded if seeded is not None and seeded.revision.fence.key.target.number == number else resolve_snapshot(number)
            install_workflow(store._workflow_db, with_issue_recovery(primary))
        object.__setattr__(store, "_workflow_authority", SQLiteWorkflowControl(
            store._workflow_db,
            WorkflowWriter("o/r", "fix", "o/r:fix:fixture"),
            resolve_ceiling=resolve_ceiling,
        ))
    if pr_repair:
        install_pr_repair_authority(gw, store)
    return store._workflow_authority
