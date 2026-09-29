"""Durable mutation witnesses for paged review archive sources."""

from contextlib import closing
import hashlib
import sqlite3

import pytest

from issue_agent.adapters.review_archive_source_versions import (
    MAX_ARCHIVE_SOURCE_VERSION, read_review_archive_source_versions,
    read_review_lifecycle_source_version,
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


def _insert_lifecycle_command(
    db, command_id="fixture:command", kind="submit_review", state="succeeded",
):
    db.execute(
        "INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,kind,target,"
        "payload_json,payload_digest,state,created_at,updated_at) "
        "VALUES (?,?,?,?,'pr:1','{}',?,?,"
        "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00')",
        (command_id, command_id, command_id, kind,
         hashlib.sha256(b"{}").hexdigest(), state),
    )


def _insert_lifecycle_effect(db, command_id="fixture:command"):
    db.execute(
        "INSERT INTO effect_receipts(command_id,executor_identity,started_at,completed_at,"
        "status,receipt_digest) VALUES (?,'fixture',"
        "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00','succeeded',?)",
        (command_id, "b" * 64),
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


def test_generation_witness_covers_all_sources_and_foreign_receipts(active):
    store, _ = active
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, "o/r")
        for namespace, key in (
            ("review-execution-generation", "foreign:receipt"),
            ("workflow-request", "accepted:request"),
            ("workflow-transition", "accepted:transition"),
        ):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,?,?,'2026-09-29T00:00:00+00:00','{}')",
                (namespace, "other/repo" if namespace == "review-execution-generation" else "o/r", key),
            )
        inserted = read_review_archive_source_versions(db, "o/r")
        assert inserted.generation_sources == before.generation_sources + 3
        assert (inserted.round_rows, inserted.all_round_rows, inserted.round_receipts) == (
            before.round_rows, before.all_round_rows, before.round_receipts,
        )
        db.execute(
            "UPDATE dedupe_keys SET namespace=CAST(namespace AS BLOB) "
            "WHERE namespace='workflow-request' AND key='accepted:request'",
        )
        aliased = read_review_archive_source_versions(db, "o/r")
        assert aliased.generation_sources == inserted.generation_sources + 1
        db.execute(
            "UPDATE dedupe_keys SET namespace='unrelated' WHERE key='accepted:transition'",
        )
        db.execute(
            "DELETE FROM dedupe_keys WHERE key='foreign:receipt'",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == (
            aliased.generation_sources + 2
        )
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('other','o/r','unrelated:key','2026-09-29T00:00:00+00:00','{}')",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == (
            aliased.generation_sources + 2
        )
        db.execute(
            "UPDATE dedupe_keys SET namespace='workflow-request' WHERE key='unrelated:key'",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == (
            aliased.generation_sources + 3
        )
        db.execute(
            "INSERT OR REPLACE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('workflow-request','o/r','unrelated:key',"
            "'2026-09-29T00:00:00+00:00','[]')",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == (
            aliased.generation_sources + 4
        )


def test_generation_witness_survives_v28_migration_with_existing_sources(active):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "workflow-transition", "accepted:transition")
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 28)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        assert read_review_archive_source_versions(db, "o/r").generation_sources == 0
        db.execute(
            "UPDATE dedupe_keys SET payload_json='[]' "
            "WHERE namespace='workflow-transition' AND key='accepted:transition'",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == 1


def test_generation_witness_rollback_missing_state_and_overflow(active):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "workflow-request", "accepted:request")
        baseline = read_review_archive_source_versions(db, "o/r")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        db.execute(
            "UPDATE dedupe_keys SET payload_json='[]' WHERE key='accepted:request'",
        )
        assert read_review_archive_source_versions(db, "o/r").generation_sources == (
            baseline.generation_sources + 1
        )
        db.rollback()
    with store.transaction() as db:
        assert read_review_archive_source_versions(db, "o/r") == baseline
        db.execute(
            "UPDATE review_archive_source_versions SET version=? "
            "WHERE source='generation_sources' AND scope=X''",
            (MAX_ARCHIVE_SOURCE_VERSION,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE dedupe_keys SET payload_json='[]' WHERE key='accepted:request'",
            )
        db.execute(
            "DELETE FROM review_archive_source_versions "
            "WHERE source='generation_sources' AND scope=X''",
        )
        with pytest.raises(WorkflowAuthorityError, match="generation source mutation witness"):
            read_review_archive_source_versions(db, "o/r")
        with pytest.raises(sqlite3.IntegrityError, match="generation source witness"):
            db.execute(
                "DELETE FROM dedupe_keys WHERE key='accepted:request'",
            )


@pytest.mark.parametrize("namespace", ("review-round-stage", "review-execution-generation"))
@pytest.mark.parametrize("operation", ("insert", "update"))
def test_archive_witness_rejects_implicit_dedupe_source_replacement(active, namespace, operation):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, namespace, "source:key")
        source_rowid = db.execute(
            "SELECT rowid FROM dedupe_keys WHERE namespace=? AND key='source:key'",
            (namespace,),
        ).fetchone()[0]
        before = read_review_archive_source_versions(db, "o/r")
        if operation == "update":
            _insert_receipt(db, "other-namespace", "unrelated:key")
            mutation = (
                "UPDATE OR REPLACE dedupe_keys SET rowid=? "
                "WHERE namespace='other-namespace' AND key='unrelated:key'"
            )
        else:
            mutation = (
                "INSERT OR REPLACE INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,'other-namespace','o/r','unrelated:key',"
                "'2026-09-29T00:00:00+00:00','{}')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="replacement would hide"):
            db.execute(mutation, (source_rowid,))
        assert read_review_archive_source_versions(db, "o/r") == before
        assert db.execute(
            "SELECT 1 FROM dedupe_keys WHERE rowid=? AND namespace=? AND key='source:key'",
            (source_rowid, namespace),
        ).fetchone() is not None


@pytest.mark.parametrize("namespace,version_field", (
    ("review-round-stage", "round_receipts"),
    ("review-execution-generation", "generation_sources"),
))
def test_archive_witness_handles_automatic_insert_with_negative_source_rowid(
    active, namespace, version_field,
):
    store, _ = active
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
            "VALUES (-1,?,'o/r','negative:source','2026-09-29T00:00:00+00:00','{}')",
            (namespace,),
        )
        before = read_review_archive_source_versions(db, "o/r")
        _insert_receipt(db, "other-namespace", "unrelated:auto")
        after_auto = read_review_archive_source_versions(db, "o/r")
        assert getattr(after_auto, version_field) == getattr(before, version_field) + 1
        assert db.execute(
            "SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND namespace=?",
            (namespace,),
        ).fetchone() is not None
        db.execute(
            "INSERT OR REPLACE INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
            "VALUES (-1,'other-namespace','o/r','unrelated:replace',"
            "'2026-09-29T00:00:00+00:00','{}')",
        )
        after_replace = read_review_archive_source_versions(db, "o/r")
        assert getattr(after_replace, version_field) == getattr(after_auto, version_field) + 1
        assert db.execute(
            "SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND namespace=?",
            (namespace,),
        ).fetchone() is None


