"""Pure world helpers."""

from issue_agent.domain.world import Comment


# ── Comment.is_bot (review R2-11: routed through identity.is_bot_login) ──────
def test_comment_is_bot_true_for_bot_suffixed_author():
    assert Comment(author="dependabot[bot]").is_bot is True


def test_comment_is_bot_false_for_human_author():
    # Anti-vacuity twin.
    assert Comment(author="a-human").is_bot is False


def test_comment_is_bot_tolerates_case_and_whitespace():
    # Same tolerance identity.py's login comparisons get everywhere else — a
    # raw `.endswith("[bot]")` (the pre-fix implementation) would miss both.
    assert Comment(author="DEPENDABOT[BOT]").is_bot is True
    assert Comment(author=" dependabot[bot] ").is_bot is True
