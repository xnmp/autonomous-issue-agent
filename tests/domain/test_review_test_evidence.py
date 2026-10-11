from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import TestExecution as Execution, TestExecutionStatus as ExecutionStatus
from issue_agent.domain.review_execution import ReviewRoundIdentity
from issue_agent.domain.review_test_evidence import (
    MissingOperatorTestCommand, OperatorTestCommand, OperatorTestWaiver, ReviewTestConfiguration,
    ReviewTestSubject, ReviewTestInvocation, ReviewTestCompletion, ReviewTestTreeProof, ReviewTestOutcome,
    posted_review,
    test_only_rejection_release_eligible as release_eligible,
)
from tests.domain.test_review_execution import binding

HEAD = "a" * 40
PATCH = "b" * 64


def configuration(command="true"):
    return ReviewTestConfiguration(OperatorTestCommand(command), (1,), 10, "c" * 64, "v1")


def invocation(config=None, *, round_id="round:1", invocation_id="invocation:1"):
    config = config or configuration()
    execution = binding()
    subject = ReviewTestSubject(execution.rejection, execution, HEAD, PATCH, config.identity)
    return ReviewTestInvocation(subject, config, ReviewRoundIdentity(execution, round_id, 1), invocation_id,
        datetime(2026, 10, 4, tzinfo=timezone.utc))


def completion(value=None, outcome=ReviewTestOutcome.PASSED):
    value = value or invocation()
    tree = ReviewTestTreeProof(HEAD, "d" * 40, "d" * 40, "d" * 40)
    status = ExecutionStatus.PASSED if outcome is ReviewTestOutcome.PASSED else ExecutionStatus.TEST_FAILED
    execution = Execution(status, ("bash", "-c", value.configuration.scope.command), 0 if status is ExecutionStatus.PASSED else 1,
        20, "e" * 64, "f" * 64, value.configuration.executor_policy, HEAD)
    return ReviewTestCompletion(value, outcome, execution, tree, tree, "executed")


def test_stable_subject_excludes_round_and_invocation_provenance():
    assert invocation().subject == invocation(round_id="round:2", invocation_id="invocation:2").subject
    assert invocation().subject.identity != invocation(configuration("false")).subject.identity


@pytest.mark.parametrize("bad", [None, True, -1, float("nan"), float("inf"), 999999999])
def test_timeout_requires_finite_bounded_number(bad):
    with pytest.raises(ValueError):
        replace(configuration(), timeout_ms=bad)


def test_pass_requires_execution_and_equal_structured_proofs():
    receipt = completion()
    with pytest.raises(ValueError):
        replace(receipt, execution=None)
    with pytest.raises(ValueError):
        replace(receipt, after=None)
    with pytest.raises(ValueError):
        replace(receipt, after=replace(receipt.after, committed_tree="f" * 40, index_tree="f" * 40, tracked_tree="f" * 40))


def test_failure_requires_operator_assertion_contract():
    receipt = completion(outcome=ReviewTestOutcome.FAILED)
    with pytest.raises(ValueError):
        replace(receipt, execution=replace(receipt.execution, exit_code=127))


def test_waiver_is_separate_from_pass_and_never_contains_execution():
    value = invocation(replace(configuration(), scope=OperatorTestWaiver()))
    tree = completion().before
    receipt = ReviewTestCompletion(value, ReviewTestOutcome.WAIVED, None, tree, tree, "operator waiver")
    assert receipt.approves
    with pytest.raises(ValueError):
        replace(receipt, outcome=ReviewTestOutcome.PASSED)


def test_posted_outcomes_cannot_contradict_event_and_escalation():
    assert posted_review("COMMENT", "ceiling").escalated
    assert posted_review("APPROVE", None).event == "APPROVE"
    with pytest.raises(ValueError):
        posted_review("APPROVE", "ceiling")


@pytest.mark.parametrize("mismatch", [None, "event", "reference", "absent", "malformed", "oversized",
                                     "completion", "outcome", "head", "patch", "rejection", "execution"])
def test_test_only_rejection_release_requires_exact_failed_harness_evidence(mismatch):
    evidence = completion(outcome=ReviewTestOutcome.FAILED)
    subject = evidence.invocation.subject
    arguments = dict(event="REQUEST_CHANGES", reference=evidence.invocation.invocation_id, completion=evidence,
                     rejection=subject.rejection, execution=subject.execution, head=subject.head_sha,
                     patch=subject.patch_id)
    if mismatch == "event":
        arguments["event"] = "APPROVE"
    elif mismatch == "reference":
        arguments["reference"] = "another-invocation"
    elif mismatch == "absent":
        arguments["reference"] = None
    elif mismatch == "malformed":
        arguments["reference"] = {"invocation": evidence.invocation.invocation_id}
    elif mismatch == "oversized":
        arguments["reference"] = "x" * 100000
    elif mismatch == "completion":
        arguments["completion"] = None
    elif mismatch == "outcome":
        arguments["completion"] = completion()
    elif mismatch == "head":
        arguments["head"] = "f" * 40
    elif mismatch == "patch":
        arguments["patch"] = "f" * 64
    elif mismatch == "rejection":
        arguments["rejection"] = replace(subject.rejection, episode_id="another-episode")
    elif mismatch == "execution":
        arguments["execution"] = replace(subject.execution, execution_episode="another-execution")
    assert release_eligible(**arguments) is (mismatch is None)
