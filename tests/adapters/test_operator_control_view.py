"""Operator resolve reads distinguish inactive, absent, and durable authority."""

from contextlib import closing
from dataclasses import replace

from issue_agent.domain.resolve_rearm import (
    AbsentResolveControl,
    InactiveResolveControl,
    ResolveControlView,
)
from issue_agent.domain.workflow import (
    CancelRearm,
    CancelledRearm,
    ContinueGrantedRearm,
    NoLinkedPr,
    PendingRearm,
    ResolveEpisode,
    WorkflowUnavailable,
)
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm,
    continue_resolve_rearm,
    reconcile_resolve_ceiling,
)
from tests.adapters.test_workflow_control import (
    ACTOR,
    NOW,
    adapter,
    commit,
    control,
    enrich,
    held,
    observation,
    request,
    resolve_snapshot,
    run,
)
from tests.fakes.workflow_authority import install_workflow


def _authority_dump(control) -> tuple[str, ...]:
    with closing(control._connect()) as db:
        return tuple(db.iterdump())


def test_current_point_reads_distinguish_inactive_absent_and_active(control) -> None:
    workflow = adapter(control)
    missing = WorkflowKey("o/r", IssueRef(99))
    assert workflow.resolve_control_view(missing) == InactiveResolveControl(missing)

    state = resolve_snapshot()
    install_workflow(control, state)
    absent = workflow.resolve_control_view(missing)
    assert absent == AbsentResolveControl(missing, state.authority)

    active = workflow.resolve_control_view(state.revision.fence.key)
    assert isinstance(active, ResolveControlView)
    assert active.snapshot == state
    assert active.requested_id is None
    assert active.requested is None
    assert active.observation is None


def test_point_read_is_pure_and_does_not_schedule_or_publish(control) -> None:
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before)).snapshot
    dump = _authority_dump(control)

    first = adapter(control).resolve_control_view(before.revision.fence.key)
    second = adapter(control).resolve_control_view(before.revision.fence.key)

    assert isinstance(first, ResolveControlView)
    assert first == second
    assert first.snapshot == pending
    assert first.requested_id == pending.intent.request_id
    assert first.requested == pending.intent
    assert first.observation is not None
    assert _authority_dump(control) == dump


def test_point_read_can_select_coherent_historical_request(control) -> None:
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before, "request:first")).snapshot
    cancelled = commit(control, cancel_resolve_rearm(
        pending,
        CancelRearm(
            "cancel:first", pending.revision, pending.intent.request_id, "pause",
        ),
        ACTOR,
    )).snapshot
    assert isinstance(cancelled.intent, CancelledRearm)
    continued = commit(control, enrich(continue_resolve_rearm(
        cancelled,
        ContinueGrantedRearm(
            "request:continued", cancelled.revision, pending.intent.request_id,
        ),
        ACTOR,
        observation(cancelled),
        NoLinkedPr(cancelled.revision.fence.key, NOW),
        now=NOW,
        active_attempts=(),
    ))).snapshot
    assert isinstance(continued.intent, PendingRearm)

    view = adapter(control).resolve_control_view(
        before.revision.fence.key, request_id="request:first",
    )

    assert isinstance(view, ResolveControlView)
    assert view.snapshot == continued
    assert view.requested_id == "request:first"
    assert isinstance(view.requested, CancelledRearm)
    assert view.requested.request_id == "request:first"
    assert view.observation is not None


def test_corrupt_activation_or_workflow_is_unavailable_not_inactive(control) -> None:
    state = resolve_snapshot()
    install_workflow(control, state)
    with control.transaction() as db:
        db.execute(
            "UPDATE workflow_authority_epochs SET epoch='corrupt' "
            "WHERE repository='o/r' AND family='resolve'"
        )
    corrupt_epoch = adapter(control).resolve_control_view(state.revision.fence.key)
    assert isinstance(corrupt_epoch, WorkflowUnavailable)
    assert "epoch" in corrupt_epoch.reason

    with control.transaction() as db:
        db.execute(
            "UPDATE workflow_authority_epochs SET epoch=1 "
            "WHERE repository='o/r' AND family='resolve'"
        )
        db.execute(
            "UPDATE workflows SET episode_json='{}' "
            "WHERE repository='o/r' AND target_kind='issue' AND target_number=1"
        )
    corrupt_workflow = adapter(control).resolve_control_view(
        state.revision.fence.key,
    )
    assert isinstance(corrupt_workflow, WorkflowUnavailable)
    assert "workflow authority" in corrupt_workflow.reason


