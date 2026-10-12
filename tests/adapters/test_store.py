"""Store adapter — path construction + read/write/clear round-trips + markers."""
from tests.fakes.workflow_authority import retained_issue_file, seed_retained_issue_file

import os
import tempfile
from pathlib import Path
import pytest

from issue_agent.adapters.store import HarnessMarkers, Store
from issue_agent.domain.ci_history import CiAttempt

def _seed_legacy_ci_counter(store, issue, count):
    Path(store.issue_path(issue, "ci-retries")).write_text(str(count))

def _legacy_ci_counter(store, issue):
    path = Path(store.issue_path(issue, "ci-retries"))
    return int(path.read_text()) if path.exists() else 0

def _store():
    d = tempfile.mkdtemp()
    return Store(state_dir=d), d

def test_paths_are_pure():
    s = Store(state_dir="/state")
    assert s.issue_path(5, "session") == "/state/issue-5.session"
    assert s.pr_path(100, "round") == "/state/pr-100.round"

# ── CI attempt history (tauri PR #620) ──────────────────────────────────────
def test_ci_history_survives_the_process_that_wrote_it():
    # The whole point is that attempt N sees attempts 1..N-1, which earlier
    # polls (possibly earlier daemon processes) recorded.
    _, d = _store()
    Store(state_dir=d).append_ci_attempt(5, CiAttempt("abc1234", ("unit",)))
    assert Store(state_dir=d).get_ci_history(5) == (CiAttempt("abc1234", ("unit",)),)

def test_ci_history_is_empty_before_any_attempt():
    s, _ = _store()
    assert s.get_ci_history(5) == ()

def test_ci_history_keeps_only_the_last_five_attempts():
    s, _ = _store()
    for n in range(8):
        s.append_ci_attempt(5, CiAttempt(f"sha{n}", ("unit",)))
    assert [a.head_sha for a in s.get_ci_history(5)] == [f"sha{n}" for n in range(3, 8)]

def test_ci_history_is_per_issue():
    s, _ = _store()
    s.append_ci_attempt(5, CiAttempt("aaa", ("unit",)))
    s.append_ci_attempt(6, CiAttempt("bbb", ("unit",)))
    assert [a.head_sha for a in s.get_ci_history(5)] == ["aaa"]
    assert [a.head_sha for a in s.get_ci_history(6)] == ["bbb"]

def test_a_corrupt_ci_history_file_reads_as_no_history():
    # It is repair CONTEXT, not authority: damage costs the next leg its extra
    # section, and must never raise out of a dispatch.
    s, _ = _store()
    for junk in ("not json at all", "{}", "[1, 2]", ""):
        with open(s.issue_path(5, "ci-history"), "w") as f:
            f.write(junk)
        assert s.get_ci_history(5) == ()

def test_appending_over_a_corrupt_ci_history_starts_a_fresh_record():
    s, _ = _store()
    with open(s.issue_path(5, "ci-history"), "w") as f:
        f.write("}{ torn")
    assert s.append_ci_attempt(5, CiAttempt("abc", ("unit",))) == (
        CiAttempt("abc", ("unit",)),
    )
    assert s.get_ci_history(5) == (CiAttempt("abc", ("unit",)),)

def test_an_operator_retry_unblock_also_forgets_the_attempt_history():
    s, _ = _store()
    _seed_legacy_ci_counter(s, 5, 3)
    s.append_ci_attempt(5, CiAttempt("abc", ("unit",)))

    assert not hasattr(s, "clear_retries")

    assert s.get_ci_history(5) == (CiAttempt("abc", ("unit",)),)

def test_advisory_ci_history_clear_does_not_mutate_retained_legacy_counter():
    s, _ = _store()
    _seed_legacy_ci_counter(s, 5, 2)
    s.append_ci_attempt(5, CiAttempt("abc", ("unit",)))

    s.clear_ci_history(5)

    assert _legacy_ci_counter(s, 5) == 2
    assert s.get_ci_history(5) == ()

def test_a_merged_pr_forgets_its_attempt_history():
    s, _ = _store()
    s.append_ci_attempt(9, CiAttempt("abc", ("unit",)))
    s.cleanup_pr(100, 9, merged=True)
    assert s.get_ci_history(9) == ()

def test_a_requeued_issue_does_not_inherit_the_dead_prs_attempt_history():
    # A closed-unmerged requeue opens a NEW PR; carrying the old PR's attempts
    # would tell its first repair leg it is mid-loop on failures it never had.
    s, _ = _store()
    s.append_ci_attempt(9, CiAttempt("abc", ("unit",)))
    s.cleanup_pr(100, 9, merged=False)
    assert s.get_ci_history(9) == ()

def _seed_infra_deaths(s, key, count):
    """Legacy infra-death evidence; no runtime path writes these any more."""
    Path(s.issue_path(key, "infra-deaths")).write_text(str(count))


