"""Workflow identities cannot alias issue/PR, repository, or episode scopes."""

import pytest

from issue_agent.domain.workflow_identity import (
    FamilyAuthority,
    IssueRef,
    PullRequestRef,
    WorkflowBinding,
    WorkflowFamily,
    WorkflowFence,
    WorkflowKey,
    WorkflowRevision,
    WorkflowWriter,
)


def test_repository_aliases_share_identity_but_issue_and_pr_do_not():
    issue = WorkflowKey(" OWNER/Repo ", IssueRef(7))
    assert issue == WorkflowKey("owner/repo", IssueRef(7))
    assert issue != WorkflowKey("owner/repo", PullRequestRef(7))
    assert issue != WorkflowKey("other/repo", IssueRef(7))


@pytest.mark.parametrize("value", [None, True, False, 0, -7, 1.5, "7", 2**63])
@pytest.mark.parametrize("reference", [IssueRef, PullRequestRef])
def test_invalid_target_number_is_rejected(reference, value):
    with pytest.raises(ValueError):
        reference(value)


@pytest.mark.parametrize("repository", [None, "", "*", "repo", "a/", "/b", "a/b/c", "https://github.com/a/b", "a/b.git"])
def test_workflow_requires_an_explicit_repository(repository):
    with pytest.raises(ValueError):
        WorkflowKey(repository, IssueRef(1))


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "1", 2**63])
def test_invalid_generation_or_version_cannot_authorize_work(value):
    key = WorkflowKey("o/r", IssueRef(1))
    with pytest.raises(ValueError):
        WorkflowFence(key, value)
    with pytest.raises(ValueError):
        WorkflowRevision(WorkflowFence(key, 0), value)


@pytest.mark.parametrize("epoch", [None, False, 0, -1, "1", 2**63])
def test_invalid_epoch_cannot_create_authority(epoch):
    with pytest.raises(ValueError):
        FamilyAuthority(WorkflowFamily.RESOLVE, epoch)


def test_new_generation_is_distinct_from_an_ordinary_version_change():
    key = WorkflowKey("o/r", IssueRef(1))
    assert WorkflowRevision(WorkflowFence(key, 1), 2) != WorkflowRevision(WorkflowFence(key, 2), 2)
    assert WorkflowRevision(WorkflowFence(key, 1), 2) != WorkflowRevision(WorkflowFence(key, 1), 3)


def test_resolve_binding_rejects_a_pr_target():
    with pytest.raises(ValueError, match="belong to issues"):
        WorkflowBinding(
            WorkflowFence(WorkflowKey("o/r", PullRequestRef(1)), 0),
            FamilyAuthority(WorkflowFamily.RESOLVE, 1),
            "episode-1",
        )


@pytest.mark.parametrize("episode", [None, "", "  ", "x" * 513, "episode\nother"])
def test_malformed_episode_identity_is_rejected(episode):
    with pytest.raises(ValueError):
        WorkflowBinding(
            WorkflowFence(WorkflowKey("o/r", IssueRef(1)), 0),
            FamilyAuthority(WorkflowFamily.RESOLVE, 1),
            episode,
        )


def test_raw_variant_claims_are_not_trusted_values():
    with pytest.raises(ValueError):
        FamilyAuthority("resolve", 1)
    with pytest.raises(ValueError):
        WorkflowKey("o/r", {"number": 1})
    with pytest.raises(ValueError):
        WorkflowWriter("o/r", "dashboard", "o/r:dashboard")


def test_dispatch_and_command_preserve_captured_binding():
    from issue_agent.domain.actions import AgentKind, RunAgent
    from issue_agent.domain.contracts import CommandId, CommandKind, LifecycleCommand

    binding = WorkflowBinding(
        WorkflowFence(WorkflowKey("o/r", IssueRef(7)), 2),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1),
        "episode-7",
    )
    action = RunAgent(AgentKind.RESOLVE, 7, workflow=binding)
    command = LifecycleCommand(
        CommandId("command-7"), "once-7", CommandKind.RELABEL, "issue:7", "revision",
        repository="o/r", executor_identity="o/r:fix", workflow=binding,
    )
    assert action.workflow == binding
    assert command.workflow == binding
    with pytest.raises(ValueError, match="issue identity"):
        RunAgent(AgentKind.RESOLVE, 8, workflow=binding)
    with pytest.raises(ValueError, match="repository"):
        LifecycleCommand(
            CommandId("command-7"), "once-7", CommandKind.RELABEL, "issue:7", "revision",
            repository="other/repo", executor_identity="other/repo:fix", workflow=binding,
        )