def test_checkpoint_witness_covers_origin_checkpoint_and_application_sources(active):
    store, _ = active
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, "o/r")
        for namespace, key in (
            ("review-execution-origin", "origin:fixture"),
            ("review-execution-checkpoint", "checkpoint:fixture"),
            ("review-checkpoint-application", "application:fixture"),
        ):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,?,?,'2026-09-29T00:00:00+00:00','{}')",
                (namespace, "other/repo", key),
            )
        inserted = read_review_archive_source_versions(db, "o/r")
        assert inserted.checkpoint_sources == before.checkpoint_sources + 3
        assert inserted.workflow_rows == before.workflow_rows
        db.execute(
            "UPDATE dedupe_keys SET namespace=CAST(namespace AS BLOB) "
            "WHERE namespace='review-execution-origin' AND key='origin:fixture'",
        )
        aliased = read_review_archive_source_versions(db, "o/r")
        assert aliased.checkpoint_sources == inserted.checkpoint_sources + 1
        db.execute(
            "UPDATE dedupe_keys SET namespace='unrelated' WHERE key='checkpoint:fixture'",
        )
        db.execute("DELETE FROM dedupe_keys WHERE key='application:fixture'")
        assert read_review_archive_source_versions(db, "o/r").checkpoint_sources == (
            aliased.checkpoint_sources + 2
        )
        _insert_receipt(db, "unrelated", "other:key")
        assert read_review_archive_source_versions(db, "o/r").checkpoint_sources == (
            aliased.checkpoint_sources + 2
        )


