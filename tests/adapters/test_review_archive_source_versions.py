"""Durable mutation witnesses for paged review archive sources."""

from contextlib import closing
import sqlite3

import pytest

from issue_agent.adapters.review_archive_source_versions import (
    MAX_ARCHIVE_SOURCE_VERSION, read_review_archive_source_versions,
)
from issue_agent.adapters.review_round_receipt_sweep import ROUND_RECEIPT_NAMESPACES
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain import review_rounds as rounds
from tests.adapters.test_control_store import _historical_fixture
from tests.adapters.test_review_execution_persistence import active, round_facts


def _insert_round(db, snapshot, sequence):
    facts, _ = round_facts(snapshot, sequence)
    identity = facts.plan.round
    binding = identity.binding
    db.execute(
        "INSERT INTO review_execution_rounds(round_id,repository,target_kind,target_number,execution_epoch,"
        "execution_episode,generation,sequence,claim_occurrence_id,plan_json,planning_json) "
        "VALUES (?,?,'pr',?,?,?,?,?,?,?,?)",
        (identity.round_id, binding.rejection.fence.key.repository,
         binding.rejection.fence.key.target.number, binding.execution_epoch,
         binding.execution_episode, binding.rejection.fence.generation,
         identity.sequence, "claim:fixture", encode_workflow(facts.plan),
         encode_workflow(rounds.ReviewPlanningOpen())),
    )
    return identity.round_id


def _insert_receipt(db, namespace, key):
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?, 'o/r', ?, '2026-09-29T00:00:00+00:00', '{}')",
        (namespace, key),
    )


def _clone_workflow_repository(db, repository):
    db.execute(
        "INSERT INTO workflows(repository,target_kind,target_number,generation,version,episode_json) "
        "SELECT ?,target_kind,target_number,generation,version,episode_json FROM workflows "
        "WHERE repository='o/r' AND target_kind='pr'",
        (repository,),
    )


def test_review_archive_mutation_witnesses_follow_round_rows_and_receipts(active):
    store, snapshot = active
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, "o/r")
        assert (before.round_rows, before.all_round_rows, before.round_receipts) == (0, 0, 0)
        round_id = _insert_round(db, snapshot, 1)
        inserted = read_review_archive_source_versions(db, "o/r")
        assert (inserted.round_rows, inserted.all_round_rows, inserted.round_receipts) == (1, 1, 0)
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
            (round_id,),
        )
        updated = read_review_archive_source_versions(db, "o/r")
        assert (updated.round_rows, updated.all_round_rows, updated.round_receipts) == (2, 2, 0)
        _insert_receipt(db, "review-round-stage", "stage:1")
        with_receipt = read_review_archive_source_versions(db, "o/r")
        assert (with_receipt.round_rows, with_receipt.all_round_rows, with_receipt.round_receipts) == (2, 2, 1)
        db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (round_id,))
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stage' AND key='stage:1'")
        deleted = read_review_archive_source_versions(db, "o/r")
        assert (deleted.round_rows, deleted.all_round_rows, deleted.round_receipts) == (3, 3, 2)


def test_review_receipt_witness_covers_every_selected_namespace_and_rekeys(active):
    store, _ = active
    with store.transaction() as db:
        for index, namespace in enumerate(ROUND_RECEIPT_NAMESPACES):
            _insert_receipt(db, namespace, f"source:{index}")
        all_inserted = read_review_archive_source_versions(db, "o/r")
        assert all_inserted.round_receipts == len(ROUND_RECEIPT_NAMESPACES)
        _insert_receipt(db, "other-namespace", "unrelated")
        assert read_review_archive_source_versions(db, "o/r") == all_inserted
        db.execute(
            "UPDATE dedupe_keys SET namespace=CAST(namespace AS BLOB) "
            "WHERE namespace='review-round-stage'",
        )
        alias = read_review_archive_source_versions(db, "o/r")
        assert alias.round_receipts == all_inserted.round_receipts + 1
        db.execute(
            "UPDATE dedupe_keys SET namespace='other-namespace' "
            "WHERE namespace=CAST('review-round-stage' AS BLOB)",
        )
        assert read_review_archive_source_versions(db, "o/r").round_receipts == alias.round_receipts + 1
        db.execute("UPDATE dedupe_keys SET namespace='review-round-stage' WHERE key='unrelated'")
        assert read_review_archive_source_versions(db, "o/r").round_receipts == alias.round_receipts + 2


