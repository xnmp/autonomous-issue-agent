"""Store adapter — path construction + read/write/clear round-trips + markers."""

import inspect
import os
import re
import tempfile

from issue_agent.adapters import store as store_module
from issue_agent.adapters.store import HarnessMarkers, Store


def _store():
    d = tempfile.mkdtemp()
    return Store(state_dir=d), d


def test_paths_are_pure():
    s = Store(state_dir="/state")
    assert s.issue_path(5, "session") == "/state/issue-5.session"
    assert s.pr_path(100, "round") == "/state/pr-100.round"
    assert s.triage_at_path(7) == "/state/triage-7.at"


def test_session_roundtrip():
    s, _ = _store()
    assert s.has_session(5) is False
    s.save_session(5, "sess-9")
    assert s.get_session(5) == "sess-9"
    assert s.has_session(5) is True


def test_empty_session_not_written():
    s, _ = _store()
    s.save_session(5, "")
    assert s.has_session(5) is False


def test_resumed_at_and_resumes_counter():
    s, _ = _store()
    s.set_resumed_at(5, "t2")
    assert s.get_resumed_at(5) == "t2"
    assert s.bump_resumes(5) == 1
    assert s.bump_resumes(5) == 2


def test_ci_retries():
    s, _ = _store()
    assert s.get_ci_retries(5) == 0
    s.set_ci_retries(5, 3)
    assert s.get_ci_retries(5) == 3


def test_malformed_numeric_state_fails_soft_to_defaults():
    s, root = _store()
    for path in (
        s.issue_path(5, "resumes"),
        s.issue_path(5, "ci-retries"),
        s.issue_path(5, "rebase-retries"),
        s.issue_path(5, "review-retries"),
        s.pr_path(100, "round"),
    ):
        with open(path, "w") as f:
            f.write("not-an-int")

    assert s.get_ci_retries(5) == 0
    assert s.get_rebase_retries(5) == 0
    assert s.get_review_retry(5) == ("", 0)
    assert s.get_round(100) == 1
    assert s.bump_resumes(5) == 1
    with open(os.path.join(root, "issue-5.resumes")) as f:
        assert f.read() == "1"


# review M3/T2: the old `test_write_is_atomic_and_leaves_no_tmp_file` above
# passed against a truncate-and-write implementation (it only ever checked
# that one hardcoded, non-pid-suffixed ".tmp" path was absent — a truncating
# `_write` never creates that path either, so the mutation sailed through).
# These replace it with tests that actually exercise the crash window and the
# collision the pid-suffixed tmp name exists to avoid.
def test_write_leaves_no_tmp_file_after_success():
    s, root = _store()
    s.set_ci_retries(5, 4)

    assert s.get_ci_retries(5) == 4
    assert os.path.exists(os.path.join(root, "issue-5.ci-retries"))
    leftover = [f for f in os.listdir(root) if f.endswith(".tmp")]
    assert leftover == []


def test_write_survives_crash_before_replace(monkeypatch):
    # The honest atomicity test: os.replace fails AFTER the tmp file is fully
    # written (simulating a kill/power-loss between fsync and rename). The
    # real state file must still hold the OLD value — never a torn or
    # zero-length one. A truncate-and-write `_write` has no os.replace call to
    # fail, so it would silently install the new (wrong-for-this-test) value
    # and this assertion would catch it.
    s, _ = _store()
    s.set_ci_retries(5, 4)

    def _boom(_src, _dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", _boom)
    try:
        s.set_ci_retries(5, 9)
    except OSError:
        pass
    assert s.get_ci_retries(5) == 4


def test_write_syncs_tmp_file_before_replace(monkeypatch):
    # review M3: fsync narrows the crash window a bare os.replace(tmp, path)
    # leaves open — without it, a torn tmp file (still in the page-cache, not
    # on disk) could get installed as the real state file by a kill between
    # write() and replace().
    s, _ = _store()
    calls = []
    real_fsync = os.fsync

    def _tracking_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _tracking_fsync)
    s.set_ci_retries(5, 4)
    assert calls, "Store._write must fsync the tmp file before os.replace"
    assert s.get_ci_retries(5) == 4


def test_tmp_name_is_pid_suffixed(monkeypatch):
    # review M3: two daemons (fix + review) sharing a STATE_DIR, or a
    # restarted process racing its predecessor, must not collide on one
    # shared "<path>.tmp" name and truncate each other's in-flight write.
    s, _ = _store()
    seen = {}
    real_replace = os.replace

    def _capture(src, dst):
        seen["src"] = src
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _capture)
    s.set_ci_retries(5, 4)
    assert str(os.getpid()) in seen["src"]
    assert s.get_ci_retries(5) == 4


