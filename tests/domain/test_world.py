"""Pure world helpers: declared-path extraction + open-PR overlap.

These feed the file-overlap gate (pick_ready_issue): an ai-ready issue whose
body-declared paths overlap an in-flight PR's changed files is held back to avoid
a guaranteed merge conflict (the #59-vs-#60 collision).
"""

from tests.domain.builders import observed_pr


from issue_agent.domain.world import (
    Comment,
)


# ── paths_overlap_open_prs ────────────────────────────────────────────────────
def _pr(number, state, files):
    return observed_pr(number=number, state=state, changed_files=frozenset(files))




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
