"""GitHub account identity never collapses a User into an app Bot (#481)."""
import pytest

from issue_agent.domain.identity import Account, account_eq, configured_account, is_bot_login


@pytest.mark.parametrize("left,right,equal", [
    ("steward", "steward[bot]", False), ("steward[bot]", "steward", False),
    ("STEWARD[bot] ", "steward[bot]", True), (" XNMP-Fixer ", "xnmp-fixer", True),
    ("fixer_acme", "FIXER_ACME", True), ("", "", False), (None, "steward", False),
    ("steward[bot][bot]", "steward[bot][bot]", False), ("x" * 100_000, "steward", False),
])
def test_account_comparison_preserves_type(left, right, equal):
    assert account_eq(left, right) is equal


def test_typed_bot_authorship_matches_only_bot_configuration():
    bot = Account("Bot", "STEWARD")
    assert account_eq(bot, "steward[bot]")
    assert not account_eq(bot, "steward")
    assert str(bot) == "steward[bot]"


@pytest.mark.parametrize("login", [None, "", "-leading", "a b", "a" * 40, "a" * 100_000, "a[bot][bot]"])
def test_malformed_configuration_never_names_an_account(login):
    assert configured_account(login) is None


@pytest.mark.parametrize("kind,login", [("Organization", "x"), (None, "x"), ("Bot", ""),
                                        ("User", "x[bot]"), ("Bot", "x" * 100_000)])
def test_an_account_cannot_hold_an_invalid_type_or_login(kind, login):
    with pytest.raises(ValueError):
        Account(kind, login)


@pytest.mark.parametrize("login,bot", [("ci[bot]", True), (" CI[BOT] ", True), ("ci", False),
                                        ("", False), (None, False)])
def test_bot_comment_classification_retains_the_account_type(login, bot):
    assert is_bot_login(login) is bot
