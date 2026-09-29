"""Retained checkpoint tail choices are bound to exact checkpoint receipts."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_checkpoint_application_source import (
    REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, read_review_checkpoint_application_source,
)
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, review_execution_checkpoint_receipt_id,
)
from issue_agent.domain.review_checkpoint_application import (
    ApplyAllCheckpointTailOutcomes, ApplyCheckpointTailByFollowingBoundary,
    application_receipt_from_proposal,
)
from tests.adapters.test_review_execution_activation_sources import (
    _accepted_generation, _stale_settlement,
)
from tests.adapters.test_review_execution_control import rig


def _record_application(db, store, manifest):
    receipt_id = review_execution_checkpoint_receipt_id(manifest.receipt)
    recorded = application_receipt_from_proposal(receipt_id, manifest.application)
    claimed_at = store.now().isoformat()
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-execution-checkpoint','o/r',?,?,?)",
        (receipt_id, claimed_at, encode_workflow(manifest.receipt)),
    )
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?,?,?,?,?)",
        (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, "o/r", receipt_id,
         claimed_at, encode_workflow(recorded)),
    )
    return receipt_id, recorded


@pytest.mark.parametrize("selected", (False, True))
def test_checkpoint_application_source_binds_real_manifest(rig, selected):
    if selected:
        store, stale_plan, _, _ = _stale_settlement(rig)
        sequence = stale_plan.round.sequence
    else:
        store = _accepted_generation(rig)
        sequence = 1
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, sequence)
        receipt_id, recorded = _record_application(db, store, manifest)
        before = db.total_changes
        observed = read_review_checkpoint_application_source(db, "o/r", receipt_id)
        assert observed.checkpoint == manifest.receipt
        assert observed.application == manifest.application
        assert observed.source_sha256 != "0" * 64
        assert observed.claimed_at == store.now()
        assert db.total_changes == before
        assert isinstance(recorded.tail, (
            ApplyCheckpointTailByFollowingBoundary if selected
            else ApplyAllCheckpointTailOutcomes
        ))


def test_checkpoint_application_source_fingerprints_retained_claim_spelling(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, 1)
        receipt_id, _ = _record_application(db, store, manifest)
        original = read_review_checkpoint_application_source(db, "o/r", receipt_id)
        row = db.execute(
            "SELECT claimed_at FROM dedupe_keys WHERE namespace=? AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
        ).fetchone()
        assert row["claimed_at"].endswith("+00:00")
        db.execute(
            "UPDATE dedupe_keys SET claimed_at=? WHERE namespace=? AND key=?",
            (row["claimed_at"].removesuffix("+00:00") + "Z",
             REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
        )
        changed = read_review_checkpoint_application_source(db, "o/r", receipt_id)
        assert changed.application == original.application
        assert changed.claimed_at == original.claimed_at
        assert changed.source_sha256 != original.source_sha256


def test_checkpoint_application_source_refuses_copied_foreign_repository_pair(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, 1)
        receipt_id, _ = _record_application(db, store, manifest)
        for namespace in ("review-execution-checkpoint", REVIEW_CHECKPOINT_APPLICATION_NAMESPACE):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
                "SELECT namespace,'other/repo',key,claimed_at,reusable_after,payload_json "
                "FROM dedupe_keys WHERE namespace=? AND scope='o/r' AND key=?",
                (namespace, receipt_id),
            )
        with pytest.raises(WorkflowAuthorityError, match="foreign repository"):
            read_review_checkpoint_application_source(db, "other/repo", receipt_id)


@pytest.mark.parametrize("damage", (
    "missing", "payload", "oversized", "foreign_key", "tail_start",
    "claim_time", "reusable", "alias",
))
def test_checkpoint_application_source_refuses_changed_or_missing_pair(rig, damage):
    store, stale_plan, _, _ = _stale_settlement(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, stale_plan.round.sequence)
        receipt_id, recorded = _record_application(db, store, manifest)
        if damage == "missing":
            db.execute(
                "DELETE FROM dedupe_keys WHERE namespace=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "payload":
            db.execute(
                "UPDATE dedupe_keys SET payload_json='{}' WHERE namespace=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "oversized":
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND key=?",
                ("x" * (MAX_WORKFLOW_PAYLOAD_BYTES + 1),
                 REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "foreign_key":
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND key=?",
                (encode_workflow(replace(recorded, checkpoint_receipt_id="foreign")),
                 REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "tail_start":
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND key=?",
                (encode_workflow(replace(
                    recorded, tail_start_sequence=manifest.plan.checkpoint.through_sequence + 1,
                )), REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "claim_time":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-30T00:00:00+00:00' "
                "WHERE namespace=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        elif damage == "reusable":
            db.execute(
                "UPDATE dedupe_keys SET reusable_after='2026-09-30T00:00:00+00:00' "
                "WHERE namespace=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id),
            )
        else:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (CAST(? AS BLOB),'o/r',?,?,?)",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, receipt_id,
                 store.now().isoformat(), encode_workflow(recorded)),
            )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint application source"):
            read_review_checkpoint_application_source(db, "o/r", receipt_id)
