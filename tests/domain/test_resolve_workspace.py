"""Workspace obligations preserve identity without inventing author evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.resolve_split import ContractResetResult
from issue_agent.domain.resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, NoPriorResolveAdmission,
    ObservedResolveBase, PrepareResolveWorkspace, PriorResolveAdmission,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def binding(*, repository="o/r", issue=7, epoch=1, generation=3, episode="old"):
    return WorkflowBinding(WorkflowFence(WorkflowKey(repository, IssueRef(issue)), generation),
                           FamilyAuthority(WorkflowFamily.RESOLVE, epoch), episode)


def handoff(*, absent=False):
    source = binding()
    previous = NoPriorResolveAdmission(source) if absent else PriorResolveAdmission(
        WorkflowAttemptIdentity(source, RunId("paid-run"), "paid-leg"), 11,
    )
    return EpisodeWorkspaceHandoff("grant", source, binding(generation=4, episode="new"),
                                    previous, "fix/7-contract", ObservedResolveBase("o/r", "refs/heads/dev", "a" * 40, NOW))


def prepared(value=None, *, generation=4):
    value = value or handoff()
    return EpisodeWorkspacePrepared(value, binding(generation=generation, episode="new"),
                                    ContractResetResult(value.revision, value.archive_ref), NOW + timedelta(seconds=1))


def test_continuation_reuses_exact_handoff_pin_and_archive():
    first = prepared()
    continued = prepared(first.handoff, generation=6)
    assert continued.handoff == first.handoff
    assert continued.handoff.handoff_id == first.handoff.handoff_id
    assert continued.result == first.result
    assert continued.handoff.base.pin_ref == first.handoff.base.pin_ref
    assert PrepareResolveWorkspace(first.handoff).handoff == first.handoff


def test_reobservation_does_not_change_selected_base_or_handoff_identity():
    original = handoff()
    later = replace(original, base=replace(original.base, observed_at=NOW + timedelta(seconds=20)))
    assert later.handoff_id == original.handoff_id
    assert later.archive_ref == original.archive_ref
    assert later.base.pin_ref == original.base.pin_ref


@pytest.mark.parametrize("field,value", [
    ("repository", "o/other"), ("base_ref", "refs/heads/main"), ("commit_sha", "b" * 40),
])
def test_base_pin_identity_changes_when_selected_source_changes(field, value):
    base = handoff().base
    assert replace(base, **{field: value}).pin_ref != base.pin_ref


def test_request_branch_revision_and_predecessor_have_distinct_handoffs():
    original = handoff()
    prior = original.predecessor
    assert isinstance(prior, PriorResolveAdmission)
    variants = (
        replace(original, grant_request_id="other-grant"),
        replace(original, branch="fix/7-other"),
        replace(original, base=replace(original.base, commit_sha="b" * 40)),
        replace(original, base=replace(original.base, base_ref="refs/heads/main")),
        replace(original, predecessor=replace(prior, admission_version=12)),
        replace(original, predecessor=replace(prior, attempt=replace(prior.attempt, run_id=RunId("other-run")))),
        replace(original, predecessor=replace(prior, attempt=replace(prior.attempt, leg_id="other-leg"))),
        handoff(absent=True),
    )
    assert len({original.handoff_id, *(value.handoff_id for value in variants)}) == len(variants) + 1
    assert all(value.archive_ref != original.archive_ref for value in variants)


@pytest.mark.parametrize("destination", [
    binding(repository="o/other", generation=4, episode="new"),
    binding(issue=8, generation=4, episode="new"),
    binding(epoch=2, generation=4, episode="new"),
    binding(generation=3, episode="new"), binding(generation=5, episode="new"),
    binding(generation=4, episode="old"), None,
])
def test_handoff_requires_one_new_episode_generation_in_same_scope(destination):
    with pytest.raises(ValueError):
        replace(handoff(), destination=destination)


@pytest.mark.parametrize("previous", [binding(issue=8), binding(repository="o/other"), binding(epoch=2), binding(generation=4)])
def test_predecessor_cannot_be_foreign_or_from_future(previous):
    prior = PriorResolveAdmission(WorkflowAttemptIdentity(previous, RunId("prior"), "leg"), 1)
    with pytest.raises(ValueError):
        replace(handoff(), predecessor=prior)


def test_older_episode_predecessor_remains_explicit_not_rebound_to_source():
    previous = binding(generation=1, episode="even-older")
    prior = PriorResolveAdmission(WorkflowAttemptIdentity(previous, RunId("prior"), "leg"), 1)
    value = replace(handoff(), predecessor=prior)
    assert value.predecessor.attempt.binding == previous
    assert value.predecessor.attempt.binding != value.source


def test_same_generation_cannot_claim_another_owning_episode():
    prior = PriorResolveAdmission(WorkflowAttemptIdentity(binding(episode="contradictory"), RunId("prior"), "leg"), 1)
    with pytest.raises(ValueError):
        replace(handoff(), predecessor=prior)


@pytest.mark.parametrize("previous", [None, {}, "absent", NoPriorResolveAdmission(binding(episode="other"))])
def test_missing_or_wrong_predecessor_is_not_verified_absence(previous):
    with pytest.raises(ValueError):
        replace(handoff(), predecessor=previous)


@pytest.mark.parametrize("authority", [binding(), binding(generation=4, episode="wrong"),
    binding(issue=8, generation=4, episode="new"), binding(epoch=2, generation=4, episode="new"),
    binding(repository="o/other", generation=4, episode="new"), None])
def test_preparation_ack_cannot_cross_granted_episode_scope(authority):
    with pytest.raises(ValueError):
        replace(prepared(), performed_under=authority)


def test_ack_requires_exact_commit_branch_and_archive():
    ack = prepared()
    revisions = (replace(ack.result.revision, head_sha="b" * 40), replace(ack.result.revision, branch="fix/7-other"))
    for revision in revisions:
        with pytest.raises(ValueError):
            replace(ack, result=replace(ack.result, revision=revision))
    with pytest.raises(ValueError):
        replace(ack, result=replace(ack.result, archive_ref="refs/issue-agent/contract-reset/" + "b" * 64))


@pytest.mark.parametrize("time", [None, "2026-09-23", datetime(2026, 9, 23)])
def test_observations_require_aware_times(time):
    with pytest.raises(ValueError):
        replace(handoff().base, observed_at=time)
    with pytest.raises(ValueError):
        replace(prepared(), observed_at=time)


def test_ack_cannot_predate_base_observation_and_compares_instants():
    with pytest.raises(ValueError):
        replace(prepared(), observed_at=NOW - timedelta(microseconds=1))
    offset = NOW.astimezone(timezone(timedelta(hours=10)))
    assert replace(prepared(), observed_at=offset).observed_at == NOW


@pytest.mark.parametrize("value", [None, "", " ", "x" * 513, "bad\0identity", "bad\nidentity"])
def test_handoff_request_identity_is_bounded(value):
    with pytest.raises(ValueError):
        replace(handoff(), grant_request_id=value)


@pytest.mark.parametrize("value", [None, "", "dev", "refs/heads/", "refs/tags/v1", "refs/heads/" + "x" * 513])
def test_upstream_requires_exact_bounded_branch_ref(value):
    with pytest.raises(ValueError):
        replace(handoff().base, base_ref=value)


@pytest.mark.parametrize("sha", [None, "", "a" * 39, "a" * 41, "A" * 40, "g" * 64, "a" * 100000])
def test_upstream_requires_complete_git_identity(sha):
    with pytest.raises(ValueError):
        replace(handoff().base, commit_sha=sha)


def test_sha256_git_objects_and_canonical_repository_are_preserved():
    base = replace(handoff().base, repository=" O/R ", commit_sha="a" * 64)
    assert base.repository == "o/r" and base.commit_sha == "a" * 64
    assert replace(base, repository="o/r").pin_ref == base.pin_ref


@pytest.mark.parametrize("repository", [None, "", "*", "owner", "o/r/extra", "o/" + "x" * 101, "bad\0owner/r"])
def test_base_requires_valid_repository_identity(repository):
    with pytest.raises(ValueError):
        replace(handoff().base, repository=repository)


@pytest.mark.parametrize("branch", [None, "", "main", "fix/8-other", "fix/7-" + "x" * 512, "fix/7-bad\nbranch"])
def test_handoff_requires_bounded_issue_owned_branch(branch):
    with pytest.raises(ValueError):
        replace(handoff(), branch=branch)


@pytest.mark.parametrize("version", [None, False, 0, -1, 2**63, 1.5, "1"])
def test_predecessor_version_must_be_bounded_positive_integer(version):
    prior = handoff().predecessor
    assert isinstance(prior, PriorResolveAdmission)
    with pytest.raises(ValueError):
        replace(prior, admission_version=version)
