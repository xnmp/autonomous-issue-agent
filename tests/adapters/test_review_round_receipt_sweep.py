"""Indexed, bounded observations of retained review round receipt rows."""

from dataclasses import replace
from contextlib import closing

import pytest

import issue_agent.adapters.review_round_receipt_sweep as sweep
from issue_agent.adapters.review_round_receipt_sweep import (
    BlockedReviewRoundReceiptSweepPage, ClassifiedReviewRoundReceiptSweepPage,
    ObservedReviewCheckpointRoundClassifiedReceiptSet, ObservedReviewRoundReceiptSweepPage,
    ReviewRoundReceiptSweepCursor,
    read_classified_checkpoint_round_receipt_members,
    read_classified_review_round_receipt_sweep_page, read_review_round_receipt_sweep_page,
)
from issue_agent.domain.review_receipt_attribution import (
    BoundReviewRoundReceipt, CommandReviewRoundReceipt,
)
from issue_agent.domain.review_rounds import (
    ReviewSubmissionRegistration, StaleReviewProductivitySettlement,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.adapters.test_review_execution_persistence import active
from tests.adapters.test_review_execution_control import current, rig
from tests.adapters.test_review_productive_settlement import (
    accepted_generation_with_open_submission, confirmed_copy, confirmed_escalation,
    workflow_for,
)
from tests.adapters.test_review_execution_stage import stage


def _insert(db, namespace, scope, key, *, payload="{}"):
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?,?,?,'2026-09-29T00:00:00+00:00',?)",
        (namespace, scope, key, payload),
    )


