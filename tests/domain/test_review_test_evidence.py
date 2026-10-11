from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import TestExecution as Execution, TestExecutionStatus as ExecutionStatus
from issue_agent.domain.review_execution import ReviewRoundIdentity
from issue_agent.domain.review_test_evidence import (
    MissingOperatorTestCommand, OperatorTestCommand, OperatorTestWaiver, ReviewTestConfiguration,
    ReviewTestSubject, ReviewTestInvocation, ReviewTestCompletion, ReviewTestTreeProof, ReviewTestOutcome,
    posted_review,
    failed_test_review_body,
    test_only_rejection_release_eligible as release_eligible,
)
from tests.domain.test_review_execution import binding
from issue_agent.domain.review_workflow import mark_review_body

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


@pytest.mark.parametrize("exit_code", [None, 1, 42])
def test_failed_test_review_has_plain_header_and_deterministic_details(exit_code):
    body = failed_test_review_body("failed\néchec 日本語\n", exit_code)
    assert body == failed_test_review_body("failed\néchec 日本語\n", exit_code)
    header = body.splitlines()[0]
    assert header == ("Independent operator tests failed on this head."
                      if exit_code is None else f"Independent operator tests failed on this head (exit {exit_code}).")
    assert HEAD not in header and PATCH not in header
    assert "<details><summary>Full test output (tail)</summary>\n\n" in body
    assert body.endswith("\n\n</details>")
    assert "échec 日本語" in body


def test_failed_test_preview_shows_last_25_nonempty_lines_and_details_keep_full_tail():
    from markdown_it import MarkdownIt

    output = "\n \n".join(f"failure {i}" for i in range(40))
    body = failed_test_review_body("failed\n" + output, 1)
    blocks = [token.content for token in MarkdownIt().parse(body) if token.type == "fence"]
    assert blocks == ["\n".join(f"failure {i}" for i in range(15, 40)) + "\n", output + "\n"]


def test_legacy_ansi_summary_renders_clean_in_preview_and_full_output():
    from markdown_it import MarkdownIt

    body = failed_test_review_body("failed\n\x1b[31mERROR échec\x1b[0m\n", 1)
    blocks = [token.content for token in MarkdownIt().parse(body) if token.type == "fence"]
    assert blocks == ["ERROR échec\n", "ERROR échec\n\n"]
    assert "\x1b" not in body


def test_test_output_cannot_close_its_markdown_fence():
    from markdown_it import MarkdownIt

    output = "failure\n```\n</details>\n``````\nlast failure"
    body = failed_test_review_body("failed\n" + output, 1)
    blocks = [token.content for token in MarkdownIt().parse(body) if token.type == "fence"]
    assert blocks == [output + "\n", output + "\n"]


@pytest.mark.parametrize("output", ["", "x" * 1000000, "`" * 1000000, ("```\n" * 250000)])
def test_huge_review_output_fits_submission_and_marker_bounds(output):
    from markdown_it import MarkdownIt

    body = failed_test_review_body("failed\n" + output, 1)
    assert len(body) <= 65536 - 1024
    assert len(mark_review_body(body, "c" * 512)) <= 65536
    assert body.endswith("</details>")
    assert len([token for token in MarkdownIt().parse(body) if token.type == "fence"]) == 2
