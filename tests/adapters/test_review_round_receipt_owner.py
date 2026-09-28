"""One source is owned only when a finalized round claims its exact digest."""

from dataclasses import replace

from issue_agent.adapters.review_round_receipt_owner import (
    BlockedReviewRoundReceiptOwner, MatchedReviewRoundReceiptOwner,
    PendingOpenReviewRoundReceiptOwner,
    reconcile_review_round_receipt_owner,
)
from issue_agent.adapters.review_round_receipt_sweep import (
    ClassifiedReviewRoundReceiptSweepPage,
    read_classified_review_round_receipt_sweep_page,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.review_rounds import ReviewPlanningRefused, ReviewPlanningSealed
from tests.adapters.test_review_execution_control import current, rig
from tests.adapters.test_review_productive_settlement import (
    confirmed_copy, confirmed_escalation, workflow_for,
)


def _classified(db):
    after = upper = None
    sources = []
    while True:
        page = read_classified_review_round_receipt_sweep_page(
            db, after=after, upper=upper,
        )
        assert isinstance(page, ClassifiedReviewRoundReceiptSweepPage)
        sources.extend(page.sources)
        if not page.has_more:
            return tuple(sources)
        after, upper = page.next_cursor, page.upper_cursor


def test_every_retained_copied_round_receipt_has_exact_owner(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        before = db.total_changes
        results = tuple(reconcile_review_round_receipt_owner(db, source)
                        for source in _classified(db))
        assert db.total_changes == before
    assert results
    assert all(isinstance(result, MatchedReviewRoundReceiptOwner)
               and result.round == plan.round for result in results)
    assert sum(result.registration_source_sha256 is not None for result in results) == 1


def test_named_ceiling_receipt_resolves_through_its_round_row(rig):
    store, gateway, _, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-ceiling-productivity")
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, MatchedReviewRoundReceiptOwner)
    assert result.round == plan.round


def test_recorded_command_without_registration_is_unclaimed(rig):
    store, _, _, selection, _ = rig
    revision = current(store, selection).revision
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-recorded-submission','o/r','command:orphan',"
            "'2026-09-29T00:00:00+00:00',?)",
            (encode_workflow(revision),),
        )
        source = next(item for item in _classified(db)
                      if item.source.cursor.key == "command:orphan")
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "absent or aliased identity" in result.reason


def test_named_receipt_without_a_round_row_is_unclaimed(rig):
    store, _, _, selection, _ = rig
    revision = current(store, selection).revision
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-superseded-productivity','o/r','round:orphan',"
            "'2026-09-29T00:00:00+00:00',?)",
            (encode_workflow(("observation:orphan", revision,
                              "2026-09-29T00:00:00+00:00")),),
        )
        source = next(item for item in _classified(db)
                      if item.source.cursor.key == "round:orphan")
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "lacks one bounded canonical round row" in result.reason


def test_named_receipt_blocks_a_blob_round_id_alias(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    revision = current(store, selection).revision
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-superseded-productivity','o/r',?,"
            "'2026-09-29T00:00:00+00:00',?)",
            (plan.round.round_id, encode_workflow((
                "observation:alias", revision, "2026-09-29T00:00:00+00:00",
            ))),
        )
        db.execute(
            "INSERT INTO review_execution_rounds "
            "(round_id,repository,target_kind,target_number,execution_epoch,"
            "execution_episode,generation,sequence,claim_occurrence_id,"
            "plan_json,planning_json,closure_json,outcome_json) "
            "SELECT CAST(round_id AS BLOB),repository,target_kind,target_number,"
            "execution_epoch,execution_episode,generation,sequence+1,"
            "claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json "
            "FROM review_execution_rounds WHERE round_id=?",
            (plan.round.round_id,),
        )
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-superseded-productivity")
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "lacks one bounded canonical round row" in result.reason


def test_open_round_receipt_remains_explicitly_pending(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-preflight")
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, PendingOpenReviewRoundReceiptOwner)
    assert result.round == plan.round
    assert result.source == source


def test_changed_open_receipt_after_sweep_is_blocked(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-preflight")
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "changed after its sweep" in result.reason


def test_open_seal_requires_completed_planning(rig):
    store, _, _, _, plan, _ = confirmed_copy(rig)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-seal")
        assert isinstance(reconcile_review_round_receipt_owner(db, source),
                          PendingOpenReviewRoundReceiptOwner)
        row = db.execute(
            "SELECT planning_json FROM review_execution_rounds WHERE round_id=?",
            (plan.round.round_id,),
        ).fetchone()
        planning = decode_workflow(row["planning_json"], ReviewPlanningSealed)
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=? WHERE round_id=?",
            (encode_workflow(replace(
                planning, conclusion=ReviewPlanningRefused("refusal:foreign"),
            )), plan.round.round_id),
        )
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "does not plan this receipt" in result.reason


def test_changed_source_after_sweep_does_not_match_round(rig):
    store, gateway, _, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-preflight")
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "absent or changed" in result.reason


def test_changed_registration_after_recorded_marker_sweep_blocks_owner(rig):
    store, gateway, _, _, _, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        marker = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-recorded-submission")
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace='review-round-submission' AND key=?",
            (str(command.command_id),),
        )
        result = reconcile_review_round_receipt_owner(db, marker)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert result.source_rowid == marker.source.source_rowid


def test_changed_named_source_after_sweep_does_not_match_round(rig):
    store, gateway, _, _, plan, command = confirmed_escalation(rig, cause="ceiling")
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        source = next(item for item in _classified(db)
                      if item.source.cursor.namespace == "review-round-ceiling-productivity")
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-ceiling-productivity' AND key=?",
            (plan.round.round_id,),
        )
        result = reconcile_review_round_receipt_owner(db, source)
    assert isinstance(result, BlockedReviewRoundReceiptOwner)
    assert "absent or changed" in result.reason
