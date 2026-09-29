"""Read-only checkpoint predecessor pages from retained SQL receipts."""

import hashlib
from dataclasses import replace

import pytest

from issue_agent.adapters.review_checkpoint_chain import read_review_checkpoint_chain_page
from issue_agent.adapters.review_execution_activation import read_checkpoint_round_receipt_source
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain import review_execution as execution
from issue_agent.domain.review_checkpoint_chain import (
    ObservedReviewCheckpointChainPage, ReviewCheckpointChainCursor,
)
from tests.adapters.test_review_execution_persistence import (
    active, checkpoint_receipt, insert_checkpoint_receipt, insert_round,
    round_facts, set_projection,
)


def _two_checkpoint_chain(active):
    store, snapshot = active
    first, first_outcome = round_facts(snapshot, 1)
    second, second_outcome = round_facts(snapshot, 2)
    first_checkpoint = execution.project_review_execution(
        snapshot.execution.checkpoint, (first_outcome,),
    ).checkpoint
    second_checkpoint = execution.project_review_execution(
        snapshot.execution.checkpoint, (first_outcome, second_outcome),
    ).checkpoint
    with store.transaction() as db:
        insert_round(db, first, first_outcome)
        insert_round(db, second, second_outcome)
        first_receipt = checkpoint_receipt(db, snapshot, first_checkpoint, first.plan.round)
        first_id = insert_checkpoint_receipt(db, first_receipt)
        second_receipt = execution.ReviewExecutionCheckpointReceipt(
            snapshot.execution.origin, first_checkpoint, first_id, second_checkpoint,
            "a" * 64, hashlib.sha256(b"").hexdigest(),
            read_checkpoint_round_receipt_source(db, second.plan.round), None,
        )
        second_id = insert_checkpoint_receipt(db, second_receipt)
        compacted = replace(snapshot, execution=replace(
            snapshot.execution, checkpoint=second_checkpoint,
            state=second_checkpoint.state, last_sequence=2,
            checkpoint_receipt=second_id,
        ))
        set_projection(db, compacted, (), 2)
    return store, snapshot.revision.fence.key, first_id, second_id


def test_checkpoint_chain_pages_reach_exact_origin_without_writes(active):
    store, key, first_id, second_id = _two_checkpoint_chain(active)
    with store.transaction() as db:
        before = db.total_changes
        first = read_review_checkpoint_chain_page(db, key)
        assert db.total_changes == before
        assert isinstance(first, ObservedReviewCheckpointChainPage)
        assert tuple(item.receipt_id for item in first.sources) == (second_id,)
        assert first.next_cursor is not None
        second = read_review_checkpoint_chain_page(db, key, after=first.next_cursor)
        assert db.total_changes == before
    assert tuple(item.receipt_id for item in second.sources) == (first_id,)
    assert second.next_cursor is None


def test_checkpoint_chain_page_can_read_both_links_in_one_bounded_page(active):
    store, key, first_id, second_id = _two_checkpoint_chain(active)
    with store.transaction() as db:
        page = read_review_checkpoint_chain_page(db, key, limit=2)
    assert tuple(item.receipt_id for item in page.sources) == (second_id, first_id)
    assert page.next_cursor is None


def test_checkpoint_chain_fingerprint_includes_retained_claim_spelling(active):
    store, key, _, second_id = _two_checkpoint_chain(active)
    with store.transaction() as db:
        before = read_review_checkpoint_chain_page(db, key)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-27T00:00:00Z' "
            "WHERE key=?", (second_id,),
        )
    with store.transaction() as db:
        after = read_review_checkpoint_chain_page(db, key)
    assert before.sources[0].receipt == after.sources[0].receipt
    assert before.sources[0].claimed_at == after.sources[0].claimed_at
    assert before.sources[0].source_sha256 != after.sources[0].source_sha256


def test_checkpoint_chain_refuses_receipt_claim_before_origin(active):
    store, key, _, second_id = _two_checkpoint_chain(active)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE key=?", (second_id,),
        )
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="changed predecessor"):
            read_review_checkpoint_chain_page(db, key)


@pytest.mark.parametrize("corruption", ["missing", "later_claim", "blob_alias"])
def test_checkpoint_chain_next_page_rechecks_predecessor(active, corruption):
    store, key, first_id, _ = _two_checkpoint_chain(active)
    with store.transaction() as db:
        first = read_review_checkpoint_chain_page(db, key)
    assert first.next_cursor is not None
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE key=?", (first_id,))
        elif corruption == "later_claim":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                "WHERE key=?", (first_id,),
            )
        else:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "SELECT CAST(namespace AS BLOB),scope,key,claimed_at,payload_json "
                "FROM dedupe_keys WHERE key=?", (first_id,),
            )
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="checkpoint"):
            read_review_checkpoint_chain_page(db, key, after=first.next_cursor)


def test_checkpoint_chain_refuses_cursor_for_other_tip_and_invalid_limit(active):
    store, key, _, _ = _two_checkpoint_chain(active)
    with store.transaction() as db:
        page = read_review_checkpoint_chain_page(db, key)
        assert page.next_cursor is not None
        with pytest.raises(WorkflowAuthorityError, match="current tip"):
            read_review_checkpoint_chain_page(
                db, key, after=replace(page.next_cursor, tip_receipt_id="checkpoint:other"),
            )
        with pytest.raises(ValueError, match="bounded inputs"):
            read_review_checkpoint_chain_page(db, key, limit=17)
    with pytest.raises(ValueError, match="aware"):
        replace(page.next_cursor, newer_claimed_at=page.sources[0].claimed_at.replace(tzinfo=None))
