"""Durable lifecycle membership scans across bounded SQL transactions."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_lifecycle_archive import (
    read_review_lifecycle_effect_page, read_review_lifecycle_source_page,
)
from issue_agent.adapters.review_lifecycle_archive_audit import (
    advance_review_lifecycle_archive_audit, read_review_lifecycle_archive_state,
)
from issue_agent.adapters.review_archive_source_versions import read_review_lifecycle_source_version
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_lifecycle_archive import (
    ObservedReviewLifecycleEffectPage, ObservedReviewLifecycleSourcePage,
)
from issue_agent.domain.review_lifecycle_archive_audit import (
    BlockedReviewLifecycleArchiveAudit, CompletedReviewLifecycleArchiveAudit,
    ScanningReviewLifecycleCommands, ScanningReviewLifecycleEffects,
    advance_review_lifecycle_command_scan, advance_review_lifecycle_effect_scan,
    begin_review_lifecycle_archive_audit,
)
from tests.adapters.test_review_archive_source_versions import (
    _insert_lifecycle_command, _insert_lifecycle_effect,
)


NOW = datetime(2026, 9, 29, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    result = SQLiteControlStore(tmp_path / "control.db")
    result.initialize()
    return result


def _review_command(db, command_id):
    _insert_lifecycle_command(db, command_id)
    db.execute(
        "UPDATE lifecycle_commands SET repository='o/r' WHERE command_id=?",
        (command_id,),
    )


def _step(store, now=NOW):
    with store.transaction() as db:
        return advance_review_lifecycle_archive_audit(db, "o/r", now=now)


def test_lifecycle_audit_completes_empty_command_and_effect_streams(store):
    first = _step(store)
    assert isinstance(first, ScanningReviewLifecycleEffects)
    assert first.commands.count == 0
    second = _step(SQLiteControlStore(store.path), NOW + timedelta(seconds=1))
    assert isinstance(second, CompletedReviewLifecycleArchiveAudit)
    assert second.commands.count == second.effects.count == 0
    with store.transaction() as db:
        assert read_review_lifecycle_archive_state(db, "o/r") == second
    assert _step(store, NOW + timedelta(seconds=2)) == second
    due = _step(store, second.next_due_at)
    assert isinstance(due, ScanningReviewLifecycleEffects)


def test_lifecycle_audit_reconciles_selected_receipts_after_bounded_global_pages(store):
    with store.transaction() as db:
        for index in range(65):
            command_id = f"unrelated:{index}"
            _insert_lifecycle_command(db, command_id, kind="other")
            _insert_lifecycle_effect(db, command_id)
        _review_command(db, "review:selected")
        _insert_lifecycle_effect(db, "review:selected")
    first = _step(store)
    assert isinstance(first, ScanningReviewLifecycleCommands)
    assert first.commands.count == 0
    assert first.after_rowid is not None
    second = _step(store, NOW + timedelta(seconds=1))
    assert isinstance(second, ScanningReviewLifecycleEffects)
    assert second.commands.count == second.joined_effects.count == 1
    third = _step(store, NOW + timedelta(seconds=2))
    assert isinstance(third, ScanningReviewLifecycleEffects)
    assert third.effects.count == 0
    completed = _step(SQLiteControlStore(store.path), NOW + timedelta(seconds=3))
    assert isinstance(completed, CompletedReviewLifecycleArchiveAudit)
    assert completed.commands.count == completed.joined_effects.count == completed.effects.count == 1
    assert completed.joined_effects == completed.effect_links
    assert completed.source_version > 0


def test_lifecycle_audit_restarts_when_effect_changes_between_phases(store):
    with store.transaction() as db:
        _review_command(db, "review:pending")
    first = _step(store)
    assert isinstance(first, ScanningReviewLifecycleEffects)
    assert first.joined_effects.count == 0
    with store.transaction() as db:
        _insert_lifecycle_effect(db, "review:pending")
    restarted = _step(store, NOW + timedelta(seconds=1))
    assert isinstance(restarted, ScanningReviewLifecycleEffects)
    assert restarted.source_version > first.source_version
    assert restarted.joined_effects.count == 1
    completed = _step(store, NOW + timedelta(seconds=2))
    assert isinstance(completed, CompletedReviewLifecycleArchiveAudit)
    assert completed.effects.count == 1


def test_lifecycle_audit_rechecks_mutable_sources_after_completion(store):
    with store.transaction() as db:
        _review_command(db, "review:settled")
        _insert_lifecycle_effect(db, "review:settled")
    _step(store)
    completed = _step(store, NOW + timedelta(seconds=1))
    assert isinstance(completed, CompletedReviewLifecycleArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE effect_receipts SET remote_revision='new' "
            "WHERE command_id='review:settled'"
        )
    restarted = _step(store, NOW + timedelta(seconds=2))
    assert isinstance(restarted, ScanningReviewLifecycleEffects)
    assert restarted.source_version > completed.source_version
    after = _step(store, NOW + timedelta(seconds=3))
    assert isinstance(after, CompletedReviewLifecycleArchiveAudit)
    assert after.source_version == restarted.source_version
    assert after.commands != completed.commands


def test_lifecycle_audit_persists_orphan_effect_discrepancy(store):
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        _insert_lifecycle_effect(db, "orphan:source")
        db.commit()
    _step(store)
    blocked = _step(store, NOW + timedelta(seconds=1))
    assert isinstance(blocked, BlockedReviewLifecycleArchiveAudit)
    assert "no matching command" in blocked.reason
    with store.transaction() as db:
        assert read_review_lifecycle_archive_state(db, "o/r") == blocked
    assert _step(store, NOW + timedelta(seconds=2)) == blocked

    with store.transaction() as db:
        _review_command(db, "orphan:source")
    restarted = _step(store, NOW + timedelta(seconds=3))
    assert isinstance(restarted, ScanningReviewLifecycleEffects)
    assert restarted.source_version > blocked.prior.source_version
    completed = _step(store, NOW + timedelta(seconds=4))
    assert isinstance(completed, CompletedReviewLifecycleArchiveAudit)
    assert completed.commands.count == completed.effects.count == 1


def test_lifecycle_audit_blocks_reconciled_effect_set_disagreement(store):
    with store.transaction() as db:
        _review_command(db, "review:source")
        _insert_lifecycle_effect(db, "review:source")
        version = read_review_lifecycle_source_version(db)
        command_page = read_review_lifecycle_source_page(db, "o/r")
        effect_page = read_review_lifecycle_effect_page(db, "o/r")
    assert isinstance(command_page, ObservedReviewLifecycleSourcePage)
    assert isinstance(effect_page, ObservedReviewLifecycleEffectPage)
    scan = begin_review_lifecycle_archive_audit("o/r", version, NOW, 3600)
    effect_scan = advance_review_lifecycle_command_scan(scan, command_page, NOW)
    assert isinstance(effect_scan, ScanningReviewLifecycleEffects)
    mismatched = advance_review_lifecycle_effect_scan(
        effect_scan, replace(effect_page, sources=(), source_bytes=0), NOW, 3600,
    )
    assert isinstance(mismatched, BlockedReviewLifecycleArchiveAudit)
    assert "membership disagrees" in mismatched.reason


def test_lifecycle_audit_refuses_aliased_persisted_cursor(store):
    _step(store)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (CAST(? AS BLOB),'o/r','review-submissions','{}',?)",
            ("review-lifecycle-archive-scan-v1", NOW.isoformat()),
        )
        with pytest.raises(WorkflowAuthorityError, match="aliased or unreadable"):
            read_review_lifecycle_archive_state(db, "o/r")
        with pytest.raises(WorkflowAuthorityError, match="aliased or unreadable"):
            advance_review_lifecycle_archive_audit(db, "o/r", now=NOW)


def test_lifecycle_audit_requires_transaction_and_aware_time(store):
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            advance_review_lifecycle_archive_audit(db, "o/r", now=NOW)
    with store.transaction() as db:
        with pytest.raises(ValueError, match="aware time"):
            advance_review_lifecycle_archive_audit(db, "o/r", now=NOW.replace(tzinfo=None))