def test_round_receipt_sweep_resumes_fixed_upper_and_revisits_later_insert(active, monkeypatch):
    store, _ = active
    monkeypatch.setattr(sweep, "MAX_ROUND_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        _insert(db, "review-round-stage", "other/repo", "stage:2")
        first = read_review_round_receipt_sweep_page(db)
    assert isinstance(first, ObservedReviewRoundReceiptSweepPage)
    assert first.has_more and len(first.sources) == 1
    assert first.sources[0].cursor.key == "round:1"
    with store.transaction() as db:
        _insert(db, "review-round-stage", "other/repo", "stage:3")
        second = read_review_round_receipt_sweep_page(
            db, after=first.next_cursor, upper=first.upper_cursor,
        )
    assert isinstance(second, ObservedReviewRoundReceiptSweepPage)
    assert not second.has_more
    assert tuple(source.cursor.key for source in second.sources) == ("stage:2",)
    with store.transaction() as db:
        next_pass = read_review_round_receipt_sweep_page(db)
    assert isinstance(next_pass, ObservedReviewRoundReceiptSweepPage)
    assert next_pass.has_more
    with store.transaction() as db:
        later = read_review_round_receipt_sweep_page(
            db, after=next_pass.next_cursor, upper=next_pass.upper_cursor,
        )
    assert isinstance(later, ObservedReviewRoundReceiptSweepPage)
    assert later.sources[0].cursor.key == "stage:2" and later.has_more
    with store.transaction() as db:
        final = read_review_round_receipt_sweep_page(
            db, after=later.next_cursor, upper=later.upper_cursor,
        )
    assert isinstance(final, ObservedReviewRoundReceiptSweepPage)
    assert tuple(source.cursor.key for source in final.sources) == ("stage:3",)
    assert not final.has_more


def test_round_receipt_sweep_observes_payload_digest_change(active):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        before = read_review_round_receipt_sweep_page(db)
    assert isinstance(before, ObservedReviewRoundReceiptSweepPage)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{\"changed\":true}' "
            "WHERE namespace='review-round-outcome' AND key='round:1'",
        )
        after = read_review_round_receipt_sweep_page(db)
    assert isinstance(after, ObservedReviewRoundReceiptSweepPage)
    assert after.sources[0].source_sha256 != before.sources[0].source_sha256


@pytest.mark.parametrize("column", ["namespace", "scope", "key"])
def test_round_receipt_sweep_blocks_blob_identity_aliases(active, column):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1")
        db.execute(
            f"UPDATE dedupe_keys SET {column}=CAST({column} AS BLOB) "
            "WHERE namespace='review-round-outcome' AND key='round:1'",
        )
        result = read_review_round_receipt_sweep_page(db)
    assert isinstance(result, BlockedReviewRoundReceiptSweepPage)
    assert result.source_rowid is not None


def test_round_receipt_sweep_blocks_oversized_source_without_loading_it(active):
    store, _ = active
    with store.transaction() as db:
        _insert(db, "review-round-outcome", "o/r", "round:1", payload="x" * (2**21 + 1))
        result = read_review_round_receipt_sweep_page(db)
    assert isinstance(result, BlockedReviewRoundReceiptSweepPage)
    assert "byte bound" in result.reason or "oversized" in result.reason


def test_round_receipt_sweep_requires_explicit_transaction(active):
    store, _ = active
    db = store._connect()
    try:
        with pytest.raises(ValueError, match="transaction"):
            read_review_round_receipt_sweep_page(db)
    finally:
        db.close()


def test_round_receipt_sweep_seeks_to_a_late_cursor(active):
    store, _ = active
    with store.transaction() as db:
        db.execute(
            "WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value<5000) "
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "SELECT 'review-round-stage','o/r',printf('stage:%06d',value),"
            "'2026-09-29T00:00:00+00:00','{}' FROM n",
        )
        calls = 0

        def count_steps():
            nonlocal calls
            calls += 1
            return 0

        db.set_progress_handler(count_steps, 100)
        try:
            page = read_review_round_receipt_sweep_page(
                db,
                after=ReviewRoundReceiptSweepCursor(
                    "review-round-stage", "o/r", "stage:004900",
                ),
                upper=ReviewRoundReceiptSweepCursor(
                    "review-round-stage", "o/r", "stage:005000",
                ),
            )
        finally:
            db.set_progress_handler(None, 0)
    assert isinstance(page, ObservedReviewRoundReceiptSweepPage)
    assert page.sources[0].cursor.key == "stage:004901" and page.has_more
    assert calls < 200


def test_classified_receipt_sweep_attributes_real_finalized_round(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        cursor = upper = None
        classified = []
        before = db.total_changes
        while True:
            page = read_classified_review_round_receipt_sweep_page(
                db, after=cursor, upper=upper,
            )
            assert isinstance(page, ClassifiedReviewRoundReceiptSweepPage)
            classified.extend(page.sources)
            if not page.has_more:
                break
            cursor, upper = page.next_cursor, page.upper_cursor
        assert db.total_changes == before
    by_namespace = {item.source.cursor.namespace: item.reference for item in classified}
    for namespace in (
        "review-round-preflight", "review-round-submission",
        "review-round-submission-index", "review-round-seal", "review-round-outcome",
    ):
        assert isinstance(by_namespace[namespace], BoundReviewRoundReceipt)
        assert by_namespace[namespace].round == plan.round
    assert isinstance(by_namespace["review-recorded-submission"], CommandReviewRoundReceipt)
    assert by_namespace["review-recorded-submission"].command_id == str(command.command_id)


def test_classified_checkpoint_round_members_match_real_receipt_payloads(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        before = db.total_changes
        result = read_classified_checkpoint_round_receipt_members(db, plan.round)
        assert db.total_changes == before
    assert isinstance(result, ObservedReviewCheckpointRoundClassifiedReceiptSet)
    assert tuple(item.member for item in result.receipts) == result.source.members
    assert all(
        item.reference == BoundReviewRoundReceipt(plan.round)
        for item in result.receipts
        if item.member.namespace != "review-recorded-submission"
    )
    recorded = next(item for item in result.receipts
                    if item.member.namespace == "review-recorded-submission")
    assert recorded.reference == CommandReviewRoundReceipt(str(command.command_id))


def test_classified_checkpoint_round_members_block_mutated_payload(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
        with pytest.raises(WorkflowAuthorityError, match="payload is unreadable"):
            read_classified_checkpoint_round_receipt_members(db, plan.round)


def test_classified_checkpoint_round_members_block_well_formed_foreign_round(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys "
            "WHERE namespace='review-round-submission' AND key=?",
            (str(command.command_id),),
        ).fetchone()
        registered = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
        foreign = replace(registered, round=replace(registered.round, round_id="round:foreign"))
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? "
            "WHERE namespace='review-round-submission' AND key=?",
            (encode_workflow(foreign), str(command.command_id)),
        )
        with pytest.raises(WorkflowAuthorityError, match="payload is unreadable"):
            read_classified_checkpoint_round_receipt_members(db, plan.round)


def test_classified_checkpoint_round_members_require_stale_generation_witness(rig):
    store, _, _, _, plan, command, workflow = accepted_generation_with_open_submission(rig)
    workflow.settle_submission(command.command_id)
    with store.transaction() as db:
        observed = read_classified_checkpoint_round_receipt_members(db, plan.round)
        assert any(item.member.namespace == "review-round-stale-productivity"
                   for item in observed.receipts)
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys "
            "WHERE namespace='review-round-stale-productivity' AND key=?",
            (plan.round.round_id,),
        ).fetchone()
        stale = decode_workflow(row["payload_json"], StaleReviewProductivitySettlement)
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? "
            "WHERE namespace='review-round-stale-productivity' AND key=?",
            (encode_workflow((stale.facts, stale.outcome)), plan.round.round_id),
        )
        with pytest.raises(WorkflowAuthorityError, match="payload is unreadable"):
            read_classified_checkpoint_round_receipt_members(db, plan.round)


def test_classified_checkpoint_round_members_require_transaction_and_round(rig):
    store, _, _, _, plan, _ = confirmed_copy(rig)
    with store.transaction() as db:
        with pytest.raises(ValueError, match="transaction and round"):
            read_classified_checkpoint_round_receipt_members(db, None)
    with closing(store._connect()) as db:
        with pytest.raises(ValueError, match="transaction and round"):
            read_classified_checkpoint_round_receipt_members(db, plan.round)


def test_classified_receipt_sweep_checks_stage_derived_key(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    with store.transaction() as db:
        page = read_classified_review_round_receipt_sweep_page(db)
        assert isinstance(page, ClassifiedReviewRoundReceiptSweepPage)
        stage_source = next(item for item in page.sources
                            if item.source.cursor.namespace == "review-round-stage")
        assert stage_source.reference == BoundReviewRoundReceipt(plan.round)
        db.execute(
            "UPDATE dedupe_keys SET key='stage:wrong' WHERE namespace='review-round-stage'",
        )
        blocked = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(blocked, BlockedReviewRoundReceiptSweepPage)
    assert "derived key" in blocked.reason


@pytest.mark.parametrize("namespace", [
    "review-recorded-submission", "review-round-superseded-productivity",
])
def test_classified_receipt_sweep_blocks_foreign_revision_repository(rig, namespace):
    store, _, _, selection, _ = rig
    revision = current(store, selection).revision
    payload = (revision if namespace == "review-recorded-submission" else
               ("observation:1", revision, "2026-09-29T00:00:00+00:00"))
    with store.transaction() as db:
        _insert(db, namespace, "another/repository", "receipt:foreign",
                payload=encode_workflow(payload))
        blocked = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(blocked, BlockedReviewRoundReceiptSweepPage)
    assert "foreign repository" in blocked.reason


def test_classified_receipt_sweep_blocks_foreign_ceiling_repository(rig):
    store, gateway, _, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET scope='another/repository' "
            "WHERE namespace='review-round-ceiling-productivity'",
        )
        blocked = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(blocked, BlockedReviewRoundReceiptSweepPage)
    assert "foreign repository" in blocked.reason


def test_classified_receipt_sweep_blocks_mixed_ceiling_repositories(rig):
    store, gateway, _, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys "
            "WHERE namespace='review-round-ceiling-productivity'",
        ).fetchone()
        decision, revision, stamp = decode_workflow(row["payload_json"], tuple)
        submission = decision.escalation.submission
        foreign_key = replace(submission.binding.fence.key, repository="another/repository")
        foreign_submission = replace(submission, binding=replace(
            submission.binding, fence=replace(submission.binding.fence, key=foreign_key),
        ))
        mixed = replace(decision, escalation=replace(
            decision.escalation, submission=foreign_submission,
        ))
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? "
            "WHERE namespace='review-round-ceiling-productivity'",
            (encode_workflow((mixed, revision, stamp)),),
        )
        blocked = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(blocked, BlockedReviewRoundReceiptSweepPage)
    assert "foreign repository" in blocked.reason


@pytest.mark.parametrize("mutation", ["naive_claim", "reusable"])
def test_classified_receipt_sweep_blocks_nonpermanent_claims(rig, mutation):
    store, _, _, selection, control = rig
    control.preflight(selection)
    with store.transaction() as db:
        if mutation == "naive_claim":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00' "
                "WHERE namespace='review-round-preflight'",
            )
        else:
            db.execute(
                "UPDATE dedupe_keys SET reusable_after='2026-09-30T00:00:00+00:00' "
                "WHERE namespace='review-round-preflight'",
            )
        blocked = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(blocked, BlockedReviewRoundReceiptSweepPage)
    assert blocked.source_rowid is not None


@pytest.mark.parametrize("namespace", sweep.ROUND_RECEIPT_NAMESPACES)
def test_classified_receipt_sweep_blocks_wrong_payload_shape(active, namespace):
    store, _ = active
    with store.transaction() as db:
        _insert(db, namespace, "o/r", "receipt:malformed")
        result = read_classified_review_round_receipt_sweep_page(db)
    assert isinstance(result, BlockedReviewRoundReceiptSweepPage)
    assert result.source_rowid is not None