def test_review_retry_pair():
    s, _ = _store()
    assert s.get_review_retry(5) == ("", 0)
    s.set_review_retry(5, "2026-01-01T00:00:00Z", 2)
    assert s.get_review_retry(5) == ("2026-01-01T00:00:00Z", 2)
    s.clear_review_retry(5)
    assert s.get_review_retry(5) == ("", 0)


def test_last_sha_and_round():
    s, _ = _store()
    assert s.get_round(100) == 1  # default
    s.set_last_sha(100, "abc")
    s.set_round(100, 3)
    assert s.get_last_sha(100) == "abc"
    assert s.get_round(100) == 3


def test_automerge_marker():
    s, _ = _store()
    assert s.automerge_enabled(100) is False
    s.mark_automerge(100)
    assert s.automerge_enabled(100) is True


def test_triage_at():
    s, _ = _store()
    s.set_triage_at(7, "t_post")
    assert s.get_triage_at(7) == "t_post"


def test_phase_roundtrip():
    s, _ = _store()
    assert s.get_phase(5) == ""           # default: not started / single-pass
    s.set_phase(5, "authored")
    assert s.get_phase(5) == "authored"


def test_jira_key_and_phase_roundtrip_and_cleared():
    s, _ = _store()
    assert s.get_jira_key(5) == "" and s.get_jira_phase(5) == ""
    s.set_jira_key(5, "ABC-12")
    s.set_jira_phase(5, "in_progress")
    assert s.get_jira_key(5) == "ABC-12"
    assert s.get_jira_phase(5) == "in_progress"
    s.set_jira_key(5, "")                 # empty key is a no-op (don't clobber)
    assert s.get_jira_key(5) == "ABC-12"
    s.clear_issue(5)
    assert s.get_jira_key(5) == "" and s.get_jira_phase(5) == ""


def test_clear_issue_removes_all():
    s, _ = _store()
    s.save_session(5, "x")
    s.set_ci_retries(5, 2)
    s.set_resolve_retries(5, 2)
    s.set_rebase_retries(5, 1)
    s.set_resumed_at(5, "t")
    s.set_phase(5, "authored")
    s.bump_infra_deaths(5)
    s.clear_issue(5)
    assert not s.has_session(5)
    assert s.get_ci_retries(5) == 0
    assert s.get_resolve_retries(5) == 0  # resolve breaker resets with the issue
    assert s.get_rebase_retries(5) == 0
    assert s.get_phase(5) == ""           # phase cleared on teardown
    assert s.get_infra_deaths(5) == 0     # review M4/D8: infra-deaths clears too


def test_clear_issue_does_not_touch_pr_automerge_marker():
    # review M4: `automerge` is a PR suffix now — clear_issue must not remove
    # issue-N.automerge (the drift this fixes: that file was never written by
    # anything, so removing it was a no-op masquerading as cleanup).
    s, _ = _store()
    s.mark_automerge(100)
    s.clear_issue(9)
    assert s.automerge_enabled(100) is True


def test_resolve_retries_roundtrip():
    s, _ = _store()
    assert s.get_resolve_retries(5) == 0
    s.set_resolve_retries(5, 2)
    assert s.get_resolve_retries(5) == 2


def test_clear_pr_review():
    s, _ = _store()
    s.save_pr_session(100, "rs")
    s.set_last_sha(100, "abc")
    s.mark_automerge(100)
    s.clear_pr_review(100)
    assert s.get_pr_session(100) == ""
    assert s.get_last_sha(100) == ""
    assert s.automerge_enabled(100) is False  # review M4: the real marker, now cleared


# ── suffix-coverage (review M4/D8) ──────────────────────────────────────────
def _accessor_suffixes(path_method: str) -> set[str]:
    """Suffix literals Store passes as the 2nd positional arg to `path_method`
    (issue_path/pr_path) anywhere in its own source. clear_issue/clear_pr_review/
    clear_issue_resume_state loop over a suffix VARIABLE (from the module
    constants), never a quoted literal, so they don't inflate this set with
    themselves — only genuine accessors do."""
    src = inspect.getsource(Store)
    pattern = rf'self\.{path_method}\([^,]+,\s*"([\w-]+)"\)'
    return set(re.findall(pattern, src))