def test_workflow_witness_covers_workflow_and_component_rows(active):
    store, _ = active
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, "o/r")
        _clone_workflow_repository(db, "other/repo")
        db.execute(
            "INSERT INTO workflow_components(repository,target_kind,target_number,family,epoch,episode_id) "
            "SELECT 'other/repo',target_kind,target_number,family,epoch,episode_id "
            "FROM workflow_components WHERE repository='o/r' AND family='review_execution'",
        )
        inserted = read_review_archive_source_versions(db, "o/r")
        assert inserted.workflow_rows == before.workflow_rows + 2
        db.execute(
            "UPDATE workflows SET episode_json=episode_json WHERE repository='other/repo'",
        )
        db.execute(
            "UPDATE workflow_components SET episode_id=episode_id WHERE repository='other/repo'",
        )
        updated = read_review_archive_source_versions(db, "o/r")
        assert updated.workflow_rows == inserted.workflow_rows + 2
        db.execute("DELETE FROM workflow_components WHERE repository='other/repo'")
        db.execute("DELETE FROM workflows WHERE repository='other/repo'")
        deleted = read_review_archive_source_versions(db, "o/r")
        assert deleted.workflow_rows == updated.workflow_rows + 2
        assert deleted.checkpoint_sources == before.checkpoint_sources


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_checkpoint_witness_rejects_implicit_dedupe_replacement(active, operation):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "review-checkpoint-application", "application:fixture")
        source_rowid = db.execute(
            "SELECT rowid FROM dedupe_keys WHERE namespace='review-checkpoint-application' "
            "AND key='application:fixture'",
        ).fetchone()[0]
        before = read_review_archive_source_versions(db, "o/r")
        if operation == "insert":
            mutation = (
                "INSERT OR REPLACE INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,'unrelated','o/r','replacement:fixture',"
                "'2026-09-29T00:00:00+00:00','{}')"
            )
        else:
            _insert_receipt(db, "unrelated", "replacement:fixture")
            mutation = (
                "UPDATE OR REPLACE dedupe_keys SET rowid=? "
                "WHERE namespace='unrelated' AND key='replacement:fixture'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="replacement would hide"):
            db.execute(mutation, (source_rowid,))
        assert read_review_archive_source_versions(db, "o/r") == before
        assert db.execute(
            "SELECT 1 FROM dedupe_keys WHERE rowid=? "
            "AND namespace='review-checkpoint-application'",
            (source_rowid,),
        ).fetchone() is not None


def test_checkpoint_witness_handles_negative_rowid_and_ordinary_insert(active):
    store, _ = active
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
            "VALUES (-1,'review-checkpoint-application','o/r','negative:source',"
            "'2026-09-29T00:00:00+00:00','{}')",
        )
        before = read_review_archive_source_versions(db, "o/r")
        _insert_receipt(db, "unrelated", "unrelated:auto")
        after_auto = read_review_archive_source_versions(db, "o/r")
        assert after_auto.checkpoint_sources == before.checkpoint_sources + 1
        assert db.execute("SELECT 1 FROM dedupe_keys WHERE rowid=-1").fetchone() is not None
        db.execute(
            "INSERT OR REPLACE INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
            "VALUES (-1,'unrelated','o/r','unrelated:replacement',"
            "'2026-09-29T00:00:00+00:00','{}')",
        )
        assert read_review_archive_source_versions(db, "o/r").checkpoint_sources == (
            after_auto.checkpoint_sources + 1
        )


def test_checkpoint_and_workflow_witnesses_survive_v29_migration(active):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "review-checkpoint-application", "application:fixture")
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 29)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, "o/r")
        assert (before.checkpoint_sources, before.workflow_rows) == (0, 0)
        db.execute(
            "UPDATE dedupe_keys SET payload_json='[]' "
            "WHERE namespace='review-checkpoint-application' AND key='application:fixture'",
        )
        workflow_updates = db.execute(
            "UPDATE workflows SET episode_json=episode_json WHERE repository='o/r'"
        ).rowcount
        after = read_review_archive_source_versions(db, "o/r")
        assert workflow_updates > 0
        assert (after.checkpoint_sources, after.workflow_rows) == (1, workflow_updates)


