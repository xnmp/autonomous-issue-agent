"""An issue stored as an inert shell has no resolve gates, so a linked pull request is never held by it."""

from tests.domain.builders import observed_pr

from dataclasses import replace
import json

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_linkage import read_review_linked_issue, review_linked_issue_holds
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, linked_issue_workflow_holds, workflow_scope,
)
from issue_agent.domain.review_linkage import ObservedLinkedIssue
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import WorkflowFamily, WorkflowFence, WorkflowRevision
from issue_agent.domain.world import PRView
from tests.adapters.test_continuation_admission import active, install_secondary
from tests.adapters.test_review_execution_admission import request as child_request
from tests.adapters.test_review_execution_control import OWNER, assert_unpaid, current, linked_selection, rig  # noqa: F401
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_linkage import IssueGateway, review_reservation
from tests.fakes.review_workflow import ISSUE_KEY, NOW, PR_KEY
from tests.domain.test_review_workflow_policy import fresh
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

LINKED = observed_pr(9, closing_issue=7)


def insert_shell(store, *, generation=0, version=0, stored=None):
    """Replace the issue row with a shell in a repository whose resolve family is active, as production rows are; `stored` overrides the payload revision."""
    revision = WorkflowRevision(WorkflowFence(ISSUE_KEY, generation), version)
    shell = IssueWorkflowShell(stored or revision)
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   ("o/r", "resolve", 1, "test-source", "test-inventory", NOW.isoformat()))
        for table in ("workflow_components", "workflows"):
            db.execute(f"DELETE FROM {table} WHERE repository=? AND target_kind=? AND target_number=?", workflow_scope(ISSUE_KEY))
        db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                   (*workflow_scope(ISSUE_KEY), generation, version, encode_workflow(shell)))
    return shell


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    return store


def holds_of(store):
    with store.transaction() as db:
        return linked_issue_workflow_holds(db, ISSUE_KEY)


def test_shell_issue_contributes_no_holds_and_does_not_raise(control):
    insert_shell(control)
    assert holds_of(control) == ()


def test_shell_issue_with_secondary_continuity_contributes_no_holds(control):
    shell = insert_shell(control)
    steward = active(ISSUE_KEY, WorkflowFamily.STEWARD)
    install_secondary(control, replace(shell, steward=steward), steward)
    assert holds_of(control) == ()


def test_shell_issue_at_a_later_revision_contributes_no_holds(control):
    insert_shell(control, generation=3, version=5)
    assert holds_of(control) == ()


def test_pull_request_linking_a_shell_issue_reads_as_unheld(control):
    insert_shell(control)
    observed = read_review_linked_issue(control, IssueGateway(), PR_KEY, LINKED)
    assert isinstance(observed, ObservedLinkedIssue) and observed.workflow_holds == ()
    assert review_linked_issue_holds(control, IssueGateway(), PR_KEY, LINKED) == ()


def test_absent_row_still_contributes_no_holds(control):
    assert holds_of(control) == ()


def test_gated_resolve_row_still_holds(control):
    from tests.adapters.test_review_linkage import held_issue
    install_workflow(control, held_issue())
    assert holds_of(control) == ("unresolved workflow gates",)


def test_shell_with_a_mismatched_revision_is_local_corruption(control):
    insert_shell(control, generation=1, version=1,
                 stored=WorkflowRevision(WorkflowFence(ISSUE_KEY, 1), 2))
    with pytest.raises(WorkflowAuthorityError, match="revision"):
        holds_of(control)
    with pytest.raises(WorkflowAuthorityError, match="revision"):
        read_review_linked_issue(control, IssueGateway(), PR_KEY, LINKED)


def test_shell_row_that_also_carries_a_resolve_component_is_local_corruption(control):
    insert_shell(control)
    install_workflow_component = resolve_snapshot(7)
    from issue_agent.adapters.workflow_rows import insert_workflow_component
    with control.transaction() as db:
        db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   ("o/r", "resolve", 1, "test-source", "test-inventory", NOW.isoformat()))
        insert_workflow_component(db, ISSUE_KEY, install_workflow_component.authority,
                                  install_workflow_component.episode.episode_id)
    with pytest.raises(WorkflowAuthorityError, match="unexpected resolve authority"):
        holds_of(control)


def test_shell_naming_an_unindexed_secondary_component_is_local_corruption(control):
    shell = insert_shell(control)
    steward = active(ISSUE_KEY, WorkflowFamily.STEWARD)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json=? WHERE repository=? AND target_kind=? AND target_number=?",
                   (encode_workflow_episodes(replace(shell, steward=steward)), *workflow_scope(ISSUE_KEY)))
    with pytest.raises(WorkflowAuthorityError):
        holds_of(control)


def test_paid_review_reservation_for_a_pull_request_linking_a_shell_issue_is_admitted(control):
    install_workflow(control, fresh())
    insert_shell(control)
    assert control.reserve(review_reservation()).admitted


def test_paid_review_reservation_still_refuses_an_inconsistent_linked_shell(control):
    install_workflow(control, fresh())
    insert_shell(control, generation=1, version=1,
                 stored=WorkflowRevision(WorkflowFence(ISSUE_KEY, 1), 2))
    assert not control.reserve(review_reservation()).admitted
    assert control.active_reservation_runs() == ()


def test_review_preflight_for_a_pull_request_linking_a_shell_issue_is_admitted(rig):
    store, _, _, _, control = rig
    selection = linked_selection(rig)
    insert_shell(store)
    plan = control.preflight(selection)
    assert plan.round.sequence == 1
    assert_unpaid(store)


def test_review_preflight_still_refuses_an_inconsistent_linked_shell(rig):
    store, _, _, _, control = rig
    selection = linked_selection(rig)
    insert_shell(store, generation=1, version=1, stored=WorkflowRevision(WorkflowFence(ISSUE_KEY, 1), 2))
    with pytest.raises(WorkflowAuthorityError, match="revision"):
        control.preflight(selection)
    assert_unpaid(store)


def test_review_child_for_a_pull_request_linking_a_shell_issue_is_admitted(rig):
    store, _, _, _, control = rig
    selection = linked_selection(rig)
    insert_shell(store)
    plan = control.preflight(selection)
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed)
    child = proposed.children[0]
    base = child_request(child)
    dispatch = {**json.loads(base.dispatch_id), "claim_target": 7, "issue": 7}
    assert store.reserve(replace(base, dispatch_id=json.dumps(dispatch))).admitted
