"""Target-wide absence proof must not inspect unrelated retained work."""

from dataclasses import replace
from contextlib import contextmanager
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import (
    SQLiteControlStore, target_has_work_admissions, workflow_episode_key,
)
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import RunId
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey
from tests.adapters.test_workflow_admission import control, request, snapshot
from tests.adapters.test_workflow_control import NOW, WRITER


def _copy_run(db, source, name, binding, *, kind="RESOLVE"):
    original = dict(db.execute("SELECT * FROM runs WHERE run_id=?", (source,)).fetchone())
    values = {**original, "run_id": name, "kind": kind,
              "workflow_episode_key": workflow_episode_key(binding),
              "workflow_binding_json": encode_workflow(binding)}
    columns = tuple(values)
    db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
               tuple(values[column] for column in columns))


@pytest.mark.parametrize("change", ["generation", "episode", "epoch"])
def test_target_absence_includes_all_retained_authorities(control, change):
    assert control.reserve(request("old")).admitted
    binding = snapshot(control).binding
    if change == "generation":
        newer = replace(binding, fence=replace(binding.fence, generation=binding.fence.generation + 1))
    elif change == "episode":
        newer = replace(binding, episode_id="another-episode")
    else:
        newer = replace(binding, authority=replace(binding.authority, epoch=binding.authority.epoch + 1))
    with control.transaction() as db:
        assert target_has_work_admissions(db, newer.fence.key)


@pytest.mark.parametrize("other", [WorkflowKey("o/r", IssueRef(10)),
                                   WorkflowKey("o/r", PullRequestRef(1)),
                                   WorkflowKey("o/r-sibling", IssueRef(1))])
def test_target_scope_does_not_confuse_numeric_prefix_type_or_repository(control, other):
    assert control.reserve(request("one")).admitted
    with control.transaction() as db:
        assert not target_has_work_admissions(db, other)


def test_router_alone_is_not_predecessor_work(control):
    assert control.reserve(request("router", stage="ROUTE")).admitted
    with control.transaction() as db:
        assert not target_has_work_admissions(db, snapshot(control).binding.fence.key)
    assert control.reserve(request("actual-work")).admitted
    with control.transaction() as db:
        assert target_has_work_admissions(db, snapshot(control).binding.fence.key)


def test_absence_query_work_does_not_grow_with_unrelated_corrupt_history(control):
    assert control.reserve(request("template")).admitted
    binding = snapshot(control).binding
    absent = replace(binding.fence.key, target=IssueRef(7))
    with control.transaction() as db:
        for index in range(4000):
            _copy_run(db, "template", f"history-{index}", binding)
        db.execute("UPDATE runs SET workflow_binding_json='unreadable historical evidence'")
        steps = 0

        def bound_work():
            nonlocal steps
            steps += 100
            return int(steps > 1000)

        db.set_progress_handler(bound_work, 100)
        try:
            assert not target_has_work_admissions(db, absent)
        finally:
            db.set_progress_handler(None, 0)


def test_missing_latest_pointer_is_not_fresh_workspace_absence(control):
    assert control.reserve(request("old")).admitted
    with control.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-work-latest'")
    denied = control.reserve(request("replacement"))
    assert not denied.admitted
    assert "latest-admission pointer" in denied.detail
    assert snapshot(control).revision.version == 1


def _publication_rows(db, items):
    db.executemany(
        "INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,kind,target,"
        "repository,executor_identity,payload_json,payload_digest,state,created_at,updated_at) "
        "VALUES (?,?,?,'publish_branch','issue:1','o/r',?,?,'fixture',?,?,?)",
        ((name, name, name, WRITER.owner_id, json.dumps({"run_id": run_id}), state,
          NOW.isoformat(), NOW.isoformat()) for name, run_id, state in items),
    )


@pytest.mark.parametrize("state", ["pending", "executing", "unknown_outcome", "retryable_failure"])
def test_quiescence_rejects_each_unresolved_publication_state(control, state):
    assert control.reserve(request("source")).admitted
    control.release_unlaunched(RunId("source"), "fixture launch refused")
    current = snapshot(control)
    with control.transaction() as db:
        _publication_rows(db, (("publication", "source", state),))
    workflow = SQLiteWorkflowControl(control, WRITER, resolve_ceiling=3)
    with pytest.raises(WorkflowAuthorityError, match="publication effect resolved"):
        with workflow.rearm_authority(current.revision):
            pytest.fail("unresolved publication must not authorize workspace mutation")


def test_quiescence_ignores_unrelated_and_terminal_publication_history_in_bounded_work(control, monkeypatch):
    assert control.reserve(request("source")).admitted
    control.release_unlaunched(RunId("source"), "fixture launch refused")
    current = snapshot(control)
    foreign = replace(current.binding, fence=replace(current.binding.fence,
                                                    key=WorkflowKey("o/r", IssueRef(8))))
    with control.transaction() as db:
        _copy_run(db, "source", "foreign-run", foreign)
        _publication_rows(db, ((f"foreign-{index}", "foreign-run", "unknown_outcome")
                              for index in range(4000)))
        _publication_rows(db, ((f"terminal-{index}", "source", "succeeded" if index % 2 else "permanent_failure")
                              for index in range(4000)))
        for index in range(4000):
            _copy_run(db, "source", f"returned-{index}", foreign)
        db.executemany(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('workflow-return','o/r',?,?,'unreadable unrelated history')",
            ((f"returned-{index}", NOW.isoformat()) for index in range(4000)),
        )
    steps = 0

    def bound_work():
        nonlocal steps
        steps += 100
        return int(steps > 3000)

    original = control.transaction

    @contextmanager
    def bounded_transaction():
        with original() as db:
            db.set_progress_handler(bound_work, 100)
            try:
                yield db
            finally:
                db.set_progress_handler(None, 0)

    monkeypatch.setattr(control, "transaction", bounded_transaction)
    workflow = SQLiteWorkflowControl(control, WRITER, resolve_ceiling=3)
    with workflow.rearm_authority(current.revision) as authorized:
        assert authorized == current


def _downgrade_to_v20(control):
    with control.transaction() as db:
        db.execute("DROP TABLE workflow_activation_evidence")
        db.execute("DROP TABLE workflow_activation_imports")
        db.execute("DROP INDEX runs_workflow_scope")
        db.execute("DROP INDEX lifecycle_publication_runs")
        db.execute("DELETE FROM schema_migrations WHERE version>=21")


def test_v21_upgrade_preserves_old_target_evidence_and_is_idempotent(control):
    assert control.reserve(request("old")).admitted
    binding = snapshot(control).binding
    _downgrade_to_v20(control)
    restarted = SQLiteControlStore(control.path)
    restarted.initialize()
    restarted.initialize()
    with restarted.transaction() as db:
        assert target_has_work_admissions(db, binding.fence.key)
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_interrupted_v21_upgrade_rolls_back_without_losing_admission(control):
    assert control.reserve(request("old")).admitted
    _downgrade_to_v20(control)

    def interrupt(version, point):
        if version == 21 and point == "before_commit":
            raise RuntimeError("interrupted migration")

    with pytest.raises(RuntimeError, match="interrupted"):
        SQLiteControlStore(control.path, migration_observer=interrupt).initialize()
    with sqlite3.connect(control.path) as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 20
        assert db.execute("SELECT run_id FROM runs").fetchone()[0] == "old"
    restarted = SQLiteControlStore(control.path)
    restarted.initialize()
    with restarted.transaction() as db:
        assert target_has_work_admissions(db, snapshot(control).binding.fence.key)
