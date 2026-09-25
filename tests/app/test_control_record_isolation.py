"""A damaged target's file must not buy work or starve another target."""

from datetime import datetime, timezone

from issue_agent.adapters.store import Store
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app.roles import _fix_state, _review_state
from issue_agent.domain.actions import RunAgent
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewConfig, decide_review
from issue_agent.domain.world import IssueView, PRView, WorldState
from issue_agent.domain.workflow import WorkflowAbsent, WorkflowMigrationRequired, WorkflowSnapshot, WorkflowUnavailable
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot
import pytest


def workflow_control(tmp_path, numbers=(1, 2)):
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    for number in numbers:
        install_workflow(control, resolve_snapshot(number))
    return control, SQLiteWorkflowControl(
        control, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"), resolve_ceiling=3,
    )


def test_corrupt_resolve_authority_holds_only_affected_issue(tmp_path):
    store = Store(str(tmp_path))
    control, workflows = workflow_control(tmp_path)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='oops' WHERE target_number=1")
    world = WorldState(issues=tuple(
        IssueView(n, labels=frozenset({Label.AI_READY}), updated_at=f"r{n}")
        for n in (1, 2)
    ))
    state = _fix_state(store, world, workflow_control=workflows)
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
    assert isinstance(state.resolve_workflows[1], WorkflowUnavailable)


@pytest.mark.parametrize("table", ["workflows", "workflow_evidence"])
def test_discovery_write_failure_rolls_back_and_does_not_starve_existing_peer(tmp_path, table):
    store = Store(str(tmp_path))
    control, workflows = workflow_control(tmp_path, numbers=(2,))
    missing_key = resolve_snapshot(1).revision.fence.key
    peer = workflows.load_workflow(resolve_snapshot(2).revision.fence.key)
    with control.transaction() as db:
        db.execute(f"CREATE TRIGGER reject_discovery BEFORE INSERT ON {table} "
                   "WHEN NEW.target_number=1 BEGIN SELECT RAISE(ABORT, 'target 1 discovery rejected'); END")
    world = WorldState(issues=tuple(
        IssueView(n, labels=frozenset({Label.AI_READY}), updated_at=f"r{n}") for n in (1, 2)
    ), now=datetime.now(timezone.utc).isoformat())

    state = _fix_state(store, world, workflow_control=workflows)

    failed = state.resolve_workflows[1]
    assert isinstance(failed, WorkflowUnavailable)
    assert "discovery rejected" in failed.reason
    assert state.resolve_workflows[2] == peer
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert [action.number for action in actions if isinstance(action, RunAgent)] == [2]
    assert isinstance(workflows.load_workflow(missing_key), WorkflowAbsent)
    with control.transaction() as db:
        db.execute("DROP TRIGGER reject_discovery")

    recovered = _fix_state(store, world, workflow_control=workflows)
    initial = recovered.resolve_workflows[1]
    assert isinstance(initial, WorkflowSnapshot)
    assert initial.episode.failures == 0 and initial.revision.version == 0
    assert recovered.resolve_workflows[2] == peer
    assert _fix_state(store, world, workflow_control=workflows).resolve_workflows[1] == initial


def test_selected_resolve_ignores_retired_file_authority(tmp_path):
    store = Store(str(tmp_path))
    _, workflows = workflow_control(tmp_path)
    (tmp_path / "issue-1.resolve-retries").write_text("oops")
    (tmp_path / "issue-1.session").write_text("legacy-session")
    world = WorldState(issues=(IssueView(1, labels=frozenset({Label.AI_READY}), updated_at="r"),))
    state = _fix_state(store, world, workflow_control=workflows)
    assert not state.control_errors
    assert not state.sessions
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [1]


def test_missing_activation_is_a_hold_not_a_fresh_allowance(tmp_path):
    _, workflows = workflow_control(tmp_path, numbers=())
    world = WorldState(issues=(IssueView(1, labels=frozenset({Label.AI_READY}), updated_at="r"),))
    state = _fix_state(Store(str(tmp_path)), world, workflow_control=workflows)
    assert isinstance(state.resolve_workflows[1], WorkflowMigrationRequired)
    assert state.resolve_workflows[1].reason == "resolve_migration_required"
    assert not decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)


def test_corrupt_review_counter_holds_only_affected_pr(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue--1.infra-deaths").write_text("oops")
    world = WorldState(prs=tuple(
        PRView(n, labels=frozenset({Label.AI_PR_OPEN}), head_ref_oid=f"h{n}")
        for n in (1, 2)
    ))
    state = _review_state(store, world)
    actions = decide_review(world, ReviewConfig(review_bot="review-bot"), state)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
    assert "issue--1.infra-deaths" in state.control_errors[1]


@pytest.mark.parametrize("raw", [
    '{"target":"ISSUE","number":1,"reservation_micros":1e309,"scope":"issue","cap_micros":100}',
    '{"target":"ISSUE","number":1,"reservation_micros":true,"scope":"issue","cap_micros":100}',
])
def test_structured_control_corruption_does_not_starve_peer(tmp_path, raw):
    (tmp_path / "issue-1.budget-block").write_text(raw)
    world = WorldState(issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY}), updated_at="r")
                                  for n in (1, 2)))
    _, workflows = workflow_control(tmp_path)
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"),
                         _fix_state(Store(str(tmp_path)), world, workflow_control=workflows), fixer_busy=False)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [2]