def test_checkpoint_and_workflow_witnesses_rollback_with_source_mutations(active):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "review-checkpoint-application", "application:fixture")
        baseline = read_review_archive_source_versions(db, "o/r")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        db.execute(
            "UPDATE dedupe_keys SET payload_json='[]' "
            "WHERE namespace='review-checkpoint-application' AND key='application:fixture'"
        )
        workflow_updates = db.execute(
            "UPDATE workflows SET episode_json=episode_json WHERE repository='o/r'"
        ).rowcount
        changed = read_review_archive_source_versions(db, "o/r")
        assert changed.checkpoint_sources == baseline.checkpoint_sources + 1
        assert changed.workflow_rows == baseline.workflow_rows + workflow_updates
        db.rollback()
    with store.transaction() as db:
        assert read_review_archive_source_versions(db, "o/r") == baseline
        assert db.execute(
            "SELECT payload_json FROM dedupe_keys "
            "WHERE namespace='review-checkpoint-application' AND key='application:fixture'"
        ).fetchone()[0] == "{}"


@pytest.mark.parametrize("source,mutation", (
    ("checkpoint_sources", "UPDATE dedupe_keys SET payload_json='[]' "
                            "WHERE namespace='review-checkpoint-application' "
                            "AND key='application:fixture'"),
    ("workflow_rows", "UPDATE workflows SET episode_json=episode_json WHERE repository='o/r'"),
))
def test_checkpoint_and_workflow_witnesses_block_missing_and_overflow(active, source, mutation):
    store, _ = active
    with store.transaction() as db:
        _insert_receipt(db, "review-checkpoint-application", "application:fixture")
        db.execute(
            "UPDATE review_archive_source_versions SET version=? WHERE source=? AND scope=X''",
            (MAX_ARCHIVE_SOURCE_VERSION, source),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(mutation)
        db.execute(
            "DELETE FROM review_archive_source_versions WHERE source=? AND scope=X''",
            (source,),
        )
        with pytest.raises(WorkflowAuthorityError, match="mutation witness is absent"):
            read_review_archive_source_versions(db, "o/r")
        with pytest.raises(sqlite3.IntegrityError, match="source witness is absent"):
            db.execute(mutation)


def test_lifecycle_witness_covers_command_and_effect_mutations(active):
    store, _ = active
    with store.transaction() as db:
        before = read_review_lifecycle_source_version(db)
        _insert_lifecycle_command(db)
        _insert_lifecycle_effect(db)
        inserted = read_review_lifecycle_source_version(db)
        assert inserted == before + 2
        db.execute(
            "UPDATE lifecycle_commands SET repository=CAST('other/repo' AS BLOB) "
            "WHERE command_id='fixture:command'"
        )
        db.execute(
            "UPDATE effect_receipts SET remote_revision=remote_revision "
            "WHERE command_id='fixture:command'"
        )
        assert read_review_lifecycle_source_version(db) == inserted + 2
        db.execute("DELETE FROM effect_receipts WHERE command_id='fixture:command'")
        db.execute("DELETE FROM lifecycle_commands WHERE command_id='fixture:command'")
        assert read_review_lifecycle_source_version(db) == inserted + 4


def test_lifecycle_witness_catches_implicit_replacement(active):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db)
        before = read_review_lifecycle_source_version(db)
        db.execute(
            "INSERT OR REPLACE INTO lifecycle_commands(command_id,idempotency_key,logical_key,"
            "kind,target,payload_json,payload_digest,state,created_at,updated_at) "
            "VALUES ('replacement:command','fixture:command','replacement:command',"
            "'other','pr:1','{}',?,'succeeded',"
            "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00')",
            ("a" * 64,),
        )
        assert read_review_lifecycle_source_version(db) == before + 1
        assert db.execute(
            "SELECT command_id FROM lifecycle_commands WHERE idempotency_key='fixture:command'"
        ).fetchone()[0] == "replacement:command"
        _insert_lifecycle_effect(db, "replacement:command")
        with_effect = read_review_lifecycle_source_version(db)
        db.execute(
            "INSERT OR REPLACE INTO effect_receipts(command_id,executor_identity,started_at,"
            "completed_at,status,receipt_digest) VALUES ('replacement:command','fixture',"
            "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00','succeeded',?)",
            ("c" * 64,),
        )
        assert read_review_lifecycle_source_version(db) == with_effect + 1


