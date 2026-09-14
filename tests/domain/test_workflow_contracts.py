from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import (
    AuthorizedOperatorCommand, CancelRearm, KeepGuidance, OperatorActor,
    RequestRearm, ResolveAttemptResult, ResolveEpisode, ResolveResultKind,
    WorkflowChanged, WorkflowMutation, WorkflowSnapshot,
    ResolveRearmPreflight, ObservedIssue, NoLinkedPr,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey,
    WorkflowRevision,
)


def snapshot(number=7):
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(number)), 0), 0),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1), ResolveEpisode("episode"),
    )


@pytest.mark.parametrize("control", [chr(value) for value in range(32)])
def test_episode_rejects_identifier_that_cannot_be_carried_to_admission(control):
    with pytest.raises(ValueError):
        ResolveEpisode(f"episode{control}7")


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
        ResolveRearmPreflight(
            ObservedIssue(before.revision.fence.key, IssueView(7, updated_at="revision"), datetime(2026, 9, 14, tzinfo=timezone.utc)),
            NoLinkedPr(before.revision.fence.key, datetime(2026, 9, 14, tzinfo=timezone.utc)),
        ),
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