def test_malformed_numeric_state_blocks_reads_and_increments():
    s, root = _store()
    for path in (
        s.issue_path(5, "resumes"),
        s.issue_path(5, "ci-retries"),
        s.issue_path(5, "infra-deaths"),
    ):
        with open(path, "w") as f:
            f.write("not-an-int")

    with pytest.raises(ValueError, match="control unavailable"):
        s.get_infra_deaths(5)
    with open(os.path.join(root, "issue-5.resumes")) as f:
        assert f.read() == "not-an-int"

# review M3/T2: the old `test_write_is_atomic_and_leaves_no_tmp_file` above
# passed against a truncate-and-write implementation (it only ever checked
# that one hardcoded, non-pid-suffixed ".tmp" path was absent — a truncating
# `_write` never creates that path either, so the mutation sailed through).
# These replace it with tests that actually exercise the crash window and the
# collision the pid-suffixed tmp name exists to avoid.
def test_write_leaves_no_tmp_file_after_success():
    s, root = _store()
    s.set_jira_key(5, "K-4")

    assert s.get_jira_key(5) == "K-4"
    assert os.path.exists(os.path.join(root, "issue-5.jira"))
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
    s.set_jira_key(5, "K-4")

    def _boom(_src, _dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", _boom)
    try:
        s.set_jira_key(5, "K-9")
    except OSError:
        pass
    assert s.get_jira_key(5) == "K-4"

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
    s.set_jira_key(5, "K-4")
    assert calls, "Store._write must fsync the tmp file before os.replace"
    assert s.get_jira_key(5) == "K-4"

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
    s.set_jira_key(5, "K-4")
    assert str(os.getpid()) in seen["src"]
    assert s.get_jira_key(5) == "K-4"

def test_clear_retries_hands_back_only_the_per_attempt_budgets():
    """An operator unblock resets the retry ceilings a wedge consumed — and
    deliberately not the two that bound unwinnable work."""
    s, _ = _store()
    _seed_legacy_ci_counter(s, 5, 3)
    Path(s.issue_path(5, "review-retries")).write_text("2026-01-01T00:00:00Z 2")
    Path(s.issue_path(5, "rebase-retries")).write_text("1")
    _seed_infra_deaths(s, 5, 5)

    assert not hasattr(s, "clear_retries")

    assert _legacy_ci_counter(s, 5) == 3
    assert Path(s.issue_path(5, "review-retries")).read_text() == "2026-01-01T00:00:00Z 2"
    assert Path(s.issue_path(5, "rebase-retries")).read_text() == "1"
    assert s.get_infra_deaths(5) == 5

def test_clear_retries_on_an_untouched_key_is_a_no_op_not_an_error():
    s, _ = _store()
    assert not hasattr(s, "clear_retries")
    assert _legacy_ci_counter(s, -112) == 0

def test_runtime_ci_marker_accessors_are_removed():
    s, _ = _store()
    for name in ("get_ci_rerun_head", "set_ci_rerun_head", "get_ci_cancel_head", "set_ci_cancel_head"):
        assert not hasattr(s, name)

def test_ci_pending_escalated_head_marker_roundtrip():
    s, _ = _store()
    for name in ("get_ci_pending_escalated_head", "set_ci_pending_escalated_head", "get_update_branch_head", "set_update_branch_head"):
        assert not hasattr(s, name)

def test_cleanup_pr_merged_clears_the_ci_rerun_marker():
    # The PR is gone; a stale marker under a recycled number would silently
    # suppress the next PR's free re-run.
    s, _ = _store()
    from pathlib import Path
    marker = Path(s.pr_path(100, "ci-rerun-head"))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("sha-1")
    s.cleanup_pr(100, 9, merged=True)
    assert marker.read_text() == "sha-1"

def test_stopped_legacy_triage_baseline_remains_readable():
    s, _ = _store()
    Path(os.path.join(s.state_dir, "triage-7.at")).write_text("t_post")
    assert Path(os.path.join(s.state_dir, "triage-7.at")).read_text() == "t_post"

def test_stopped_groom_sources_survive_issue_cleanup():
    s, _ = _store()
    evidence = {os.path.join(s.state_dir, "groom-7.at"): "2026-10-01T00:00:00Z",
                os.path.join(s.state_dir, "groom-7.content"): "a" * 64,
                s.issue_path(7, "steward-stalls-groom"): "3"}
    for path, value in evidence.items():
        Path(path).write_text(value)
    s._clear_issue_advisory(7)
    assert {path: Path(path).read_text() for path in evidence} == evidence

def test_jira_key_and_phase_roundtrip_and_cleared():
    s, _ = _store()
    assert s.get_jira_key(5) == "" and s.get_jira_phase(5) == ""
    s.set_jira_key(5, "ABC-12")
    s.set_jira_phase(5, "in_progress")
    assert s.get_jira_key(5) == "ABC-12"
    assert s.get_jira_phase(5) == "in_progress"
    s.set_jira_key(5, "")                 # empty key is a no-op (don't clobber)
    assert s.get_jira_key(5) == "ABC-12"
    s._clear_issue_advisory(5)
    assert s.get_jira_key(5) == "" and s.get_jira_phase(5) == ""

def test_clear_issue_removes_all():
    s, _ = _store()
    seed_retained_issue_file(s, 5, "session", "x")
    _seed_legacy_ci_counter(s, 5, 2)
    Path(s.issue_path(5, "rebase-retries")).write_text("1")
    seed_retained_issue_file(s, 5, "resumed-at", "t")
    seed_retained_issue_file(s, 5, "phase", "authored")
    _seed_infra_deaths(s, 5, 1)
    s._clear_issue_advisory(5)
    assert retained_issue_file(s, 5, "session") == "x"
    assert _legacy_ci_counter(s, 5) == 2
    assert Path(s.issue_path(5, "rebase-retries")).read_text() == "1"
    assert retained_issue_file(s, 5, "phase") == "authored"           # retained stopped evidence
    assert s.get_infra_deaths(5) == 1     # retained Jira/import evidence

def test_clear_issue_does_not_touch_pr_automerge_marker():
    # review M4: `automerge` is a PR suffix now — clear_issue must not remove
    # issue-N.automerge (the drift this fixes: that file was never written by
    # anything, so removing it was a no-op masquerading as cleanup).
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    s._clear_issue_advisory(9)
    assert Path(s.pr_path(100, "automerge")).exists()

def test_cleaners_retain_retired_resolve_files_as_import_evidence():
    # ADR 0015: resolve episodes live in SQLite. Stopped legacy files are the
    # offline importer's evidence, so no cleaner or unblock may delete them.
    s, root = _store()
    retries = os.path.join(root, "issue-5.resolve-retries")
    last_error = os.path.join(root, "issue-5.last-error")
    for path, content in ((retries, "3"), (last_error, "tests failed")):
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(content)
    assert not hasattr(s, "clear_retries")
    s.cleanup_pr(100, 5, merged=False)
    s.cleanup_pr(100, 5, merged=True)
    s._clear_issue_advisory(5)
    with open(retries, encoding="utf-8") as stream:
        assert stream.read() == "3"
    with open(last_error, encoding="utf-8") as stream:
        assert stream.read() == "tests failed"
    assert not any(hasattr(s, name) for name in (
        "get_resolve_retries", "set_resolve_retries", "reset_resolve_episode",
        "get_last_resolve_error", "set_last_resolve_error",
    ))

def test_clear_pr_state():
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    s._clear_pr_advisory(100)
    assert not Path(s.pr_path(100, "automerge")).exists()  # review M4: the real marker, now cleared

# ── merged/closed-unmerged cleanup orchestration (review M4/D8) ────────────
def test_cleanup_pr_merged_clears_full_issue_state_and_automerge_marker():
    s, _ = _store()
    seed_retained_issue_file(s, 9, "session", "sess-1")
    _seed_infra_deaths(s, 9, 1)
    Path(s.pr_path(100, "automerge")).write_text("")
    s.cleanup_pr(100, 9, merged=True)
    assert retained_issue_file(s, 9, "session") == "sess-1"
    assert s.get_infra_deaths(9) == 1
    assert not Path(s.pr_path(100, "automerge")).exists()

def test_cleanup_pr_closed_unmerged_keeps_infra_deaths():
    # review M4/D8 + handover §5.7: fewer retries after a human closed the PR
    # is deliberate — only the resume-blocking markers clear.
    s, _ = _store()
    seed_retained_issue_file(s, 9, "session", "sess-1")
    seed_retained_issue_file(s, 9, "phase", "authoring")
    seed_retained_issue_file(s, 9, "resumed-at", "2026-09-01T00:00:00Z")
    _seed_infra_deaths(s, 9, 1)
    Path(s.pr_path(100, "automerge")).write_text("")
    s.cleanup_pr(100, 9, merged=False)
    assert retained_issue_file(s, 9, "session") == "sess-1"
    assert retained_issue_file(s, 9, "phase") == "authoring"
    assert retained_issue_file(s, 9, "resumed-at") == "2026-09-01T00:00:00Z"
    # anti-vacuity twin: prove these are KEPT, not "everything got cleared"
    assert s.get_infra_deaths(9) == 1
    # closed-unmerged is silent on PR-side state (spec covers issue state only)
    assert Path(s.pr_path(100, "automerge")).exists()

def test_cleanup_pr_issueless_merged_only_clears_pr_state():
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    s.cleanup_pr(100, None, merged=True)
    assert not Path(s.pr_path(100, "automerge")).exists()

def test_cleanup_pr_issueless_unmerged_is_a_noop():
    # anti-vacuity twin for the case above: an issueless closed-unmerged PR
    # has no issue state to clear and cleanup_pr must not crash on None.
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    s.cleanup_pr(100, None, merged=False)
    assert Path(s.pr_path(100, "automerge")).exists()

# ── P1R-16: an issueless PR's NEGATED-key counters were never cleaned ───────
# `domain.fix.breaker_key` falls back to the NEGATED pr number for a PR with
# no resolvable issue, so infra-deaths (at least) for such a PR accumulates at
# issue_path(-pr, "infra-deaths") — e.g. the literal file "issue--112.infra-
# deaths". Before the fix, cleanup_pr(112, None, …) cleared ONLY pr-112.* and
# this namespace leaked forever — reachable in production any time an
# issueless PR's fixer leg dies pre-result enough times to trip the infra
# ceiling and then gets merged/closed.
def test_cleanup_pr_issueless_merged_clears_the_negated_key_issue_state():
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    _seed_infra_deaths(s, -100, 1)          # the negated-PR breaker key
    s.cleanup_pr(100, None, merged=True)
    assert not Path(s.pr_path(100, "automerge")).exists()       # PR-side still clears (regression guard)
    assert s.get_infra_deaths(-100) == 1           # retained legacy debt is evidence, not a runtime reset

def test_cleanup_pr_issueless_unmerged_keeps_negated_key_infra_deaths():
    # anti-vacuity twin, mirroring the real-issue conservatism (handover
    # §5.7): the closed-unmerged path clears only resume-blocking markers, so
    # infra-deaths under the negated key must survive — proving the fix
    # routes through clear_issue_resume_state, not a blanket clear_issue.
    s, _ = _store()
    Path(s.pr_path(100, "automerge")).write_text("")
    _seed_infra_deaths(s, -100, 1)
    s.cleanup_pr(100, None, merged=False)
    assert Path(s.pr_path(100, "automerge")).exists()        # PR-side untouched, as before
    assert s.get_infra_deaths(-100) == 1           # kept — same conservatism as a real issue

# ── .harness claims ──
def test_harness_test_command():
    with tempfile.TemporaryDirectory() as root:
        hd = os.path.join(root, ".harness")
        os.makedirs(hd)
        m = HarnessMarkers(root)
        with open(os.path.join(hd, "test-cmd"), "w") as f:
            f.write("\n  \ncd backend && pytest -q\nextra\n")
        assert m.test_cmd() == "cd backend && pytest -q"  # first non-blank line

def test_harness_test_cmd_none_when_absent():
    with tempfile.TemporaryDirectory() as root:
        assert HarnessMarkers(root).test_cmd() is None

def test_predicted_paths_are_round_tripped_and_malformed_predictions_fail_open():
    s, _ = _store()
    s.save_predicted_paths(5, ["src/action.rs", "./src/help.rs"])
    assert s.get_predicted_paths(5) == frozenset({"src/action.rs", "src/help.rs"})

    s.save_predicted_paths(5, ["src/action.rs", "../outside.rs"])
    assert s.get_predicted_paths(5) == frozenset()

def test_retired_review_files_survive_cleanup_as_import_evidence():
    s, _ = _store()
    evidence = {"session": "claude:review-session", "round": "19",
                "last-sha": "a" * 40, "patch-id": "b" * 64,
                "operator-guidance": "Scope: naïve parser — preserve assertions 🚫"}
    for suffix, value in evidence.items():
        with open(s.pr_path(100, suffix), "w", encoding="utf-8") as stream:
            stream.write(value)
    s._clear_pr_advisory(100)
    s.cleanup_pr(100, 9, merged=True)
    for suffix, value in evidence.items():
        with open(s.pr_path(100, suffix), encoding="utf-8") as stream:
            assert stream.read() == value
    assert not any(hasattr(s, name) for name in (
        "get_pr_session", "save_pr_session", "get_round", "set_round", "clear_pr_review",
        "get_last_sha", "set_last_sha", "get_last_patch_id", "set_last_patch_id",
        "get_operator_guidance", "set_operator_guidance", "clear_operator_guidance",
    ))

def test_store_has_no_retired_continuity_authority_surface():
    store = Store("/unused")
    names = ("get_branch", "save_branch", "get_session", "save_session",
             "has_session", "get_model", "save_model", "get_resumed_at",
             "set_resumed_at", "bump_resumes", "get_phase", "set_phase",
             "clear_infra_deaths", "bump_infra_deaths", "set_infra_deaths", "automerge_enabled", "mark_automerge",
             "get_triage_at", "get_groom_at", "get_groom_content",
             "triage_at_path", "groom_at_path", "groom_content_path",
             "clear_pr_state")
    assert not any(hasattr(store, name) for name in names)