def test_lifecycle_witness_catches_implicit_update_replacement(active):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db, "fixture:first", state="pending")
        _insert_lifecycle_command(db, "fixture:second", state="pending")
        before = read_review_lifecycle_source_version(db)
        db.execute(
            "UPDATE OR REPLACE lifecycle_commands SET idempotency_key='fixture:first' "
            "WHERE command_id='fixture:second'"
        )
        assert read_review_lifecycle_source_version(db) == before + 1
        assert db.execute(
            "SELECT command_id FROM lifecycle_commands WHERE idempotency_key='fixture:first'"
        ).fetchone()[0] == "fixture:second"


def test_lifecycle_witness_survives_v30_migration_with_existing_rows(active):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db)
        _insert_lifecycle_effect(db)
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 30)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        assert read_review_lifecycle_source_version(db) == 0
        db.execute(
            "UPDATE lifecycle_commands SET repository='o/r' WHERE command_id='fixture:command'"
        )
        db.execute(
            "UPDATE effect_receipts SET remote_revision='fixture' "
            "WHERE command_id='fixture:command'"
        )
        assert read_review_lifecycle_source_version(db) == 2


def test_lifecycle_witness_rollback_is_atomic(active):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db)
        _insert_lifecycle_effect(db)
        baseline = read_review_lifecycle_source_version(db)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        db.execute(
            "UPDATE lifecycle_commands SET repository='other/repo' "
            "WHERE command_id='fixture:command'"
        )
        db.execute(
            "UPDATE effect_receipts SET remote_revision='changed' "
            "WHERE command_id='fixture:command'"
        )
        assert read_review_lifecycle_source_version(db) == baseline + 2
        db.rollback()
    with store.transaction() as db:
        assert read_review_lifecycle_source_version(db) == baseline
        assert db.execute(
            "SELECT remote_revision FROM effect_receipts WHERE command_id='fixture:command'"
        ).fetchone()[0] is None


