"""GitHub login comparison — the one place the `[bot]` suffix asymmetry is
absorbed (review C1). These pin the contract every call site relies on."""

from issue_agent.domain.identity import is_bot_login, login_eq, normalize_login


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


def test_two_unknown_logins_never_compare_equal():
    # P1R-7: reverses the PRIOR documented contract (login_eq("", "") used to
    # be True — "callers must not rely on login_eq to reject an empty
    # configured identity"). That was unsafe in practice: an unresolved PR
    # author (adapter couldn't resolve `author`, e.g. a deleted/ghost account,
    # -> "") compared against an unset FIX_BOT ("") read as OURS, letting a
    # stranger's PR be treated as the fixer's own. Unknown must never match —
    # not a real bot identity, and not another unknown either.
    assert not login_eq("", "")
    assert not login_eq("", "[bot]")  # "[bot]" alone also normalizes to ""


def test_known_login_never_matches_an_unknown_one_either_order():
    # Anti-vacuity twin: the empty-normalizes-to-False rule must fire
    # regardless of which argument is the unknown one.
    assert not login_eq("fix-bot", "")
    assert not login_eq("", "fix-bot")


# ── P1R-6: whitespace must not defeat normalization ───────────────────────────
def test_normalize_login_strips_surrounding_whitespace():
    assert normalize_login("fix-bot ") == "fix-bot"
    assert normalize_login(" fix-bot") == "fix-bot"
    assert normalize_login("\tfix-bot\n") == "fix-bot"


def test_normalize_login_strips_whitespace_before_the_suffix_check():
    # The trailing space sits AFTER the [bot] suffix — strip must happen
    # first, or removesuffix never sees a matching tail.
    assert normalize_login("fix-bot[bot] ") == "fix-bot"


def test_trailing_whitespace_reproduces_and_then_fixes_the_c1_symptom():
    # THE P1R-6 repro: FIX_BOT='fix-bot ' (a trailing space, e.g. round-tripped
    # through a shell env file) must still match the bare `fix-bot` author —
    # otherwise every fixer PR leg silently dies again, the original C1 shape.
    assert login_eq("fix-bot ", "fix-bot")
    assert login_eq("fix-bot", " fix-bot ")


def test_whitespace_does_not_collapse_distinct_logins():
    # Anti-vacuity twin: stripping whitespace must not make unrelated logins
    # compare equal.
    assert not login_eq("fix-bot ", "review-bot")


# ── ABSENCE test (review §5): the whitespace strip is load-bearing ────────────
def test_absence_of_whitespace_stripping_would_reintroduce_c1():
    # Directly exercises what a regression looks like if `.strip()` were ever
    # removed from `normalize_login`: casefold+removesuffix ALONE do not
    # collapse "fix-bot " and "fix-bot". This test fails loudly the moment
    # someone reverts the strip, instead of only failing on the happy path.
    unstripped = "fix-bot ".casefold().removesuffix("[bot]")
    assert unstripped != "fix-bot"  # proves the strip is doing real work
    assert normalize_login("fix-bot ") == "fix-bot"  # …and the real fn is fixed


# ── R2-11: is_bot_login (world.Comment.is_bot routes through here) ───────────
def test_is_bot_login_matches_the_bot_suffix_case_insensitively():
    assert is_bot_login("dependabot[bot]")
    assert is_bot_login("DEPENDABOT[BOT]")
    assert is_bot_login(" dependabot[bot] ")


def test_is_bot_login_rejects_non_bot_and_degenerate_logins():
    # Anti-vacuity twin: a human login, and the degenerate empty/None cases,
    # must not read as a bot.
    assert not is_bot_login("a-human")
    assert not is_bot_login("")
    assert not is_bot_login(None)  # type: ignore[arg-type]
