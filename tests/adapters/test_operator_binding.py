"""Operator epoch/episode fences are checked inside deployment authority."""

from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowRevisionConflict
from issue_agent.domain.workflow import CancelRearm, CancelledRearm, WorkflowCommitted, WorkflowConflict
from issue_agent.domain.workflow_identity import IssueRef
from tests.adapters.test_workflow_control import ACTOR, adapter, commit, control, held, request, resolve_snapshot
from tests.fakes.workflow_authority import install_workflow


def different_binding(binding, difference):
    if difference == "epoch":
        return replace(binding, authority=replace(binding.authority, epoch=binding.authority.epoch + 1))
    if difference == "episode":
        return replace(binding, episode_id="another-episode")
    if difference == "generation":
        return replace(binding, fence=replace(binding.fence, generation=binding.fence.generation + 1))
    if difference == "target":
        return replace(binding, fence=replace(binding.fence, key=replace(binding.fence.key, target=IssueRef(99))))
    raise AssertionError(difference)


def authority_dump(control):
    with closing(control._connect()) as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize("difference", ["epoch", "episode"])
def test_wrong_epoch_or_episode_cannot_enter_rearm_authority_at_identical_revision(control, difference):
    state = resolve_snapshot()
    install_workflow(control, state)
    wrong = different_binding(state.binding, difference)
    assert wrong.fence == state.binding.fence
    before = authority_dump(control)
    with pytest.raises(WorkflowRevisionConflict) as error:
        with adapter(control).rearm_authority(state.revision, binding=wrong):
            pytest.fail("wrong binding acquired Git/remote authority")
    assert error.value.current == state.revision
    assert authority_dump(control) == before


@pytest.mark.parametrize("difference", ["generation", "target"])
def test_mismatched_fence_never_enters_authority(control, difference):
    state = resolve_snapshot()
    install_workflow(control, state)
    before = authority_dump(control)
    with pytest.raises(ValueError):
        with adapter(control).rearm_authority(state.revision, binding=different_binding(state.binding, difference)):
            pytest.fail("foreign fence acquired authority")
    assert authority_dump(control) == before


@pytest.mark.parametrize("malformed", [{}, "epoch:1", 1, False])
def test_malformed_binding_never_enters_authority(control, malformed):
    state = resolve_snapshot()
    install_workflow(control, state)
    before = authority_dump(control)
    with pytest.raises(ValueError):
        with adapter(control).rearm_authority(state.revision, binding=malformed):
            pytest.fail("malformed binding acquired authority")
    assert authority_dump(control) == before


def test_exact_binding_yields_current_authority_without_writes(control):
    state = resolve_snapshot()
    install_workflow(control, state)
    before = authority_dump(control)
    with adapter(control).rearm_authority(state.revision, binding=state.binding) as acquired:
        assert acquired == state
    assert authority_dump(control) == before


def test_binding_is_checked_after_barrier_acquisition_not_only_before_it(control, monkeypatch):
    state = resolve_snapshot()
    install_workflow(control, state)
    workflow = adapter(control)
    barrier = workflow._rearm_barriers
    changed = replace(state, episode=replace(state.episode, episode_id="replacement-episode"))
    @contextmanager
    def changed_while_acquiring():
        with barrier():
            with control.transaction() as db:
                db.execute("UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='issue' AND target_number=1",
                           (encode_workflow((changed.authority, changed.episode)),))
            yield
    monkeypatch.setattr(workflow, "_rearm_barriers", changed_while_acquiring)
    with pytest.raises(WorkflowRevisionConflict):
        with workflow.rearm_authority(state.revision, binding=state.binding):
            pytest.fail("pre-lock binding observation authorized a replacement episode")
    assert workflow.load_workflow(state.revision.fence.key) == changed


@pytest.mark.parametrize("difference", ["epoch", "episode"])
def test_cancellation_binding_fences_new_and_replayed_cancellation(control, difference):
    state = held()
    install_workflow(control, state)
    accepted = commit(control, request(state)).snapshot
    command = CancelRearm("cancel-binding", accepted.revision, accepted.intent.request_id, "pause")
    wrong = different_binding(accepted.binding, difference)
    before = authority_dump(control)
    assert isinstance(adapter(control).cancel_rearm(command, ACTOR, binding=wrong), WorkflowConflict)
    assert authority_dump(control) == before
    cancelled = adapter(control).cancel_rearm(command, ACTOR, binding=accepted.binding)
    assert isinstance(cancelled, WorkflowCommitted)
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    after = authority_dump(control)
    replay = adapter(control).cancel_rearm(command, ACTOR, binding=accepted.binding)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert isinstance(adapter(control).cancel_rearm(command, ACTOR, binding=wrong), WorkflowConflict)
    assert authority_dump(control) == after
