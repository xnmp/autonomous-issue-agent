"""Pure world helpers: declared-path extraction + open-PR overlap.

These feed the file-overlap gate (pick_ready_issue): an ai-ready issue whose
body-declared paths overlap an in-flight PR's changed files is held back to avoid
a guaranteed merge conflict (the #59-vs-#60 collision).
"""

import pytest

from builders import issue, pr, world

from issue_agent.domain.world import (
    Comment,
    PRView,
    extract_declared_paths,
    fix_probe_safe,
    normalize_predicted_paths,
    paths_overlap_open_prs,
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
    return PRView(number=number, state=state, changed_files=frozenset(files))


def test_overlap_true_when_declared_hits_open_pr():
    prs = (_pr(1, "OPEN", {"a.py", "b.py"}),)
    assert paths_overlap_open_prs(frozenset({"b.py"}), prs) is True


def test_overlap_false_when_no_common_file():
    prs = (_pr(1, "OPEN", {"a.py"}),)
    assert paths_overlap_open_prs(frozenset({"z.py"}), prs) is False


def test_overlap_ignores_non_open_prs():
    prs = (_pr(1, "MERGED", {"a.py"}), _pr(2, "CLOSED", {"a.py"}))
    assert paths_overlap_open_prs(frozenset({"a.py"}), prs) is False


def test_overlap_empty_declared_is_failopen():
    prs = (_pr(1, "OPEN", {"a.py"}),)
    assert paths_overlap_open_prs(frozenset(), prs) is False


# ── fix_probe_safe (#3: when may the fixer coast on the cheap signature?) ────
# The 1-point change signature is blind to CI completion and mergeability flips.
# Those only matter while something is in flight, so "nothing in flight" is the
# whole predicate — and it must read OPEN state, not just labels.
def test_probe_safe_on_an_empty_world():
    assert fix_probe_safe(world()) is True


def test_probe_unsafe_while_a_fixer_holds_an_issue():
    assert fix_probe_safe(world(issues=[issue(1, "ai-working")])) is False


def test_probe_unsafe_while_a_harness_pr_is_open():
    # This is the CI/mergeability case: the fixer acts on checks and
    # mergeStateStatus for this PR, neither of which bumps updated_at.
    assert fix_probe_safe(world(prs=[pr(7, "ai-pr-open")])) is False


def test_probe_safe_when_the_labelled_pr_is_already_merged_or_closed():
    # Lifecycle labels outlive the PR; only OPEN ones are in flight.
    merged = world(prs=[pr(7, "ai-pr-open", state="MERGED")])
    closed = world(prs=[pr(8, "ai-pr-open", state="CLOSED")])
    assert (fix_probe_safe(merged), fix_probe_safe(closed)) == (True, True)


def test_probe_safe_when_the_ai_working_issue_is_closed():
    assert fix_probe_safe(world(issues=[issue(1, "ai-working", state="closed")])) is True


def test_probe_safe_with_only_queued_and_finished_work():
    # ai-ready is NOT in flight: labelling it bumped updated_at, so the very
    # signature the probe reads already announced it. ai-done is history.
    w = world(
        issues=[issue(1, "ai-ready"), issue(2, "ai-done")],
        prs=[pr(3, state="OPEN")],  # an unlabelled human PR is not the fixer's
    )
    assert fix_probe_safe(w) is True


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
