"""Store adapter — path construction + read/write/clear round-trips + markers."""

import inspect
import os
import re
import tempfile
from pathlib import Path
import pytest

from issue_agent.adapters import store as store_module
from issue_agent.adapters.store import HarnessMarkers, Store
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.ci_history import CiAttempt
from issue_agent.domain.triage import grooming_candidates, grooming_content
from issue_agent.domain.world import IssueView, WorldState


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
    s.set_resumed_at(5, "2026-01-01T00:00:02Z")
    assert s.get_resumed_at(5) == "2026-01-01T00:00:02Z"
    assert s.bump_resumes(5) == 1
    assert s.bump_resumes(5) == 2


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

    assert s.clear_retries(5) == 1  # counters retired — history is not one

    assert s.get_ci_history(5) == ()


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


def test_malformed_numeric_state_blocks_reads_and_increments():
    s, root = _store()
    for path in (
        s.issue_path(5, "resumes"),
        s.issue_path(5, "ci-retries"),
        s.issue_path(5, "infra-deaths"),
    ):
        with open(path, "w") as f:
            f.write("not-an-int")

    for read in (lambda: s.get_infra_deaths(5),
                 lambda: s.bump_resumes(5)):
        with pytest.raises(ValueError, match="control unavailable"):
            read()
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
    s.set_infra_deaths(5, 4)

    assert s.get_infra_deaths(5) == 4
    assert os.path.exists(os.path.join(root, "issue-5.infra-deaths"))
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
    s.set_infra_deaths(5, 4)

    def _boom(_src, _dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", _boom)
    try:
        s.set_infra_deaths(5, 9)
    except OSError:
        pass
    assert s.get_infra_deaths(5) == 4


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
    s.set_infra_deaths(5, 4)
    assert calls, "Store._write must fsync the tmp file before os.replace"
    assert s.get_infra_deaths(5) == 4


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
    s.set_infra_deaths(5, 4)
    assert str(os.getpid()) in seen["src"]
    assert s.get_infra_deaths(5) == 4




def test_clear_retries_hands_back_only_the_per_attempt_budgets():
    """An operator unblock resets the retry ceilings a wedge consumed — and
    deliberately not the two that bound unwinnable work."""
    s, _ = _store()
    _seed_legacy_ci_counter(s, 5, 3)
    Path(s.issue_path(5, "review-retries")).write_text("2026-01-01T00:00:00Z 2")
    Path(s.issue_path(5, "rebase-retries")).write_text("1")
    s.set_infra_deaths(5, 5)

    assert s.clear_retries(5) == 3  # reports what it actually retired

    assert _legacy_ci_counter(s, 5) == 0
    assert not Path(s.issue_path(5, "review-retries")).exists()
    assert not Path(s.issue_path(5, "rebase-retries")).exists()
    assert s.get_infra_deaths(5) == 5


def test_clear_retries_on_an_untouched_key_is_a_no_op_not_an_error():
    s, _ = _store()
    assert s.clear_retries(-112) == 0  # the negated-PR breaker key namespace
    assert _legacy_ci_counter(s, -112) == 0


def test_automerge_marker():
    s, _ = _store()
    assert s.automerge_enabled(100) is False
    s.mark_automerge(100)
    assert s.automerge_enabled(100) is True


def test_ci_rerun_head_marker_roundtrip():
    # #38: the head SHA whose ONE free CI re-run has been spent. Absent reads as
    # "" — a fresh PR must never look like it already used its allowance.
    s, _ = _store()
    assert s.get_ci_rerun_head(100) == ""
    s.set_ci_rerun_head(100, "sha-1")
    assert s.get_ci_rerun_head(100) == "sha-1"
    s.set_ci_rerun_head(100, "sha-2")           # a push moves it, never appends
    assert s.get_ci_rerun_head(100) == "sha-2"


def test_ci_rerun_head_ignores_an_empty_sha():
    # An unknown head cannot key the allowance; writing "" would make the marker
    # read as "never re-run" anyway, so refuse the write rather than leave a file
    # that says nothing.
    s, _ = _store()
    s.set_ci_rerun_head(100, "sha-1")
    s.set_ci_rerun_head(100, "")
    assert s.get_ci_rerun_head(100) == "sha-1"


def test_ci_pending_escalated_head_marker_roundtrip():
    s, _ = _store()
    assert s.get_ci_pending_escalated_head(100) == ""
    s.set_ci_pending_escalated_head(100, "sha-1")
    assert s.get_ci_pending_escalated_head(100) == "sha-1"


def test_cleanup_pr_merged_clears_the_ci_rerun_marker():
    # The PR is gone; a stale marker under a recycled number would silently
    # suppress the next PR's free re-run.
    s, _ = _store()
    s.set_ci_rerun_head(100, "sha-1")
    s.cleanup_pr(100, 9, merged=True)
    assert s.get_ci_rerun_head(100) == ""


def test_stopped_legacy_triage_baseline_remains_readable():
    s, _ = _store()
    Path(s.triage_at_path(7)).write_text("t_post")
    assert s.get_triage_at(7) == "t_post"


def test_stopped_groom_sources_survive_issue_cleanup():
    s, _ = _store()
    evidence = {s.groom_at_path(7): "2026-10-01T00:00:00Z",
                s.groom_content_path(7): "a" * 64,
                s.issue_path(7, "steward-stalls-groom"): "3"}
    for path, value in evidence.items():
        Path(path).write_text(value)
    s.clear_issue(7)
    assert {path: Path(path).read_text() for path in evidence} == evidence


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
    _seed_legacy_ci_counter(s, 5, 2)
    Path(s.issue_path(5, "rebase-retries")).write_text("1")
    s.set_resumed_at(5, "t")
    s.set_phase(5, "authored")
    s.bump_infra_deaths(5)
    s.clear_issue(5)
    assert not s.has_session(5)
    assert _legacy_ci_counter(s, 5) == 0
    assert not Path(s.issue_path(5, "rebase-retries")).exists()
    assert s.get_phase(5) == ""           # phase cleared on teardown
    assert s.get_infra_deaths(5) == 0     # review M4/D8: infra-deaths clears too


def test_set_infra_deaths_writes_the_tally_the_reader_sees():
    # The saturate a deterministic death needs (#13): one write, read back by
    # the same accessor the ceiling uses.
    s, _ = _store()
    s.bump_infra_deaths(5)
    s.set_infra_deaths(5, 5)
    assert s.get_infra_deaths(5) == 5
    s.set_infra_deaths(5, -3)  # a negative tally is meaningless, not an error
    assert s.get_infra_deaths(5) == 0


def test_clear_issue_does_not_touch_pr_automerge_marker():
    # review M4: `automerge` is a PR suffix now — clear_issue must not remove
    # issue-N.automerge (the drift this fixes: that file was never written by
    # anything, so removing it was a no-op masquerading as cleanup).
    s, _ = _store()
    s.mark_automerge(100)
    s.clear_issue(9)
    assert s.automerge_enabled(100) is True


def test_cleaners_retain_retired_resolve_files_as_import_evidence():
    # ADR 0015: resolve episodes live in SQLite. Stopped legacy files are the
    # offline importer's evidence, so no cleaner or unblock may delete them.
    s, root = _store()
    retries = os.path.join(root, "issue-5.resolve-retries")
    last_error = os.path.join(root, "issue-5.last-error")
    for path, content in ((retries, "3"), (last_error, "tests failed")):
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(content)
    s.clear_retries(5)
    s.cleanup_pr(100, 5, merged=False)
    s.cleanup_pr(100, 5, merged=True)
    s.clear_issue(5)
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
    s.mark_automerge(100)
    s.clear_pr_state(100)
    assert s.automerge_enabled(100) is False  # review M4: the real marker, now cleared


# ── suffix-coverage (review M4/D8) ──────────────────────────────────────────
def _accessor_suffixes(path_method: str) -> set[str]:
    """Suffix literals Store passes as the 2nd positional arg to `path_method`
    (issue_path/pr_path) anywhere in its own source. clear_issue/clear_pr_state/
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
    # Retired counters still need cleanup in existing state roots.
    assert _accessor_suffixes("issue_path") | {"steward-stalls-triage", "ci-retries", "review-retries", "rebase-retries", "machine-block", "budget-block"} == set(store_module._ISSUE_SUFFIXES)


def test_every_pr_accessor_suffix_is_covered_by_exactly_one_cleaner():
    # Same check for the PR namespace — this is what catches the `automerge`
    # drift: automerge_enabled/mark_automerge use pr_path, so `automerge` must
    # be in _PR_SUFFIXES (it used to sit, unused, in the issue tuple instead).
    # F2 retired the runtime accessor; historical files still need cleanup.
    assert _accessor_suffixes("pr_path") | {"resumed-at"} == set(store_module._PR_SUFFIXES)


# ── merged/closed-unmerged cleanup orchestration (review M4/D8) ────────────
def test_cleanup_pr_merged_clears_full_issue_state_and_automerge_marker():
    s, _ = _store()
    s.save_session(9, "sess-1")
    s.bump_infra_deaths(9)
    s.mark_automerge(100)
    s.cleanup_pr(100, 9, merged=True)
    assert s.get_session(9) == ""
    assert s.get_infra_deaths(9) == 0
    assert s.automerge_enabled(100) is False


def test_cleanup_pr_closed_unmerged_keeps_infra_deaths():
    # review M4/D8 + handover §5.7: fewer retries after a human closed the PR
    # is deliberate — only the resume-blocking markers clear.
    s, _ = _store()
    s.save_session(9, "sess-1")
    s.set_phase(9, "authoring")
    s.set_resumed_at(9, "t1")
    s.bump_infra_deaths(9)
    s.mark_automerge(100)
    s.cleanup_pr(100, 9, merged=False)
    assert s.get_session(9) == ""
    assert s.get_phase(9) == ""
    assert s.get_resumed_at(9) == ""
    # anti-vacuity twin: prove these are KEPT, not "everything got cleared"
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
    s.mark_automerge(100)
    s.bump_infra_deaths(-100)          # the negated-PR breaker key
    s.cleanup_pr(100, None, merged=True)
    assert s.automerge_enabled(100) is False       # PR-side still clears (regression guard)
    assert s.get_infra_deaths(-100) == 0           # NEW: the negated-key namespace clears too


def test_cleanup_pr_issueless_unmerged_keeps_negated_key_infra_deaths():
    # anti-vacuity twin, mirroring the real-issue conservatism (handover
    # §5.7): the closed-unmerged path clears only resume-blocking markers, so
    # infra-deaths under the negated key must survive — proving the fix
    # routes through clear_issue_resume_state, not a blanket clear_issue.
    s, _ = _store()
    s.mark_automerge(100)
    s.bump_infra_deaths(-100)
    s.cleanup_pr(100, None, merged=False)
    assert s.automerge_enabled(100) is True        # PR-side untouched, as before
    assert s.get_infra_deaths(-100) == 1           # kept — same conservatism as a real issue


# ── namespace-level coverage across ALL THREE path methods (review P1R-16 /
# process finding §5) ────────────────────────────────────────────────────────
# The two suffix-only tests above are blind to two distinct gaps: (1) a key
# NAMESPACE distinct from the issue/PR number itself — the negated-PR breaker
# key `-pr` shares every suffix STRING with a real issue, so a suffix-only
# diff can never see that `issue--112.*` needed its own clear call (P1R-16's
# core bug); (2) a path method that carries NO suffix parameter at all —
# `triage_at_path`'s "at" is baked into its f-string, not passed as an
# argument, so it never appears in the suffix regex and was silently absent
# from BOTH `_ISSUE_SUFFIXES`/`_PR_SUFFIXES`. This test derives both the
# "used" and "cleared" sides independently from Store's own source and asserts
# SET EQUALITY — never membership of a couple of examples (§5's lesson: an
# absence bug survives a membership check by construction).
def _triage_at_has_a_cleaner() -> bool:
    """Does Store's OWN source retire `triage_at_path` from within a
    `_remove` call anywhere (a cleaner) — as opposed to only ever reading it
    (`get_triage_at`) or writing it (`set_triage_at`)?"""
    src = inspect.getsource(Store)
    return bool(re.search(r"self\._remove\(self\.triage_at_path\(", src))


def test_every_state_namespace_across_all_three_path_methods_has_a_cleaner():
    used = {("issue_path", s) for s in _accessor_suffixes("issue_path")}
    used |= {("issue_path", "steward-stalls-triage"), ("issue_path", "ci-retries"), ("issue_path", "review-retries"), ("issue_path", "rebase-retries"), ("issue_path", "machine-block"), ("issue_path", "budget-block")}
    used |= {("pr_path", s) for s in _accessor_suffixes("pr_path")}
    used.add(("pr_path", "resumed-at"))
    used.add(("triage_at_path", None))  # single fixed namespace, no suffix param

    cleared = {("issue_path", s) for s in store_module._ISSUE_SUFFIXES}
    cleared |= {("pr_path", s) for s in store_module._PR_SUFFIXES}
    if _triage_at_has_a_cleaner():
        cleared.add(("triage_at_path", None))

    assert used == cleared


def test_namespace_coverage_check_is_not_vacuously_true():
    # anti-vacuity twin: prove the SET EQUALITY check above can actually FAIL.
    # Simulate the pre-fix shape (a namespace nothing clears) and confirm the
    # equality breaks — otherwise the test above could pass "by construction"
    # regardless of whether clear_issue genuinely retires triage_at_path.
    used = {("triage_at_path", None)}
    cleared: set[tuple[str, str | None]] = set()
    assert used != cleared


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


def test_visual_ac_is_a_claim_but_read_receipts_are_not_worktree_markers():
    with tempfile.TemporaryDirectory() as root:
        hd = os.path.join(root, ".harness")
        os.makedirs(hd)
        m = HarnessMarkers(root)
        assert m.visual_ac() is False
        open(os.path.join(hd, "visual-ac"), "w").close()
        with open(os.path.join(hd, "screenshots-read"), "w") as f:
            f.write("tab-title.png\n\nchart.png\n")
        assert m.visual_ac() is True
        assert not hasattr(m, "screenshots_read")


# ── runtime-tagged sessions (the FIXER_RUNTIME flip, 2026-07-27) ──────────────
def test_a_session_is_only_resumable_by_the_runtime_that_created_it(tmp_path):
    """REBASE #541 died in 0.3s: codex was handed the Claude resolve's session
    id (`codex exec resume <claude-uuid>` → "no rollout found"). A runtime
    flip must read as NO session — the leg cold-starts."""
    claude = Store(str(tmp_path), runtime="claude")
    claude.save_session(7, "abc-123")
    assert claude.get_session(7) == "abc-123"
    assert claude.has_session(7)

    codex = Store(str(tmp_path), runtime="codex")
    assert codex.get_session(7) == ""
    assert not codex.has_session(7)

    codex.save_session(7, "thread-9")
    assert codex.get_session(7) == "thread-9"
    assert Store(str(tmp_path), runtime="claude").get_session(7) == ""


def test_legacy_untagged_session_files_belong_to_claude(tmp_path):
    """Files written before tagging existed are bare uuids; claude (the only
    runtime that existed then) still resumes them, codex must not."""
    with open(tmp_path / "issue-9.session", "w") as fh:
        fh.write("966d3799-429e-44e6-a909-38c5ed27ca4f")
    assert Store(str(tmp_path), runtime="claude").get_session(9) == (
        "966d3799-429e-44e6-a909-38c5ed27ca4f"
    )
    assert Store(str(tmp_path), runtime="codex").get_session(9) == ""


def test_runtime_enum_writes_canonical_tags_and_reads_deployed_legacy_tags(
    tmp_path,
):
    store = Store(str(tmp_path), runtime=RuntimeName.CODEX)
    store.save_session(7, "session-new")
    store.save_model(7, "gpt-5.6-terra")

    assert (tmp_path / "issue-7.session").read_text() == "codex:session-new"
    assert (tmp_path / "issue-7.model").read_text() == "codex:gpt-5.6-terra"

    (tmp_path / "issue-7.session").write_text("RuntimeName.CODEX:session-old")
    (tmp_path / "issue-7.model").write_text("RuntimeName.CODEX:gpt-5.6-terra")

    assert store.get_session(7) == "session-old"
    assert store.get_model(7) == "gpt-5.6-terra"


def test_model_marker_roundtrip_and_runtime_isolation():
    s, d = _store()
    assert s.get_model(5) == ""
    s.save_model(5, "gpt-5.6-terra")
    assert s.get_model(5) == "gpt-5.6-terra"
    s.save_model(5, None)  # no model → marker untouched, not clobbered
    assert s.get_model(5) == "gpt-5.6-terra"
    # A model routed for one runtime must not leak into another (#21).
    other = Store(state_dir=d, runtime="codex")
    assert other.get_model(5) == ""


def test_predicted_paths_are_round_tripped_and_malformed_predictions_fail_open():
    s, _ = _store()
    s.save_predicted_paths(5, ["src/action.rs", "./src/help.rs"])
    assert s.get_predicted_paths(5) == frozenset({"src/action.rs", "src/help.rs"})

    s.save_predicted_paths(5, ["src/action.rs", "../outside.rs"])
    assert s.get_predicted_paths(5) == frozenset()


def test_groom_content_digest_round_trips_and_damps_comment_only_updates():
    s, _ = _store()
    for body in ("Steps:\n1. click\n", "Steps:\r\n1. click\r\n"):
        issue = IssueView(9, title="Login crashes", body=body, updated_at="after-comment")
        Path(s.groom_content_path(9)).write_text(grooming_content(issue))
        assert s.get_groom_content(9) == grooming_content(issue)
        assert grooming_candidates(
            WorldState(issues=(issue,)), {9: "before-comment"}, True,
            {9: s.get_groom_content(9)},
        ) == []


def test_retired_review_files_survive_cleanup_as_import_evidence():
    s, _ = _store()
    evidence = {"session": "claude:review-session", "round": "19",
                "last-sha": "a" * 40, "patch-id": "b" * 64,
                "operator-guidance": "Scope: naïve parser — preserve assertions 🚫"}
    for suffix, value in evidence.items():
        with open(s.pr_path(100, suffix), "w", encoding="utf-8") as stream:
            stream.write(value)
    s.clear_pr_state(100)
    s.cleanup_pr(100, 9, merged=True)
    for suffix, value in evidence.items():
        with open(s.pr_path(100, suffix), encoding="utf-8") as stream:
            assert stream.read() == value
    assert not any(hasattr(s, name) for name in (
        "get_pr_session", "save_pr_session", "get_round", "set_round", "clear_pr_review",
        "get_last_sha", "set_last_sha", "get_last_patch_id", "set_last_patch_id",
        "get_operator_guidance", "set_operator_guidance", "clear_operator_guidance",
    ))
