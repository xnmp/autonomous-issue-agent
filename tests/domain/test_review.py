"""A review leg's verdict is the agent's claim: parsed, validated and bounded."""

import pytest

from issue_agent.domain.review import REVIEW_BODY_LIMIT, Verdict, parse_verdict


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
def test_an_exact_decision_with_a_body_is_a_verdict(event):
    assert parse_verdict({"decision": event, "body": "  Handle None.  "}) == Verdict(event, "Handle None.")


def test_an_approval_may_have_an_empty_body():
    assert parse_verdict({"decision": "APPROVE", "body": ""}) == Verdict("APPROVE", "")


@pytest.mark.parametrize("structured", [
    None,
    "APPROVE",
    ["APPROVE"],
    {},
    {"decision": "APPROVE"},
    {"decision": "APPROVE", "body": None},
    {"decision": "APPROVE", "body": 7},
    {"decision": "approve", "body": "lower case is not the contract"},
    {"decision": "ESCALATE", "body": "escalation is derive's, not the agent's"},
    {"decision": "COMMENT", "body": "x"},
    {"decision": ["APPROVE"], "body": "x"},
    {"decision": "REQUEST_CHANGES", "body": ""},
    {"decision": "REQUEST_CHANGES", "body": " \n\t "},
])
def test_anything_else_is_no_verdict(structured):
    assert parse_verdict(structured) is None


def test_a_huge_body_is_bounded_to_one_github_review():
    verdict = parse_verdict({"decision": "REQUEST_CHANGES", "body": "x" * (REVIEW_BODY_LIMIT * 3)})

    assert verdict is not None and len(verdict.body) == REVIEW_BODY_LIMIT


def test_a_verdict_cannot_be_constructed_outside_its_contract():
    with pytest.raises(ValueError):
        Verdict("REQUEST_CHANGES", "  ")
    with pytest.raises(ValueError):
        Verdict("ESCALATE", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Verdict("APPROVE", "x" * (REVIEW_BODY_LIMIT + 1))
