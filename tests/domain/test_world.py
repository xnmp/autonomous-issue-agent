"""Pure world helpers: declared-path extraction + open-PR overlap.

These feed the file-overlap gate (pick_ready_issue): an ai-ready issue whose
body-declared paths overlap an in-flight PR's changed files is held back to avoid
a guaranteed merge conflict (the #59-vs-#60 collision).
"""

from tests.domain.builders import observed_pr

import pytest

from tests.domain.builders import Check, issue, pr, world

from issue_agent.domain.labels import Label

from issue_agent.domain.world import (
    Comment,
    PRView,
    extract_declared_paths,
    normalize_predicted_paths,
)


# ── extract_declared_paths ────────────────────────────────────────────────────
def test_extract_picks_backticked_paths():
    body = "Add `backend/app/services/conversation_service.py` and update `services/fakes.py`."
    got = extract_declared_paths(body)
    assert got == frozenset(
        {"backend/app/services/conversation_service.py", "services/fakes.py"}
    )


def test_extract_matches_bare_extension_file():
    assert extract_declared_paths("touch `foo.ts` and `bar.py`") == frozenset(
        {"foo.ts", "bar.py"}
    )


def test_extract_ignores_prose_in_backticks():
    # backticked words that aren't paths (no slash, no source extension) are ignored.
    assert extract_declared_paths("set `status` to `True` in the `handler`") == frozenset()


def test_extract_strips_leading_dot_slash():
    assert extract_declared_paths("see `./backend/app/x.py`") == frozenset(
        {"backend/app/x.py"}
    )


def test_extract_empty_body():
    assert extract_declared_paths("") == frozenset()
    assert extract_declared_paths(None) == frozenset()  # tolerant of None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["src/action.rs", "./src/help.rs"], frozenset({"src/action.rs", "src/help.rs"})),
        (["src/action.rs", 7], frozenset()),
        ([f"src/{n}.py" for n in range(33)], frozenset()),
        (["/etc/passwd"], frozenset()),
        (["src\\windows.py"], frozenset()),
        ([" src/action.rs"], frozenset()),
        ("src/action.rs", frozenset()),
        (None, frozenset()),
    ],
)
def test_normalize_predicted_paths_fails_open_for_untrusted_router_output(raw, expected):
    assert normalize_predicted_paths(raw) == expected


# ── paths_overlap_open_prs ────────────────────────────────────────────────────
def _pr(number, state, files):
    return observed_pr(number=number, state=state, changed_files=frozenset(files))


IN_FLIGHT = world(prs=[pr(7, "ai-pr-open", head_ref_oid="sha1")])


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
