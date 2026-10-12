"""The public legacy action cannot bypass or replace durable resolve authority."""

from dataclasses import replace

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.domain.workflow_identity import WorkflowKey
from tests.app.test_dashweb_actions import _active_values, _unblock_backend
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


def _files(dirs):
    return {str(path): path.read_bytes() for directory in dirs.values()
            for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("state", ["active_target", "active_family", "missing_epoch", "wrong_path", "missing_database"])
def test_refusal_preserves_counters_guidance_and_remote_holds(monkeypatch, tmp_path, state):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    (dirs["fix"] / "issue-7.resolve-retries").write_text("4")
    (dirs["fix"] / "issue-7.last-error").write_text("preserve failure")
    (dirs["review"] / "pr-12.operator-guidance").write_text("preserve scope")
    control = SQLiteControlStore(backend.control_db_path)
    if state in {"active_target", "active_family", "missing_epoch"}:
        install_workflow(control, resolve_snapshot(99 if state == "active_family" else 7))
    if state == "missing_epoch":
        with control.transaction() as db:
            db.execute("DELETE FROM workflow_authority_epochs")
    elif state == "wrong_path":
        backend.control_db_path = str(tmp_path / "wrong.sqlite")
    elif state == "missing_database":
        from pathlib import Path
        Path(control.path).unlink()
    before = _files(dirs)

    ok, detail = backend.unblock("demo", 7)

    assert not ok
    assert "unblock" in detail
    assert calls == []
    assert _files(dirs) == before


def test_repository_capture_is_reused_through_the_entire_legacy_action(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path, prs=())
    control = SQLiteControlStore(backend.control_db_path)
    other = resolve_snapshot(7)
    other = replace(other, revision=replace(other.revision, fence=replace(
        other.revision.fence, key=WorkflowKey("other/repo", other.revision.fence.key.target),
    )))
    install_workflow(control, other)
    monkeypatch.setattr(backend, "overlay_field", lambda *_: "other/repo")
    observed = []
    issue_reader = backend.gh.issues
    monkeypatch.setattr(backend.gh, "issues", lambda repo: (observed.append(repo), issue_reader(repo))[1])
    factory = backend.effects_factory
    def capture_factory(repo, *args, **kwargs):
        observed.append(repo)
        return factory(repo, *args, **kwargs)
    monkeypatch.setattr(backend, "effects_factory", capture_factory)

    assert backend.unblock("demo", 7) == (True, "ok")
    assert observed and set(observed) == {"o/r"}


def test_configuration_change_cannot_redirect_unblock_files_or_effects(monkeypatch, tmp_path):
    from issue_agent.app import cli

    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, pr_labels=("needs-human-review",), hold_role="fix",
    )
    other_dirs = {role: tmp_path / "replacement" / role for role in dirs}
    for scope in (dirs, other_dirs):
        for directory in scope.values():
            directory.mkdir(parents=True, exist_ok=True)
        (scope["fix"] / "issue-7.resolve-retries").write_text("4")
        (scope["review"] / "pr-12.last-sha").write_text("preserve-head")
        (scope["review"] / "pr-12.operator-guidance").write_text("old scope")
    untouched = _files(other_dirs)
    factory = backend.effects_factory
    captured_dirs = []
    def change_configuration(repo, state_dirs, **kwargs):
        captured_dirs.append(state_dirs)
        _active_values(tmp_path, REPO="other/repo", STATE_DIR=str(other_dirs["fix"]),
                       REVIEW_STATE_DIR=str(other_dirs["review"]),
                       REVIEW_BOT="replacement-bot", REVIEW_MAX_REJECTIONS="0")
        monkeypatch.setattr(cli, "journal_paths", lambda _: [str(path / "journal.jsonl") for path in other_dirs.values()])
        return factory(repo, state_dirs, **kwargs)
    monkeypatch.setattr(backend, "effects_factory", change_configuration)

    assert backend.unblock("demo", 7) == (True, "ok")

    assert _files(other_dirs) == untouched
    assert all(paths == tuple(str(path) for path in dirs.values()) for paths in captured_dirs)
    # Retired resolve files are import evidence, never reset by an unblock.
    assert (dirs["fix"] / "issue-7.resolve-retries").read_text() == "4"
    assert (dirs["review"] / "pr-12.last-sha").read_text() == "preserve-head"
    assert (dirs["review"] / "pr-12.operator-guidance").read_text() == "old scope"
    assert (dirs["fix"] / "events-v2.jsonl").exists()


def test_journal_cache_is_scoped_to_repository_and_captured_path(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    first = backend._operator_journal("demo", "o/r")
    assert backend._operator_journal("demo", "o/r") is first
    other_repo = backend._operator_journal("demo", "other/repo")
    other_path = backend._operator_journal("demo", "o/r", legacy_path=str(tmp_path / "other" / "journal.jsonl"))
    assert first is not None and other_repo is not None and other_path is not None
    assert other_repo is not first and other_path is not first


def test_overlay_cannot_hide_active_snapshot_family_authority(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    install_workflow(SQLiteControlStore(backend.control_db_path), resolve_snapshot(7))
    monkeypatch.setattr(backend, "overlay_field", lambda *_: "future/repo")
    (dirs["fix"] / "issue-7.resolve-retries").write_text("4")
    before = _files(dirs)

    ok, detail = backend.unblock("demo", 7)

    assert not ok and "retired" in detail
    assert calls == [] and _files(dirs) == before


def test_missing_active_snapshot_never_falls_back_to_overlay_or_creates_authority(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    (tmp_path / "config" / ".active.demo.env").unlink()
    (dirs["fix"] / "issue-7.resolve-retries").write_text("4")
    before = _files(dirs)

    ok, detail = backend.unblock("demo", 7)

    assert not ok and "environment file" in detail
    assert calls == [] and _files(dirs) == before


def test_admission_projection_uses_captured_snapshot_not_overlay_or_ui_cache(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path, prs=())
    monkeypatch.setattr(backend, "snapshot", lambda *a, **kw: pytest.fail("legacy admission reused UI snapshot"))
    collect = backend._collector.collect
    captured = []
    def observe(**kwargs):
        captured.append(kwargs)
        return collect(**kwargs)
    monkeypatch.setattr(backend._collector, "collect", observe)
    monkeypatch.setattr(backend, "overlay_field", lambda *_: "future/repo")

    assert backend.unblock("demo", 7) == (True, "ok")

    assert len(captured) == 1
    assert captured[0]["repo"] == "o/r"
    assert captured[0]["target_dir"] == str(tmp_path / "checkout")
    assert captured[0]["journal_paths"] == [str(path / "journal.jsonl") for path in dirs.values()]
    assert captured[0]["control_db_path"] == backend.control_db_path
