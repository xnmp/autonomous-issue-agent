"""CI attempt history — bounding, normalization, and corruption tolerance.

The behavioral contract: a history that cannot be trusted reads as no history,
and no input shape can make it unbounded.
"""

from issue_agent.domain.ci_history import (
    CI_HISTORY_LIMIT,
    MAX_CHECKS_PER_ATTEMPT,
    MAX_FIELD_CHARS,
    CiAttempt,
    append_attempt,
    ci_history_payload,
    make_attempt,
    parse_ci_history,
)


def test_appending_keeps_only_the_most_recent_attempts():
    history = ()
    for n in range(CI_HISTORY_LIMIT + 3):
        history = append_attempt(history, CiAttempt(f"sha{n}"))
    assert len(history) == CI_HISTORY_LIMIT
    # oldest-first, and the ones dropped are the OLDEST
    assert [a.head_sha for a in history] == [
        f"sha{n}" for n in range(3, CI_HISTORY_LIMIT + 3)
    ]


def test_a_round_trip_through_the_json_shape_preserves_the_attempts():
    history = (
        CiAttempt("a" * 40, ("unit", "lint")),
        CiAttempt("b" * 40, ("unit",)),
    )
    assert parse_ci_history(ci_history_payload(history)) == history


def test_make_attempt_accepts_the_newline_joined_check_names_the_prompt_uses():
    a = make_attempt("abc1234def", "unit\nlint\n")
    assert a.failed_checks == ("unit", "lint")
    assert a.short_sha() == "abc1234"


def test_make_attempt_drops_duplicate_and_non_string_check_names():
    a = make_attempt("sha", ["unit", "unit", None, 7, "  lint  ", ""])
    assert a.failed_checks == ("unit", "lint")


def test_an_attempt_with_no_recoverable_fields_still_counts_as_an_attempt():
    # The pattern that matters is "this check failed AGAIN"; a missing head SHA
    # must not silently erase the round from the record.
    a = make_attempt(None, None)
    assert a == CiAttempt("", ())
    assert a.short_sha() == "unknown"
    assert len(append_attempt((), a)) == 1


def test_huge_inputs_cannot_grow_an_attempt_without_bound():
    a = make_attempt("f" * 10_000, [f"check-{i}" for i in range(5_000)])
    assert len(a.head_sha) == MAX_FIELD_CHARS
    assert len(a.failed_checks) == MAX_CHECKS_PER_ATTEMPT
    assert all(len(name) <= MAX_FIELD_CHARS for name in a.failed_checks)


def test_a_single_huge_check_name_is_truncated_not_dropped():
    a = make_attempt("sha", ["x" * 10_000])
    assert a.failed_checks == ("x" * MAX_FIELD_CHARS,)


def test_corrupt_or_foreign_shapes_read_as_no_history():
    for raw in (None, "", "not-json-decoded", 7, {"head_sha": "a"}, [1, 2, 3]):
        assert parse_ci_history(raw) == ()


def test_a_partially_corrupt_history_keeps_the_usable_entries():
    parsed = parse_ci_history(
        [{"head_sha": "aaa", "failed_checks": ["unit"]}, "junk", {"nope": 1}]
    )
    assert parsed == (CiAttempt("aaa", ("unit",)), CiAttempt("", ()))


def test_parsing_bounds_an_oversized_file_to_the_limit():
    parsed = parse_ci_history([{"head_sha": str(n)} for n in range(50)])
    assert len(parsed) == CI_HISTORY_LIMIT
    assert parsed[-1].head_sha == "49"  # the most recent survives