@pytest.mark.parametrize("table,column", (
    ("lifecycle_commands", "repository"),
    ("effect_receipts", "remote_revision"),
))
def test_lifecycle_witness_blocks_missing_and_overflow(active, table, column):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db)
        _insert_lifecycle_effect(db)
        db.execute(
            "UPDATE review_archive_source_versions SET version=? "
            "WHERE source='lifecycle_rows' AND scope=X''",
            (MAX_ARCHIVE_SOURCE_VERSION,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE {table} SET {column}={column} WHERE command_id='fixture:command'")
        db.execute(
            "DELETE FROM review_archive_source_versions "
            "WHERE source='lifecycle_rows' AND scope=X''"
        )
        with pytest.raises(WorkflowAuthorityError, match="lifecycle row mutation witness"):
            read_review_lifecycle_source_version(db)
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle source witness"):
            db.execute(f"UPDATE {table} SET {column}={column} WHERE command_id='fixture:command'")


@pytest.mark.parametrize("source,scope,mutation,unchanged", (
    ("round_rows", b"o/r",
     "UPDATE OR IGNORE review_execution_rounds SET planning_json='[]' WHERE round_id=?",
     "SELECT planning_json FROM review_execution_rounds WHERE round_id=?"),
    ("all_round_rows", b"",
     "UPDATE OR IGNORE review_execution_rounds SET planning_json='[]' WHERE round_id=?",
     "SELECT planning_json FROM review_execution_rounds WHERE round_id=?"),
    ("round_receipts", b"",
     "UPDATE OR IGNORE dedupe_keys SET payload_json='[]' WHERE key='stage:fixture'",
     "SELECT payload_json FROM dedupe_keys WHERE key='stage:fixture'"),
    ("generation_sources", b"",
     "UPDATE OR IGNORE dedupe_keys SET payload_json='[]' WHERE key='generation:fixture'",
     "SELECT payload_json FROM dedupe_keys WHERE key='generation:fixture'"),
    ("checkpoint_sources", b"",
     "UPDATE OR IGNORE dedupe_keys SET payload_json='[]' WHERE key='checkpoint:fixture'",
     "SELECT payload_json FROM dedupe_keys WHERE key='checkpoint:fixture'"),
    ("workflow_rows", b"",
     "UPDATE OR IGNORE workflows SET episode_json='[]' WHERE repository='o/r'",
     "SELECT episode_json FROM workflows WHERE repository='o/r'"),
    ("lifecycle_rows", b"",
     "UPDATE OR IGNORE lifecycle_commands SET repository='other/repo' "
     "WHERE command_id='fixture:command'",
     "SELECT repository FROM lifecycle_commands WHERE command_id='fixture:command'"),
))
def test_archive_witness_rejects_or_ignore_overflow_for_every_source(
    active, source, scope, mutation, unchanged,
):
    store, snapshot = active
    with store.transaction() as db:
        round_id = _insert_round(db, snapshot, 1)
        _insert_receipt(db, "review-round-stage", "stage:fixture")
        _insert_receipt(db, "workflow-request", "generation:fixture")
        _insert_receipt(db, "review-checkpoint-application", "checkpoint:fixture")
        _insert_lifecycle_command(db)
        params = (round_id,) if "round_id=?" in mutation else ()
        before = db.execute(unchanged, params).fetchone()[0]
        db.execute(
            "UPDATE review_archive_source_versions SET version=? WHERE source=? AND scope=?",
            (MAX_ARCHIVE_SOURCE_VERSION, source, scope),
        )
        with pytest.raises(sqlite3.IntegrityError, match="witness version overflow"):
            db.execute(mutation, params)
        assert db.execute(unchanged, params).fetchone()[0] == before
        assert db.execute(
            "SELECT version FROM review_archive_source_versions WHERE source=? AND scope=?",
            (source, scope),
        ).fetchone()[0] == MAX_ARCHIVE_SOURCE_VERSION


@pytest.mark.parametrize("table", ("lifecycle_commands", "effect_receipts"))
def test_lifecycle_witness_rejects_or_ignore_insert_at_overflow(active, table):
    store, _ = active
    with store.transaction() as db:
        _insert_lifecycle_command(db)
        if table == "lifecycle_commands":
            mutation = (
                "INSERT OR IGNORE INTO lifecycle_commands(command_id,idempotency_key,logical_key,"
                "kind,target,payload_json,payload_digest,state,created_at,updated_at) "
                "VALUES ('fixture:extra','fixture:extra','fixture:extra','submit_review',"
                "'pr:1','{}',?,'succeeded',"
                "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00')"
            )
            params = (hashlib.sha256(b"{}").hexdigest(),)
            absent = "SELECT 1 FROM lifecycle_commands WHERE command_id='fixture:extra'"
        else:
            mutation = (
                "INSERT OR IGNORE INTO effect_receipts(command_id,executor_identity,"
                "started_at,completed_at,status,receipt_digest) VALUES ('fixture:command',"
                "'fixture','2026-09-29T00:00:00+00:00',"
                "'2026-09-29T00:00:00+00:00','succeeded',?)"
            )
            params = ("b" * 64,)
            absent = "SELECT 1 FROM effect_receipts WHERE command_id='fixture:command'"
        db.execute(
            "UPDATE review_archive_source_versions SET version=? "
            "WHERE source='lifecycle_rows' AND scope=X''",
            (MAX_ARCHIVE_SOURCE_VERSION,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="witness version overflow"):
            db.execute(mutation, params)
        assert db.execute(absent).fetchone() is None
        assert read_review_lifecycle_source_version(db) == MAX_ARCHIVE_SOURCE_VERSION


def test_lifecycle_witness_requires_transaction(active):
    store, _ = active
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_review_lifecycle_source_version(db)


def test_review_archive_witness_requires_transaction(active):
    store, _ = active
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_review_archive_source_versions(db, "o/r")
