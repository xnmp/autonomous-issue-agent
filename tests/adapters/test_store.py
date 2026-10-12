"""Store adapter — path construction + read/write/clear round-trips + markers."""

import os
import tempfile
from pathlib import Path

from issue_agent.adapters.store import Store

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


# review M3/T2: the old `test_write_is_atomic_and_leaves_no_tmp_file` above
# passed against a truncate-and-write implementation (it only ever checked
# that one hardcoded, non-pid-suffixed ".tmp" path was absent — a truncating
# `_write` never creates that path either, so the mutation sailed through).
# These replace it with tests that actually exercise the crash window and the
# collision the pid-suffixed tmp name exists to avoid.
def test_write_leaves_no_tmp_file_after_success():
    s, root = _store()
    s.save_predicted_paths(5, ["src/a.py"])

    assert s.get_predicted_paths(5) == frozenset({"src/a.py"})
    assert os.path.exists(os.path.join(root, "issue-5.predicted-paths"))
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
    s.save_predicted_paths(5, ["src/a.py"])

    def _boom(_src, _dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", _boom)
    try:
        s.save_predicted_paths(5, ["src/b.py"])
    except OSError:
        pass
    assert s.get_predicted_paths(5) == frozenset({"src/a.py"})

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
    s.save_predicted_paths(5, ["src/a.py"])
    assert calls, "Store._write must fsync the tmp file before os.replace"
    assert s.get_predicted_paths(5) == frozenset({"src/a.py"})

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
    s.save_predicted_paths(5, ["src/a.py"])
    assert str(os.getpid()) in seen["src"]
    assert s.get_predicted_paths(5) == frozenset({"src/a.py"})

def test_clear_retries_hands_back_only_the_per_attempt_budgets():
    """An operator unblock resets the retry ceilings a wedge consumed — and
    deliberately not the two that bound unwinnable work."""
    s, _ = _store()
    _seed_legacy_ci_counter(s, 5, 3)
    Path(s.issue_path(5, "review-retries")).write_text("2026-01-01T00:00:00Z 2")
    Path(s.issue_path(5, "rebase-retries")).write_text("1")

    assert not hasattr(s, "clear_retries")

    assert _legacy_ci_counter(s, 5) == 3
    assert Path(s.issue_path(5, "review-retries")).read_text() == "2026-01-01T00:00:00Z 2"
    assert Path(s.issue_path(5, "rebase-retries")).read_text() == "1"

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


def test_predicted_paths_are_round_tripped_and_malformed_predictions_fail_open():
    s, _ = _store()
    s.save_predicted_paths(5, ["src/action.rs", "./src/help.rs"])
    assert s.get_predicted_paths(5) == frozenset({"src/action.rs", "src/help.rs"})

    s.save_predicted_paths(5, ["src/action.rs", "../outside.rs"])
    assert s.get_predicted_paths(5) == frozenset()


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