def test_missing_activation_with_retained_workflow_is_unavailable(control) -> None:
    state = resolve_snapshot()
    install_workflow(control, state)
    with control.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs WHERE repository='o/r'")

    result = adapter(control).resolve_control_view(state.revision.fence.key)

    assert isinstance(result, WorkflowUnavailable)
    assert "missing despite retained" in result.reason


def test_missing_activation_with_only_a_retained_rearm_is_unavailable(control) -> None:
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    with closing(control._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM workflow_gates WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_evidence WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_components WHERE repository='o/r'")
        db.execute("DELETE FROM workflows WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_authority_epochs WHERE repository='o/r'")
        db.commit()
        assert db.execute(
            "SELECT 1 FROM workflow_rearms WHERE repository='o/r'"
        ).fetchone() is not None

    result = adapter(control).resolve_control_view(before.revision.fence.key)

    assert isinstance(result, WorkflowUnavailable)
    assert "missing despite retained" in result.reason


def test_missing_activation_with_only_a_bound_run_is_unavailable(control) -> None:
    state = resolve_snapshot()
    install_workflow(control, state)
    run(control, state, name="retained-bound-run")
    with control.transaction() as db:
        db.execute(
            "DELETE FROM workflow_gates WHERE repository='o/r'"
        )
        db.execute(
            "DELETE FROM workflow_rearms WHERE repository='o/r'"
        )
        db.execute(
            "DELETE FROM workflow_evidence WHERE repository='o/r'"
        )
        db.execute("DELETE FROM workflow_components WHERE repository='o/r'")
        db.execute("DELETE FROM workflows WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_authority_epochs WHERE repository='o/r'")

    result = adapter(control).resolve_control_view(state.revision.fence.key)

    assert isinstance(result, WorkflowUnavailable)
    assert "missing despite retained" in result.reason


def test_active_epoch_missing_target_with_retained_rearm_is_unavailable(control) -> None:
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    with closing(control._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM workflow_gates WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_evidence WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_components WHERE repository='o/r'")
        db.execute("DELETE FROM workflows WHERE repository='o/r'")
        db.commit()

    result = adapter(control).resolve_control_view(before.revision.fence.key)

    assert isinstance(result, WorkflowUnavailable)
    assert "retained" in result.reason


def test_active_epoch_missing_target_with_bound_run_is_unavailable(control) -> None:
    state = resolve_snapshot(10)
    install_workflow(control, state)
    run(control, state, name="active-orphan-bound-run")
    with control.transaction() as db:
        db.execute("DELETE FROM workflow_evidence WHERE repository='o/r'")
        db.execute("DELETE FROM workflow_components WHERE repository='o/r'")
        db.execute("DELETE FROM workflows WHERE repository='o/r'")

    prefix_neighbor = adapter(control).resolve_control_view(
        WorkflowKey("o/r", IssueRef(1)),
    )
    result = adapter(control).resolve_control_view(state.revision.fence.key)

    assert isinstance(prefix_neighbor, AbsentResolveControl)
    assert isinstance(result, WorkflowUnavailable)
    assert "retained" in result.reason


def test_active_epoch_missing_target_with_retained_gate_is_unavailable(control) -> None:
    state = reconcile_resolve_ceiling(
        replace(resolve_snapshot(10), episode=ResolveEpisode("episode-10", 3)),
        ceiling=3,
        evidence_ids=("import:gate-10",),
    ).snapshot
    install_workflow(control, state)
    with closing(control._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM workflow_components WHERE repository='o/r' AND target_kind='issue' AND target_number=10")
        db.execute(
            "DELETE FROM workflows WHERE repository='o/r' "
            "AND target_kind='issue' AND target_number=10"
        )
        db.commit()

    prefix_neighbor = adapter(control).resolve_control_view(
        WorkflowKey("o/r", IssueRef(1)),
    )
    result = adapter(control).resolve_control_view(state.revision.fence.key)

    assert isinstance(prefix_neighbor, AbsentResolveControl)
    assert isinstance(result, WorkflowUnavailable)
    assert "retained" in result.reason
