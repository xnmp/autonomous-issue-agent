"""Bounded checkpoint receipt absence scans before the first compaction."""

from datetime import timedelta

import pytest

from issue_agent.adapters.review_initial_checkpoint_absence import (
    advance_initial_checkpoint_absence_audit, read_initial_checkpoint_absence_state,
)
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_initial_checkpoint_absence import (
    BlockedInitialCheckpointAbsence, CompletedInitialCheckpointAbsence,
    ScanningInitialCheckpointAbsence,
)
from tests.adapters.test_review_execution_persistence import active


def _binding(snapshot):
    return snapshot.execution.checkpoint.state.binding


def test_initial_checkpoint_absence_scans_unrelated_global_rows_in_bounded_steps(active):
    store, snapshot = active
    binding = _binding(snapshot)
    now = store.now()
    with store.transaction() as db:
        for index in range(65):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('unrelated-checkpoint-test','o/r',?,?,?)",
                (f"other:{index}", now.isoformat(), "{}"),
            )
    with store.transaction() as db:
        first = advance_initial_checkpoint_absence_audit(db, binding, now=now)
    assert isinstance(first, ScanningInitialCheckpointAbsence)
    assert first.after_rowid is not None
    for _ in range(64):
        with store.transaction() as db:
            state = advance_initial_checkpoint_absence_audit(db, binding, now=now)
        if isinstance(state, CompletedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("bounded checkpoint absence scan did not complete")
    assert state.source_version == first.source_version
    with store.transaction() as db:
        assert read_initial_checkpoint_absence_state(db, binding) == state
    assert advance_at(store, binding, now + timedelta(seconds=1)) == state


def advance_at(store, binding, now):
    with store.transaction() as db:
        return advance_initial_checkpoint_absence_audit(db, binding, now=now)


def test_initial_checkpoint_absence_blocks_corrupt_receipt_and_recovers_after_repair(active):
    store, snapshot = active
    binding = _binding(snapshot)
    now = store.now()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-checkpoint','o/r','corrupt:checkpoint',?, '{}')",
            (now.isoformat(),),
        )
    for _ in range(64):
        state = advance_at(store, binding, now)
        if isinstance(state, BlockedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("corrupt checkpoint receipt was not found")
    assert "unreadable" in state.reason
    assert advance_at(store, binding, now + timedelta(seconds=1)) == state
    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='review-execution-checkpoint' "
            "AND key='corrupt:checkpoint'",
        )
    for _ in range(64):
        repaired = advance_at(store, binding, now + timedelta(seconds=2))
        if isinstance(repaired, CompletedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("repaired checkpoint receipt remained blocked")
    assert repaired.source_version > state.prior.source_version


def test_initial_checkpoint_absence_rejects_blob_namespace_alias(active):
    store, snapshot = active
    binding = _binding(snapshot)
    now = store.now()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (CAST(? AS BLOB),'o/r','alias:checkpoint',?, '{}')",
            ("review-execution-checkpoint", now.isoformat()),
        )
    for _ in range(64):
        state = advance_at(store, binding, now)
        if isinstance(state, BlockedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("checkpoint namespace alias was not found")
    assert "BLOB alias" in state.reason


def test_initial_checkpoint_absence_blocks_orphan_application_source(active):
    store, snapshot = active
    binding = _binding(snapshot)
    key = snapshot.revision.fence.key
    now = store.now()
    for _ in range(64):
        completed = advance_at(store, binding, now)
        if isinstance(completed, CompletedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("initial checkpoint absence did not complete")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-checkpoint-application','o/r','orphan:application',?, '{}')",
            (now.isoformat(),),
        )
        assert read_initial_checkpoint_absence_state(db, binding) == completed
    for _ in range(64):
        blocked = advance_at(store, binding, now + timedelta(seconds=1))
        if isinstance(blocked, BlockedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("orphan checkpoint application was not found")
    assert "unreadable" in blocked.reason
    assert blocked.prior.source_version > completed.source_version


def test_initial_checkpoint_absence_rejects_aliased_persisted_cursor(active):
    store, snapshot = active
    binding = _binding(snapshot)
    now = store.now()
    advance_at(store, binding, now)
    with store.transaction() as db:
        row = db.execute(
            "SELECT scope,key FROM level_state "
            "WHERE namespace='review-initial-checkpoint-absence-v1'",
        ).fetchone()
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (CAST(? AS BLOB),? ,?,'{}',?)",
            ("review-initial-checkpoint-absence-v1", row["scope"], row["key"], now.isoformat()),
        )
        with pytest.raises(WorkflowAuthorityError, match="aliased or unreadable"):
            read_initial_checkpoint_absence_state(db, binding)
