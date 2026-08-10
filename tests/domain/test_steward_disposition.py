from issue_agent.domain.actions import CloseIssue, PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.steward import (
    DuplicateOfClaim,
    DuplicateRefusal,
    RefusedDuplicateOf,
    ValidatedDuplicateOf,
    duplicate_close_actions,
    parse_groom_disposition,
    validate_duplicate_of,
)
from issue_agent.domain.world import IssueView


def _claim(target: int = 42) -> DuplicateOfClaim:
    parsed = parse_groom_disposition({
        "kind": "duplicate_of",
        "issue": target,
        "reason": "The same crash is tracked by the canonical report.",
    })
    assert parsed is not None
    return parsed


def test_validated_duplicate_disposition_closes_with_harness_owned_reason():
    current = IssueView(
        196,
        author="alice",
        labels=frozenset({Label.AI_TRIAGE}),
    )
    decision = validate_duplicate_of(
        current,
        _claim(),
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
        promote=True,
    )

    assert isinstance(decision, ValidatedDuplicateOf)
    actions = duplicate_close_actions(current, decision)
    close = next(action for action in actions if isinstance(action, CloseIssue))
    assert close.reason == "completed"
    assert "validated that #42 exists and is closed" in close.comment
    assert "The same crash is tracked" in close.comment
    assert Relabel(
        Target.ISSUE, 196, Label.AI_TRIAGE, Label.AI_CLOSED
    ) in actions
    assert not any(isinstance(action, PostComment) for action in actions)


def test_open_referenced_issue_is_refused_without_a_close_action():
    decision = validate_duplicate_of(
        IssueView(196), _claim(), IssueView(42, state="open"), promote=True
    )

    assert decision == RefusedDuplicateOf(42, DuplicateRefusal.REFERENCE_OPEN)
    assert not isinstance(decision, ValidatedDuplicateOf)


def test_self_referential_duplicate_is_refused_even_if_the_view_is_closed():
    decision = validate_duplicate_of(
        IssueView(196),
        _claim(196),
        IssueView(196, state="closed", labels=frozenset({Label.AI_DONE})),
        promote=True,
    )

    assert decision == RefusedDuplicateOf(196, DuplicateRefusal.SELF_REFERENCE)


def test_absent_and_malformed_dispositions_are_no_claim():
    malformed = (
        None,
        {},
        {"kind": "already_fixed", "issue": 42, "reason": "fixed"},
        {"kind": "duplicate_of", "issue": True, "reason": "same"},
        {"kind": "duplicate_of", "issue": 42, "reason": ""},
        {"kind": "duplicate_of", "issue": 42, "reason": "x" * 501},
        {"kind": "duplicate_of", "issue": 42, "reason": "same", "extra": 1},
    )

    assert all(parse_groom_disposition(value) is None for value in malformed)


def test_a_human_reopen_and_disabled_promotion_cannot_be_overridden():
    reopened = validate_duplicate_of(
        IssueView(196, state_reason="REOPENED"),
        _claim(),
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
        promote=True,
    )
    disabled = validate_duplicate_of(
        IssueView(196), _claim(), IssueView(42, state="closed"), promote=False
    )

    assert reopened == RefusedDuplicateOf(42, DuplicateRefusal.REOPENED)
    assert disabled == RefusedDuplicateOf(42, DuplicateRefusal.PROMOTE_DISABLED)


def test_active_current_issue_is_refused_even_with_a_harness_done_reference():
    decision = validate_duplicate_of(
        IssueView(196, labels=frozenset({Label.AI_TRIAGE, Label.AI_WORKING})),
        _claim(),
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
        promote=True,
    )

    assert decision == RefusedDuplicateOf(
        42, DuplicateRefusal.LIFECYCLE_ACTIVE
    )


def test_old_closed_non_harness_issue_is_not_a_valid_duplicate_target():
    decision = validate_duplicate_of(
        IssueView(196),
        _claim(),
        IssueView(42, state="closed"),
        promote=True,
    )

    assert decision == RefusedDuplicateOf(
        42, DuplicateRefusal.REFERENCE_NOT_HARNESS_DONE
    )


def test_duplicate_reason_is_one_code_span_with_markdown_and_mentions_inert():
    current = IssueView(196, author="alice")
    disposition = ValidatedDuplicateOf(
        42,
        "`break out` ![tracking](https://evil.invalid/pixel) @everyone",
    )

    close = next(
        action
        for action in duplicate_close_actions(current, disposition)
        if isinstance(action, CloseIssue)
    )

    assert (
        "**Groom finding:** `break out ![tracking](https://evil.invalid/pixel) "
        "@everyone`"
    ) in close.comment
    assert close.comment.count("`", close.comment.index("**Groom finding:**")) == 2
