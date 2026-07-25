"""GitHub login comparison — the one place the `[bot]` suffix asymmetry is
absorbed (review C1). These pin the contract every call site relies on."""

from issue_agent.domain.identity import login_eq, normalize_login


# ── the C1 asymmetry ──────────────────────────────────────────────────────────
def test_suffixed_and_bare_form_are_the_same_login():
    # THE C1 case: an App configured as `a[bot]` surfaces bare as `a` in the
    # review/PR author fields. These must compare equal, in both orders.
    assert login_eq("a[bot]", "a")
    assert login_eq("a", "a[bot]")


def test_identical_forms_compare_equal():
    assert login_eq("a", "a")
    assert login_eq("a[bot]", "a[bot]")


def test_different_logins_are_not_equal():
    # Anti-vacuity twin: login_eq must still discriminate, suffix or not.
    assert not login_eq("fix-bot", "review-bot")
    assert not login_eq("fix-bot[bot]", "review-bot")
    assert not login_eq("fix-bot[bot]", "review-bot[bot]")


def test_suffix_is_not_a_substring_match():
    # `a` and `ab` differ; stripping must not turn a prefix into a match.
    assert not login_eq("a[bot]", "ab")
    assert not login_eq("a", "a-bot")


# ── case-insensitivity (GitHub logins are case-insensitive) ───────────────────
def test_comparison_is_case_insensitive():
    assert login_eq("A[bot]", "a")
    assert login_eq("Fix-Bot", "fix-bot")
    assert login_eq("MY-Reviewer-Bot[bot]", "my-reviewer-bot")


def test_case_insensitivity_does_not_collapse_distinct_logins():
    # Anti-vacuity twin for casefolding.
    assert not login_eq("Fix-Bot", "review-bot")


# ── suffix stripping is exactly one level ─────────────────────────────────────
def test_only_one_bot_suffix_is_stripped():
    # A hypothetical literal `x[bot][bot]` normalizes to `x[bot]`, not `x`, so it
    # is NOT the same login as `x` — one suffix of tolerance, not unbounded.
    assert normalize_login("x[bot][bot]") == "x[bot]"
    assert not login_eq("x[bot][bot]", "x")
    assert login_eq("x[bot][bot]", "x[bot][bot]")


def test_suffix_must_be_trailing():
    # `[bot]` in the middle is part of the name, not a suffix.
    assert normalize_login("a[bot]b") == "a[bot]b"
    assert not login_eq("a[bot]b", "a")


# ── degenerate inputs ─────────────────────────────────────────────────────────
def test_empty_and_none_normalize_to_empty():
    assert normalize_login("") == ""
    assert normalize_login(None) == ""       # type: ignore[arg-type]
    assert normalize_login("[bot]") == ""    # a bare suffix is an empty login


def test_unknown_author_never_matches_a_configured_bot():
    # The fail-safe the fixer's authorship gate depends on: an unknown ("") author
    # is not the fix bot, so a PR we cannot confirm we authored is left alone.
    assert not login_eq("", "fix-bot")
    assert not login_eq(None, "fix-bot")     # type: ignore[arg-type]
    assert not login_eq("", "fix-bot[bot]")


def test_two_unknown_logins_compare_equal():
    # Documented consequence, not an accident: with an UNSET bot login every
    # unknown author matches. Callers must not rely on `login_eq` to reject an
    # empty configured identity — config validation owns that (review C1/D1).
    assert login_eq("", "")
    assert login_eq("", "[bot]")
