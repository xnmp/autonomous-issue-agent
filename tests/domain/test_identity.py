"""GitHub login comparison — the one place the `[bot]` suffix asymmetry is
absorbed (review C1). These pin the contract every call site relies on."""

import pytest

from issue_agent.domain.identity import is_bot_login, login_eq, normalize_login


@pytest.mark.parametrize("a,b,equal", [
    # C1: an App configured as `a[bot]` surfaces bare as `a` in author fields.
    ("a[bot]", "a", True), ("a", "a[bot]", True), ("a", "a", True), ("a[bot]", "a[bot]", True),
    ("fix-bot", "review-bot", False), ("fix-bot[bot]", "review-bot", False),
    ("fix-bot[bot]", "review-bot[bot]", False),
    ("a[bot]", "ab", False), ("a", "a-bot", False),  # stripping is not a prefix match
    # GitHub logins are case-insensitive, without collapsing distinct ones.
    ("A[bot]", "a", True), ("Fix-Bot", "fix-bot", True), ("MY-Reviewer-Bot[bot]", "my-reviewer-bot", True),
    ("Fix-Bot", "review-bot", False),
    # Exactly one trailing suffix of tolerance.
    ("x[bot][bot]", "x", False), ("x[bot][bot]", "x[bot][bot]", True), ("a[bot]b", "a", False),
    # P1R-7: an unknown login never matches anything, including another unknown
    # (an unresolved PR author vs an unset FIX_BOT once read as OURS).
    ("", "fix-bot", False), (None, "fix-bot", False), ("", "fix-bot[bot]", False), ("", "", False),
    ("", "[bot]", False), ("fix-bot", "", False),
    # P1R-6: FIX_BOT='fix-bot ' (a shell round-trip) must still match `fix-bot`.
    ("fix-bot ", "fix-bot", True), ("fix-bot", " fix-bot ", True), ("fix-bot ", "review-bot", False),
])
def test_login_eq(a, b, equal):
    assert login_eq(a, b) is equal


@pytest.mark.parametrize("raw,normal", [
    ("x[bot][bot]", "x[bot]"), ("a[bot]b", "a[bot]b"), ("", ""), (None, ""), ("[bot]", ""),
    ("fix-bot ", "fix-bot"), (" fix-bot", "fix-bot"), ("\tfix-bot\n", "fix-bot"),
    # The strip must precede the suffix check, or removesuffix never matches.
    ("fix-bot[bot] ", "fix-bot"),
])
def test_normalize_login(raw, normal):
    assert normalize_login(raw) == normal


@pytest.mark.parametrize("login,bot", [
    # R2-11: world.Comment.is_bot routes through here.
    ("dependabot[bot]", True), ("DEPENDABOT[BOT]", True), (" dependabot[bot] ", True),
    ("a-human", False), ("", False), (None, False),
])
def test_is_bot_login(login, bot):
    assert is_bot_login(login) is bot