def test_every_issue_accessor_suffix_is_covered_by_exactly_one_cleaner():
    # review M4: a new accessor with an unlisted suffix must fail this test
    # instead of silently leaking a STATE_DIR file forever (introspects the
    # module: accessors' suffix strings vs _ISSUE_SUFFIXES).
    assert _accessor_suffixes("issue_path") == set(store_module._ISSUE_SUFFIXES)


def test_every_pr_accessor_suffix_is_covered_by_exactly_one_cleaner():
    # Same check for the PR namespace — this is what catches the `automerge`
    # drift: automerge_enabled/mark_automerge use pr_path, so `automerge` must
    # be in _PR_SUFFIXES (it used to sit, unused, in the issue tuple instead).
    assert _accessor_suffixes("pr_path") == set(store_module._PR_SUFFIXES)


# ── merged/closed-unmerged cleanup orchestration (review M4/D8) ────────────
def test_cleanup_pr_merged_clears_full_issue_state_and_automerge_marker():
    s, _ = _store()
    s.save_session(9, "sess-1")
    s.set_resolve_retries(9, 3)
    s.bump_infra_deaths(9)
    s.mark_automerge(100)
    s.cleanup_pr(100, 9, merged=True)
    assert s.get_session(9) == ""
    assert s.get_resolve_retries(9) == 0
    assert s.get_infra_deaths(9) == 0
    assert s.automerge_enabled(100) is False


def test_cleanup_pr_closed_unmerged_keeps_resolve_retries_and_infra_deaths():
    # review M4/D8 + handover §5.7: fewer retries after a human closed the PR
    # is deliberate — only the resume-blocking markers clear.
    s, _ = _store()
    s.save_session(9, "sess-1")
    s.set_phase(9, "authoring")
    s.set_resumed_at(9, "t1")
    s.set_resolve_retries(9, 3)
    s.bump_infra_deaths(9)
    s.mark_automerge(100)
    s.cleanup_pr(100, 9, merged=False)
    assert s.get_session(9) == ""
    assert s.get_phase(9) == ""
    assert s.get_resumed_at(9) == ""
    # anti-vacuity twin: prove these are KEPT, not "everything got cleared"
    assert s.get_resolve_retries(9) == 3
    assert s.get_infra_deaths(9) == 1
    # closed-unmerged is silent on PR-side state (spec covers issue state only)
    assert s.automerge_enabled(100) is True


def test_cleanup_pr_issueless_merged_only_clears_pr_state():
    s, _ = _store()
    s.mark_automerge(100)
    s.cleanup_pr(100, None, merged=True)
    assert s.automerge_enabled(100) is False


def test_cleanup_pr_issueless_unmerged_is_a_noop():
    # anti-vacuity twin for the case above: an issueless closed-unmerged PR
    # has no issue state to clear and cleanup_pr must not crash on None.
    s, _ = _store()
    s.mark_automerge(100)
    s.cleanup_pr(100, None, merged=False)
    assert s.automerge_enabled(100) is True


# ── .harness markers ──
def test_harness_markers():
    with tempfile.TemporaryDirectory() as root:
        hd = os.path.join(root, ".harness")
        os.makedirs(hd)
        m = HarnessMarkers(root)
        assert m.ac_posted() is False
        open(os.path.join(hd, "ac-posted"), "w").close()
        open(os.path.join(hd, "test-plan-posted"), "w").close()
        with open(os.path.join(hd, "adversarial-verdict"), "w") as f:
            f.write("PASS looks good\n")
        with open(os.path.join(hd, "test-cmd"), "w") as f:
            f.write("\n  \ncd backend && pytest -q\nextra\n")
        assert m.ac_posted() is True
        assert m.test_plan_posted() is True
        assert m.adversarial_verdict() == "PASS looks good"
        assert m.test_cmd() == "cd backend && pytest -q"  # first non-blank line


def test_harness_test_cmd_none_when_absent():
    with tempfile.TemporaryDirectory() as root:
        assert HarnessMarkers(root).test_cmd() is None


def test_visual_ac_and_screenshots_read_markers():
    with tempfile.TemporaryDirectory() as root:
        hd = os.path.join(root, ".harness")
        os.makedirs(hd)
        m = HarnessMarkers(root)
        assert m.visual_ac() is False
        assert m.screenshots_read() == ()
        open(os.path.join(hd, "visual-ac"), "w").close()
        with open(os.path.join(hd, "screenshots-read"), "w") as f:
            f.write("tab-title.png\n\nchart.png\n")
        assert m.visual_ac() is True
        assert m.screenshots_read() == ("tab-title.png", "chart.png")  # blanks dropped