def test_review_archive_witness_rekey_and_rollback_are_atomic(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        baseline = read_review_archive_source_versions(db, "o/r")
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        db.execute(
            "UPDATE review_execution_rounds SET repository=CAST(repository AS BLOB) WHERE round_id=?",
            (round_id,),
        )
        assert read_review_archive_source_versions(db, "o/r").round_rows == baseline.round_rows + 1
        db.rollback()
    with store.transaction() as db:
        assert read_review_archive_source_versions(db, "o/r") == baseline
        assert db.execute(
            "SELECT typeof(repository) FROM review_execution_rounds WHERE round_id=?",
            (round_id,),
        ).fetchone()[0] == "text"


def test_review_archive_witness_rekey_advances_both_repository_scopes(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        _clone_workflow_repository(db, "other/repo")
        before_a = read_review_archive_source_versions(db, "o/r")
        before_b = read_review_archive_source_versions(db, "other/repo")
        assert before_b.round_rows == 0
        db.execute(
            "UPDATE review_execution_rounds SET repository='other/repo' WHERE round_id=?",
            (round_id,),
        )
        assert read_review_archive_source_versions(db, "o/r").round_rows == before_a.round_rows + 1
        assert read_review_archive_source_versions(db, "other/repo").round_rows == before_b.round_rows + 1
        assert read_review_archive_source_versions(db, "o/r").all_round_rows == before_a.all_round_rows + 1
        assert read_review_archive_source_versions(db, "other/repo").all_round_rows == before_b.all_round_rows + 1


def test_global_round_witness_covers_foreign_rows_without_changing_local_version(active):
    store, snapshot = active
    with store.transaction() as db:
        _clone_workflow_repository(db, "other/repo")
        before = read_review_archive_source_versions(db, "o/r")
        round_id = _insert_round(db, snapshot, 1)
        db.execute(
            "UPDATE review_execution_rounds SET repository='other/repo' WHERE round_id=?",
            (round_id,),
        )
        moved = read_review_archive_source_versions(db, "o/r")
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
            (round_id,),
        )
        foreign_updated = read_review_archive_source_versions(db, "o/r")
        assert foreign_updated.round_rows == moved.round_rows
        assert foreign_updated.round_receipts == before.round_receipts
        assert foreign_updated.all_round_rows == moved.all_round_rows + 1


def test_review_archive_witness_rejects_implicit_round_replacement(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        other_round = _insert_round(db, snapshot, 2)
        _clone_workflow_repository(db, "other/repo")
        before_a = read_review_archive_source_versions(db, "o/r")
        before_b = read_review_archive_source_versions(db, "other/repo")
        with pytest.raises(sqlite3.IntegrityError, match="replacement would hide"):
            db.execute(
                "INSERT OR REPLACE INTO review_execution_rounds("
                "round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,sequence,claim_occurrence_id,plan_json,planning_json) "
                "SELECT round_id,'other/repo',target_kind,target_number,execution_epoch,"
                "execution_episode,generation,sequence,claim_occurrence_id,plan_json,planning_json "
                "FROM review_execution_rounds WHERE round_id=?",
                (round_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="replacement would hide"):
            db.execute(
                "UPDATE OR REPLACE review_execution_rounds SET sequence=1 WHERE round_id=?",
                (other_round,),
            )
        assert read_review_archive_source_versions(db, "o/r") == before_a
        assert read_review_archive_source_versions(db, "other/repo") == before_b
        assert {row["round_id"] for row in db.execute("SELECT round_id FROM review_execution_rounds")} == {
            round_id, other_round,
        }


def test_review_workflow_alias_cannot_reset_a_missing_round_witness(active):
    store, _ = active
    with store.transaction() as db:
        _clone_workflow_repository(db, "other/repo")
        db.execute(
            "UPDATE workflows SET repository=CAST(repository AS BLOB) "
            "WHERE repository='other/repo'",
        )
        db.execute(
            "DELETE FROM review_archive_source_versions "
            "WHERE source='round_rows' AND scope=CAST('other/repo' AS BLOB)",
        )
        with pytest.raises(WorkflowAuthorityError, match="round row mutation witness"):
            read_review_archive_source_versions(db, "other/repo")
        with pytest.raises(sqlite3.IntegrityError, match="lost its round source witness"):
            db.execute(
                "INSERT INTO workflows(repository,target_kind,target_number,generation,version,episode_json) "
                "SELECT 'other/repo',target_kind,target_number+1,generation,version,episode_json "
                "FROM workflows WHERE repository=CAST('other/repo' AS BLOB)",
            )
        assert db.execute(
            "SELECT 1 FROM review_archive_source_versions "
            "WHERE source='round_rows' AND scope=CAST('other/repo' AS BLOB)",
        ).fetchone() is None


def test_review_archive_witness_survives_v26_migration_with_existing_rows(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        _insert_receipt(db, "review-round-stage", "stage:1")
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 26)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        baseline = read_review_archive_source_versions(db, "o/r")
        assert (baseline.round_rows, baseline.round_receipts) == (0, 0)
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
            (round_id,),
        )
        db.execute("UPDATE dedupe_keys SET payload_json='[]' WHERE key='stage:1'")
        assert read_review_archive_source_versions(db, "o/r").round_rows == 1
        assert read_review_archive_source_versions(db, "o/r").round_receipts == 1


def test_global_round_witness_survives_v27_migration_with_existing_rows(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 27)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        baseline = read_review_archive_source_versions(db, "o/r")
        assert baseline.all_round_rows == 0
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
            (round_id,),
        )
        assert read_review_archive_source_versions(db, "o/r").all_round_rows == 1


def test_review_archive_witness_blocks_overflow_and_missing_state(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        db.execute(
            "UPDATE review_archive_source_versions SET version=? "
            "WHERE source='round_rows' AND scope=CAST('o/r' AS BLOB)",
            (MAX_ARCHIVE_SOURCE_VERSION,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
                (round_id,),
            )
        assert read_review_archive_source_versions(db, "o/r").round_rows == MAX_ARCHIVE_SOURCE_VERSION
        db.execute(
            "DELETE FROM review_archive_source_versions "
            "WHERE source='round_rows' AND scope=CAST('o/r' AS BLOB)",
        )
        with pytest.raises(WorkflowAuthorityError, match="round row mutation witness"):
            read_review_archive_source_versions(db, "o/r")
        with pytest.raises(sqlite3.IntegrityError, match="round source witness"):
            db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (round_id,))
        assert db.execute(
            "SELECT 1 FROM review_execution_rounds WHERE round_id=?", (round_id,),
        ).fetchone() is not None
        db.execute(
            "DELETE FROM review_archive_source_versions WHERE source='round_receipts'",
        )
        with pytest.raises(WorkflowAuthorityError, match="round receipt mutation witness"):
            read_review_archive_source_versions(db, "o/r")
        with pytest.raises(sqlite3.IntegrityError, match="round receipt source witness"):
            _insert_receipt(db, "review-round-stage", "stage:1")


def test_global_round_witness_blocks_missing_state_and_overflow(active):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        db.execute(
            "UPDATE review_archive_source_versions SET version=? "
            "WHERE source='all_round_rows' AND scope=X''",
            (MAX_ARCHIVE_SOURCE_VERSION,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE review_execution_rounds SET planning_json=planning_json WHERE round_id=?",
                (round_id,),
            )
        db.execute(
            "DELETE FROM review_archive_source_versions WHERE source='all_round_rows' AND scope=X''",
        )
        with pytest.raises(WorkflowAuthorityError, match="global round row mutation witness"):
            read_review_archive_source_versions(db, "o/r")
        with pytest.raises(sqlite3.IntegrityError, match="global round source witness"):
            db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (round_id,))


def test_review_archive_witness_requires_transaction(active):
    store, _ = active
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_review_archive_source_versions(db, "o/r")
