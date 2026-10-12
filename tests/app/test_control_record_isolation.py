"""A damaged target's file must not buy work or starve another target."""

from tests.domain.builders import observed_pr

from datetime import datetime, timezone

from issue_agent.adapters.store import Store
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app.roles import _fix_state, _review_unproductive_rounds
from issue_agent.domain.actions import RunAgent
from issue_agent.domain.fix import FixConfig, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.review import BoundReviewState, ReviewConfig, decide_bound_review
from tests.fakes.review_authority import review_binding
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.world import IssueView, PRView, WorldState
from issue_agent.domain.workflow import WorkflowAbsent, WorkflowMigrationRequired, WorkflowSnapshot, WorkflowUnavailable
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery
import pytest


def workflow_control(tmp_path, numbers=(1, 2)):
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    for number in numbers:
        install_workflow(control, with_issue_recovery(resolve_snapshot(number)))
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


@pytest.mark.parametrize("legacy", ["unreadable", "present", "absent"])
def test_retired_automerge_hint_cannot_suppress_sql_authorized_work(tmp_path, legacy):
    from tests.domain.builders import issue, pr, world
    store = Store(str(tmp_path))
    _, workflows = workflow_control(tmp_path, numbers=(1,))
    marker = tmp_path / "pr-9.automerge"
    if legacy == "unreadable":
        marker.mkdir()
    elif legacy == "present":
        marker.write_text("retained historical hint")
    observed = world(issues=(issue(1, "ai-ready", updated_at="current-issue"),),
                     prs=(pr(9, state="CLOSED", closing_issue=1),))
    state = _fix_state(store, observed, workflow_control=workflows)
    actions = decide_fix(observed, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert [action.number for action in actions if isinstance(action, RunAgent)] == [1]
    assert not state.control_errors


@pytest.mark.parametrize("table", ["workflows", "workflow_evidence"])
def test_discovery_write_failure_rolls_back_and_does_not_starve_existing_peer(tmp_path, table):
    store = Store(str(tmp_path))
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from tests.fakes.workflow_authority import seed_family_epoch, with_issue_recovery
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    with control.transaction() as db:
        for family in (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR):
            seed_family_epoch(db, "o/r", family, control.now())
    existing = resolve_snapshot(2)
    install_workflow(control, with_issue_recovery(existing))
    workflows = SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", "o/r:fix:fixture"), resolve_ceiling=3)
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


from tests.adapters.test_review_execution_control import rig


def test_corrupt_current_review_allowance_holds_only_affected_pr(rig):
    control, _, _, _, _ = rig
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='oops' WHERE target_kind='pr' AND target_number=9")
    world = WorldState(prs=tuple(
        observed_pr(n, labels=frozenset({Label.AI_PR_OPEN}), head_ref_oid=f"h{n}")
        for n in (9, 10)
    ))
    errors = {}
    rounds = _review_unproductive_rounds(control, "o/r", world, errors)
    state = BoundReviewState(owed={n: (AgentKind.REVIEW, review_binding(n)) for n in (9, 10)},
                             unproductive_rounds=rounds, control_errors=errors)
    actions = decide_bound_review(world, ReviewConfig(review_bot="review-bot"), state)
    assert [a.number for a in actions if isinstance(a, RunAgent)] == [10]
    assert errors[9]

