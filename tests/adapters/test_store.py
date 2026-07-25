"""Store adapter — path construction + read/write/clear round-trips + markers."""

import os
import tempfile

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


def test_write_is_atomic_and_leaves_no_tmp_file():
    s, root = _store()
    s.set_ci_retries(5, 4)

    assert s.get_ci_retries(5) == 4
    assert os.path.exists(os.path.join(root, "issue-5.ci-retries"))
    assert not os.path.exists(os.path.join(root, "issue-5.ci-retries.tmp"))


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
    s.clear_issue(5)
    assert not s.has_session(5)
    assert s.get_ci_retries(5) == 0
    assert s.get_resolve_retries(5) == 0  # resolve breaker resets with the issue
    assert s.get_rebase_retries(5) == 0
    assert s.get_phase(5) == ""           # phase cleared on teardown


def test_resolve_retries_roundtrip():
    s, _ = _store()
    assert s.get_resolve_retries(5) == 0
    s.set_resolve_retries(5, 2)
    assert s.get_resolve_retries(5) == 2


def test_clear_pr_review():
    s, _ = _store()
    s.save_pr_session(100, "rs")
    s.set_last_sha(100, "abc")
    s.clear_pr_review(100)
    assert s.get_pr_session(100) == ""
    assert s.get_last_sha(100) == ""


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
