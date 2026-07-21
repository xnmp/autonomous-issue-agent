"""Pure world helpers: declared-path extraction + open-PR overlap.

These feed the file-overlap gate (pick_ready_issue): an ai-ready issue whose
body-declared paths overlap an in-flight PR's changed files is held back to avoid
a guaranteed merge conflict (the #59-vs-#60 collision).
"""

from issue_agent.domain.world import (
    PRView,
    extract_declared_paths,
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
