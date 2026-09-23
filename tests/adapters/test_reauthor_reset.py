"""Replacement contract writing consumes isolated, harness-proven baselines."""

from dataclasses import replace

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import RunId
from issue_agent.domain.resolve_split import ContractResetCompleted, ContractResetResult, ContractRevision, ReauthorInput, ReauthorResetIntent, UnavailableContractRevision
from issue_agent.domain.workflow import AuthoredResolve, ResolveStage, WorkflowCommitted
from tests.adapters.test_split_workflow_control import (
    NOW, author, authority, capture, context, finalize, state, store,
)
from tests.adapters.test_workflow_control import return_observations


def reset(store, fingerprint="b" * 64):
    control = authority(store)
    intent = control.reauthor_reset_intent(state(store).binding, fingerprint)
    assert isinstance(intent, ReauthorResetIntent)
    with control.contract_reset_authority(intent) as previous:
        return previous or control.complete_contract_reset(ContractResetCompleted(
            intent, ContractResetResult(intent.baseline.revision, intent.archive_ref), NOW,
        ))


def test_changed_issue_return_clears_cursor_but_preserves_safe_reauthor_baseline(store):
    ready = author(store)
    returned = capture(store, ResolveStage.IMPLEMENT, ready)
    issue, linkage = return_observations(returned)
    completed = authority(store).finalize_return(returned, replace(issue, issue=replace(issue.issue, body="changed")), linkage)
    assert isinstance(completed, WorkflowCommitted)
    assert completed.snapshot.episode.continuation.split is None
    ack = reset(store)
    assert ack.intent.source == returned.attempt
    assert ack.intent.baseline == ready.baseline
    replacement = capture(store, ResolveStage.AUTHOR, ReauthorInput(ack, context()), name="replacement",
                          fingerprint="b" * 64, revision=ContractRevision(context().branch, "b" * 40))
    result = finalize(store, replacement)
    assert isinstance(result, WorkflowCommitted)
    assert result.snapshot.episode.continuation.split.baseline.revision.head_sha == "b" * 40


@pytest.mark.parametrize("status,error", [("blocked", False), ("failed", True)])
def test_failed_replacement_author_resets_to_prior_contract_not_its_dirty_head(store, status, error):
    ready = author(store)
    first = reset(store)
    failed = capture(store, ResolveStage.AUTHOR, ReauthorInput(first, context()), name="failed-author",
                     fingerprint="b" * 64, status=status, is_error=error,
                     revision=ContractRevision(context().branch, "d" * 40))
    completed = finalize(store, failed)
    assert isinstance(completed, WorkflowCommitted)
    following = reset(store)
    assert following.intent.source == failed.attempt
    assert following.intent.baseline == ready.baseline
    assert following.intent.intent_id != first.intent.intent_id
    assert following.result.archive_ref != first.result.archive_ref
    retry = capture(store, ResolveStage.AUTHOR, ReauthorInput(following, context()), name="author-retry",
                    fingerprint="b" * 64, reserve_only=True)
    assert retry.admitted, retry.detail


def test_unlaunched_replacement_reuses_exact_completed_reset_without_another_git_effect(store):
    author(store)
    ack = reset(store)
    capture(store, ResolveStage.AUTHOR, ReauthorInput(ack, context()), name="unlaunched-author",
            fingerprint="b" * 64, reserve_only=True)
    store.release_unlaunched(RunId("unlaunched-author"), "spawn never started")
    assert reset(store) == ack


def test_reset_cannot_invent_baseline_or_reuse_current_author_inputs(store):
    author(store)
    with pytest.raises(WorkflowAuthorityError, match="applicable author inputs"):
        authority(store).reauthor_reset_intent(state(store).binding, "a" * 64)
    intent = authority(store).reauthor_reset_intent(state(store).binding, "b" * 64)
    assert isinstance(intent, ReauthorResetIntent)
    forged = replace(intent, baseline=replace(intent.baseline, revision=ContractRevision(context().branch, "d" * 40)))
    with pytest.raises(WorkflowAuthorityError, match="predecessor or baseline"):
        with authority(store).contract_reset_authority(forged):
            pytest.fail("forged reset authorized Git")


def test_reauthor_reset_is_fenced_after_new_author_is_admitted(store):
    author(store)
    ack = reset(store)
    capture(store, ResolveStage.AUTHOR, ReauthorInput(ack, context()), name="active-author",
            fingerprint="b" * 64, reserve_only=True)
    with pytest.raises(WorkflowAuthorityError, match="immutable paid return"):
        with authority(store).contract_reset_authority(ack.intent):
            pytest.fail("reset raced a new paid contract writer")


@pytest.mark.parametrize("status", ["fresh", "unlaunched", "failed"])
def test_initial_author_without_inherited_baseline_needs_no_reset(store, status):
    if status == "unlaunched":
        capture(store, ResolveStage.AUTHOR, context(), name="first-author", reserve_only=True)
        store.release_unlaunched(RunId("first-author"), "spawn never started")
    elif status == "failed":
        returned = capture(store, ResolveStage.AUTHOR, context(), status="blocked")
        assert isinstance(finalize(store, returned), WorkflowCommitted)
    assert authority(store).reauthor_reset_intent(state(store).binding, "b" * 64) is None


@pytest.mark.parametrize("recovered", [False, True])
def test_successful_author_changed_facts_requires_reset_even_without_checkpoint(store, recovered):
    baseline = ContractRevision(context().branch, "c" * 40)
    returned = capture(store, ResolveStage.AUTHOR, context(), revision=UnavailableContractRevision("read failed") if recovered else baseline)
    issue, linkage = return_observations(returned)
    completed = authority(store).finalize_return(returned, replace(issue, issue=replace(issue.issue, title="changed")), linkage,
                                                 contract_revision=baseline if recovered else None)
    assert isinstance(completed, WorkflowCommitted)
    assert not isinstance(completed.snapshot.episode.continuation, AuthoredResolve)
    ack = reset(store)
    assert ack.intent.baseline.revision == baseline
    assert ack.intent.source == returned.attempt
    reservation = capture(store, ResolveStage.AUTHOR, ReauthorInput(ack, context()), name="new-requirements",
                          fingerprint="b" * 64, reserve_only=True)
    assert reservation.admitted, reservation.detail


@pytest.mark.parametrize("fingerprint", ["", "A" * 64, "a" * 65, None])
def test_fresh_workflow_still_rejects_malformed_reset_input_identity(store, fingerprint):
    with pytest.raises(ValueError, match="SHA-256"):
        authority(store).reauthor_reset_intent(state(store).binding, fingerprint)
