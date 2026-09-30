"""Current admission cutover preserves stopped conversion and held recovery."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.workflow_rows import (
    ReviewExecutionHeld, WorkflowAuthorityError, convert_legacy_review_execution,
    load_review_workflow_row, persist_workflow_snapshot,
)
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.domain.review_execution import ActivatedReviewExecution, CurrentReviewExecution
from issue_agent.domain.review_rounds import FinalizedReviewRound, ReviewSubmissionKind
from tests.adapters.test_review_execution_control import rig, OWNER, current, dump
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import settlement, returned
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_productive_settlement import workflow_for
from issue_agent.domain.contracts import EffectReceipt, EffectStatus


def hold_prior(store, snapshot):
    authority = ActivatedReviewExecution(snapshot.execution.origin, snapshot.execution.baseline,
                                        snapshot.execution.state, snapshot.execution.last_sequence)
    held = replace(snapshot, execution=authority)
    with store.transaction() as db:
        persist_workflow_snapshot(db, held)
        retained = db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0]
        with pytest.raises(ReviewExecutionHeld):
            load_review_workflow_row(db, snapshot.revision.fence.key)
    return held, retained


def test_stopped_conversion_is_bounded_idempotent_and_refuses_live_claims(rig):
    store, snapshot, _, selection, _ = rig
    held, retained = hold_prior(store, snapshot)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="quiescent"):
            convert_legacy_review_execution(db, selection.expected.fence.key)
        assert db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0] == retained
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        converted = convert_legacy_review_execution(db, selection.expected.fence.key)
        assert isinstance(converted.execution, CurrentReviewExecution)
        assert converted.revision == held.revision
        assert converted.execution.state == held.execution.state
        before = tuple(db.iterdump())
        assert convert_legacy_review_execution(db, selection.expected.fence.key) == converted
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("productive", [False, True])
def test_held_prior_refuses_admission_but_settles_money_and_round_once(rig, productive):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    planned = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    command = None
    if productive:
        command = command_for(current(store, selection), patch_id="0" * 64)
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.PAID)
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
        store.begin_command(command.command_id)
        store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                          EffectStatus.SUCCEEDED, remote_object_id="review:held"))
    reservation_retry = store.reserve(proposed)
    held, retained = hold_prior(store, current(store, selection))
    fresh = replace(selection, request_id="blocked:new", expected=held.revision)
    with pytest.raises(ReviewExecutionHeld):
        control.preflight(fresh)
    # Existing reservation and preflight retries do not grant another round.
    assert store.reserve(proposed) == reservation_retry
    assert control.preflight(selection).round.round_id == plan.round.round_id
    result = returned(store, proposed)
    payment = settlement(proposed)
    assert store.settle_review_return(payment, result)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    if not productive:
        control.seal_abandoned_planning(plan.round.round_id)
    finalized = (control.finalize_completed_round(plan.round.round_id) if productive
                 else control.finalize_abandoned_round(plan.round.round_id))
    assert isinstance(finalized, FinalizedReviewRound)
    with store.transaction() as db:
        assert db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0] == retained
        assert db.execute("SELECT COUNT(*),SUM(amount_micros) FROM charges").fetchone()[:] == (1, 12)
        with pytest.raises(ReviewExecutionHeld):
            load_review_workflow_row(db, selection.expected.fence.key)
    before = dump(store)
    assert store.settle_review_return(payment, result)
    assert (control.finalize_completed_round(plan.round.round_id) if productive
            else control.finalize_abandoned_round(plan.round.round_id)) == finalized
    assert dump(store) == before
    if productive:
        assert command is not None
        assert workflow_for(store, gateway).settle_submission(command.command_id) is None


def test_caught_stopped_conversion_failure_preserves_held_authority_and_outer_work(rig):
    store, snapshot, _, selection, _ = rig
    _, retained = hold_prior(store, snapshot)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        db.execute("CREATE TEMP TRIGGER fail_conversion BEFORE UPDATE ON workflow_components "
                   "BEGIN SELECT RAISE(ABORT,'caught conversion failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="caught conversion failure"):
            convert_legacy_review_execution(db, selection.expected.fence.key)
        db.execute("INSERT INTO level_state VALUES (?,?,?,?,?)", (
            "test-outer", "o/r", "committed", "{}", store.now().isoformat(),
        ))
    with store.transaction() as db:
        assert db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0] == retained
        assert db.execute("SELECT key FROM level_state WHERE namespace='test-outer'").fetchone()[0] == "committed"
        with pytest.raises(ReviewExecutionHeld):
            load_review_workflow_row(db, selection.expected.fence.key)


def test_finalization_exact_retry_survives_malformed_current_authority(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    finalized = control.finalize_abandoned_round(plan.round.round_id)
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='malformed' WHERE target_kind='pr'")
    before = dump(store)
    assert control.finalize_abandoned_round(plan.round.round_id) == finalized
    assert dump(store) == before


def test_old_certificate_and_unrelated_retained_history_do_not_gate_admission(rig):
    store, _, _, selection, control = rig
    with store.transaction() as db:
        db.execute("INSERT INTO level_state VALUES (?,?,?,?,?)", (
            "review-execution-archive-gate-v1", "o/r", "invalid-expired-certificate", "malformed", store.now().isoformat(),
        ))
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)", (
            "review-execution-generation", "unrelated/repository", "retained:malformed", store.now().isoformat(), "malformed",
        ))
    assert control.preflight(selection).round.sequence == 1


def test_schema34_legacy_file_mutations_do_not_regain_current_review_authority(rig, tmp_path):
    from issue_agent.adapters.store import Store

    store, _, _, selection, control = rig
    before = current(store, selection)
    legacy = Store(str(tmp_path / 'legacy-review'))
    legacy.set_infra_deaths(-9, 0)
    legacy.set_round(9, 10**12)
    legacy.save_pr_session(9, 'foreign-session')
    assert legacy.get_infra_deaths(-9) == 0
    assert legacy.get_round(9) == 10**12
    store.initialize()
    assert current(store, selection) == before
    assert control.preflight(selection).round.sequence == 1
    assert current(store, selection).execution.state == before.execution.state
