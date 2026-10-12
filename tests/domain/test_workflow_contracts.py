from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import (
    AuthorizedOperatorCommand, CancelRearm, KeepGuidance, OperatorActor,
    RequestRearm, ResolveAttemptResult, ResolveEpisode, ResolveResultKind,
    WorkflowChanged, WorkflowMutation, WorkflowSnapshot,
    ResolveRearmAcceptance, ResolveRearmPreflight, ObservedIssue, NoLinkedPr,
    FreshResolve, ResolveAdmission, ResolveSession, ResolveStage,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey,
    WorkflowRevision,
)
from tests.fakes.workflow_workspace import workspace_observation


def snapshot(number=7):
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(number)), 0), 0),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1), ResolveEpisode("episode"),
    )


@pytest.mark.parametrize("control", [chr(value) for value in range(32)])
def test_episode_rejects_identifier_that_cannot_be_carried_to_admission(control):
    with pytest.raises(ValueError):
        ResolveEpisode(f"episode{control}7")


@pytest.mark.parametrize("identifier", ["x" * 512, "épisode", "episode\x7f", "episode\u2028next"])
def test_episode_preserves_bounded_unicode_identifiers(identifier):
    assert ResolveEpisode(identifier).episode_id == identifier


@pytest.mark.parametrize("identifier", ["", " \u2003 ", "x" * 513])
def test_episode_refuses_empty_or_oversized_identifiers(identifier):
    with pytest.raises(ValueError):
        ResolveEpisode(identifier)


def test_episode_allows_multiline_operator_guidance_and_error_details():
    state = replace(snapshot(), episode=ResolveEpisode("episode", 1, "one\ntwo", "three\nfour"))
    assert state.binding.episode_id == "episode"
    assert state.episode.guidance == "three\nfour"


def test_cancel_command_cannot_authorize_new_rearm_generation():
    before = snapshot()
    after = replace(before, revision=WorkflowRevision(replace(before.revision.fence, generation=1), 1))
    audit = AuthorizedOperatorCommand(
        OperatorActor("operator", "test"),
        CancelRearm("cancel", before.revision, "old-request", "cancelled"),
    )
    with pytest.raises(ValueError, match="matching authorized request"):
        WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after, audit)


def test_rearm_cannot_advance_generation_without_pending_intent():
    before = snapshot()
    after = replace(before, revision=WorkflowRevision(replace(before.revision.fence, generation=1), 1))
    audit = AuthorizedOperatorCommand(
        OperatorActor("operator", "test"),
        RequestRearm("request", before.revision, "gate", KeepGuidance()),
        ResolveRearmAcceptance(ResolveRearmPreflight(
            ObservedIssue(before.revision.fence.key, IssueView(7, updated_at="revision"), datetime(2026, 9, 14, tzinfo=timezone.utc)),
            NoLinkedPr(before.revision.fence.key, datetime(2026, 9, 14, tzinfo=timezone.utc)),
        ), workspace_observation(before.binding, datetime(2026, 9, 14, tzinfo=timezone.utc))),
    )
    with pytest.raises(ValueError, match="pending intent"):
        WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after, audit)


@pytest.mark.parametrize("wrong_binding", ["target", "generation", "epoch", "episode"])
def test_result_cannot_authorize_changes_to_another_attempt_scope(wrong_binding):
    before = snapshot()
    binding = before.binding
    if wrong_binding == "target":
        binding = snapshot(999).binding
    elif wrong_binding == "generation":
        binding = replace(binding, fence=replace(binding.fence, generation=1))
    elif wrong_binding == "epoch":
        binding = replace(binding, authority=replace(binding.authority, epoch=2))
    else:
        binding = replace(binding, episode_id="different")
    result = ResolveAttemptResult(
        WorkflowAttemptIdentity(binding, RunId("run"), "leg"),
        ResolveResultKind.COUNTED_REQUEUE,
    )
    after = replace(before, revision=replace(before.revision, version=1))
    with pytest.raises(ValueError, match="trusted attempt identity"):
        WorkflowChanged(before.revision, WorkflowMutation.RECORD_RESULT, after, result)


def continuation_state():
    state = snapshot()
    source = WorkflowAttemptIdentity(state.binding, RunId("resolve"), "resolve-leg")
    session = ResolveSession(source, "session:with:separator", "codex", "<runtime-default>")
    return replace(state, revision=replace(state.revision, version=1),
                   episode=replace(state.episode, continuation=FreshResolve(session, 1)))


def test_same_episode_continuation_survives_a_generation_fence_advance():
    state = continuation_state()
    resumed = replace(state, revision=WorkflowRevision(replace(state.revision.fence, generation=1), 2))
    assert resumed.episode.continuation == state.episode.continuation
    assert resumed.episode.continuation.session.session_id == "session:with:separator"


@pytest.mark.parametrize("version", [True, -1, 2**63, "1", None])
def test_continuation_order_is_a_bounded_integer_not_a_permissive_marker(version):
    with pytest.raises(ValueError):
        FreshResolve(last_applied_admission_version=version)


def test_session_without_applied_admission_is_invalid():
    state = continuation_state()
    with pytest.raises(ValueError):
        FreshResolve(session=state.episode.continuation.session)


def test_router_result_cannot_supply_a_resolve_session():
    state = continuation_state()
    session = state.episode.continuation.session
    with pytest.raises(ValueError):
        ResolveAttemptResult(session.source, ResolveResultKind.STAGE_PROGRESSED, stage=ResolveStage.ROUTE, session=session)


def test_returned_session_must_belong_to_exact_result_attempt():
    state = continuation_state()
    session = state.episode.continuation.session
    other = replace(session.source, run_id=RunId("other"))
    with pytest.raises(ValueError):
        ResolveAttemptResult(other, ResolveResultKind.BLOCKED, session=session)


def test_admission_revision_is_positive_and_matches_attempt_generation():
    state = snapshot()
    attempt = WorkflowAttemptIdentity(state.binding, RunId("run"), "leg")
    with pytest.raises(ValueError):
        ResolveAdmission(attempt, state.revision, ResolveStage.RESOLVE)
    with pytest.raises(ValueError):
        ResolveAdmission(attempt, WorkflowRevision(replace(state.revision.fence, generation=1), 1), ResolveStage.RESOLVE)


