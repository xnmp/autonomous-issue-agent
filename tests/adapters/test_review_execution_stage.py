"""Ordered unpaid child-stage registration bound to one live review parent."""

from dataclasses import replace
import sqlite3
import subprocess
import sys

import pytest

from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, WorkflowRevisionConflict
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import ReviewChildIdentity, ReviewChildKind
from issue_agent.domain.review_rounds import ReviewPlanningComplete, ReviewPlanningSealed, ReviewRoundPlan, ReviewStagePlan
from tests.adapters.test_review_execution_control import OWNER, assert_unpaid, current, dump, rig


def stage(plan, kind=ReviewChildKind.REVIEWER, *, name="review", child="reviewer"):
    return ReviewStagePlan(name, kind, (ReviewChildIdentity(plan.round, name, child, kind),))


def test_registering_one_stage_advances_only_aggregate_version_and_keeps_the_outer_sequence(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    before = current(store, selection)
    child_stage = stage(plan)
    planned = control.register_stage(plan.round.round_id, before.revision, child_stage)
    after = current(store, selection)
    assert planned == replace(plan, stages=(child_stage,))
    assert after.revision.version == before.revision.version + 1
    assert after.revision.fence == before.revision.fence
    assert after.execution == before.execution and after.episode == before.episode
    with store.transaction() as db:
        persisted = decode_workflow(db.execute("SELECT plan_json FROM review_execution_rounds").fetchone()[0], ReviewRoundPlan)
    assert persisted == planned
    assert_unpaid(store)


def test_exact_stage_replay_is_inert_after_parent_claim_release(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    child_stage = stage(plan)
    registered = control.register_stage(plan.round.round_id, expected, child_stage)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    gateway.error = AssertionError("stage replay read gateway")
    before = dump(store)
    assert control.register_stage(plan.round.round_id, expected, child_stage) == registered
    assert dump(store) == before
    assert_unpaid(store)


def test_reusing_a_stage_identity_for_different_children_conflicts_without_mutation(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    control.register_stage(plan.round.round_id, expected, stage(plan))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        control.register_stage(plan.round.round_id, expected, stage(plan, child="different-child"))
    assert dump(store) == before
    assert_unpaid(store)


def test_stage_cas_conflict_preserves_the_original_plan_and_every_paid_table(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    with store.transaction() as db:
        db.execute("UPDATE workflows SET version=version+1 WHERE target_kind='pr'")
    before = dump(store)
    with pytest.raises(WorkflowRevisionConflict):
        control.register_stage(plan.round.round_id, expected, stage(plan))
    assert dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("replacement", [False, True])
def test_new_stage_requires_its_original_still_owned_claim_occurrence(rig, replacement):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    if replacement:
        assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        control.register_stage(plan.round.round_id, expected, stage(plan))
    assert dump(store) == before
    assert_unpaid(store)


def test_parent_planning_seal_prevents_late_child_registration(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET planning_json=?",
                   (encode_workflow(ReviewPlanningSealed("sealed:1", ReviewPlanningComplete())),))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        control.register_stage(plan.round.round_id, expected, stage(plan))
    assert dump(store) == before
    assert_unpaid(store)


def test_two_multi_review_stages_retain_gate_then_dimensions_without_reallocating_a_round(rig):
    store, _, _, selection, control = rig
    selection = replace(selection, mode=ReviewMode.MULTI)
    plan = control.preflight(selection)
    before = current(store, selection)
    gate = stage(plan, ReviewChildKind.GATE, name="gate", child="gate")
    dimensions = ReviewStagePlan("dimensions", ReviewChildKind.DIMENSION, tuple(
        ReviewChildIdentity(plan.round, "dimensions", name, ReviewChildKind.DIMENSION) for name in ("correctness", "security")))
    control.register_stage(plan.round.round_id, before.revision, gate)
    after_gate = current(store, selection)
    planned = control.register_stage(plan.round.round_id, after_gate.revision, dimensions)
    after = current(store, selection)
    assert planned.stages == (gate, dimensions)
    assert tuple(child.child_id for child in planned.children) == ("gate", "correctness", "security")
    assert after.revision.version == before.revision.version + 2
    assert after.execution.last_sequence == 1 and after.execution.state == before.execution.state
    assert_unpaid(store)


@pytest.mark.parametrize("invalid", ["single_gate", "multi_dimensions_first", "foreign_round", "second_reviewer", "duplicate_child"])
def test_mode_stage_order_and_child_membership_refusals_are_atomic(rig, invalid):
    store, _, _, selection, control = rig
    if invalid in {"multi_dimensions_first", "duplicate_child"}:
        selection = replace(selection, mode=ReviewMode.MULTI)
    plan = control.preflight(selection)
    if invalid == "single_gate":
        proposed = stage(plan, ReviewChildKind.GATE, name="gate")
    elif invalid == "multi_dimensions_first":
        proposed = stage(plan, ReviewChildKind.DIMENSION, name="dimensions")
    elif invalid == "foreign_round":
        proposed = stage(replace(plan, round=replace(plan.round, round_id="another-round")))
    elif invalid == "second_reviewer":
        control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
        proposed = stage(plan, name="second", child="second-reviewer")
    else:
        control.register_stage(plan.round.round_id, current(store, selection).revision,
                               stage(plan, ReviewChildKind.GATE, name="gate", child="shared-id"))
        proposed = stage(plan, ReviewChildKind.DIMENSION, name="dimensions", child="shared-id")
    expected = current(store, selection).revision
    before = dump(store)
    with pytest.raises((ValueError, WorkflowAuthorityError)):
        control.register_stage(plan.round.round_id, expected, proposed)
    assert dump(store) == before
    assert_unpaid(store)


def test_failure_to_persist_stage_rolls_back_the_plan_and_aggregate_version(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_stage BEFORE UPDATE OF plan_json ON review_execution_rounds "
                   "BEGIN SELECT RAISE(ABORT,'stage write refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="stage write refused"):
        control.register_stage(plan.round.round_id, expected, stage(plan))
    assert dump(store) == before
    assert_unpaid(store)


def test_an_exited_real_parent_cannot_append_to_its_preflight_round(rig):
    store, _, _, selection, control = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    child = subprocess.run([sys.executable, "-c", """
import sys
from dataclasses import replace
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_control import Gateway
selection = decode_workflow(sys.argv[3], ReviewPreflightSelection)
store = SQLiteControlStore(sys.argv[1], now=lambda: selection.observation.observed_at)
store.initialize()
assert store.claim_dispatch('o/r','review',-9,'REVIEW',sys.argv[2],frozenset())
claim = store.active_dispatch_claims('o/r','review')[0]
control = SQLiteReviewExecutionControl(store, WorkflowWriter('o/r','review',sys.argv[2]), Gateway())
print(encode_workflow(control.preflight(replace(selection, claim_occurrence_id=claim.claim_key))))
""", store.path, OWNER, encode_workflow(selection)], capture_output=True, text=True, check=True)
    plan = decode_workflow(child.stdout.strip(), ReviewRoundPlan)
    expected = current(store, selection).revision
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="claim|parent"):
        control.register_stage(plan.round.round_id, expected, stage(plan))
    assert dump(store) == before
    assert_unpaid(store)


def test_registered_stage_retains_its_exact_scoped_immutable_receipt(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed)
    with store.transaction() as db:
        receipts = db.execute("SELECT scope,payload_json FROM dedupe_keys WHERE namespace='review-round-stage'").fetchall()
    assert len(receipts) == 1 and receipts[0]["scope"] == "o/r"
    assert decode_workflow(receipts[0]["payload_json"], tuple) == (plan.round.round_id, proposed)
    assert_unpaid(store)


@pytest.mark.parametrize("corruption", ["missing", "wrong_round", "changed_stage", "unreadable"])
def test_stage_replay_requires_its_original_intact_receipt(rig, corruption):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, expected, proposed)
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stage'")
        else:
            payload = ("unreadable" if corruption == "unreadable" else
                       encode_workflow(("another-round", proposed)) if corruption == "wrong_round" else
                       encode_workflow((plan.round.round_id, stage(plan, child="different-child"))))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-stage'", (payload,))
    before = dump(store)
    with pytest.raises((WorkflowAuthorityError, ValueError)):
        control.register_stage(plan.round.round_id, expected, proposed)
    assert dump(store) == before
    assert_unpaid(store)


def test_stage_receipt_failure_rolls_back_plan_version_and_all_other_effects(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    expected = current(store, selection).revision
    proposed = stage(plan)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_stage_receipt BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-stage' BEGIN SELECT RAISE(ABORT,'stage receipt refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="stage receipt refused"):
        control.register_stage(plan.round.round_id, expected, proposed)
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_stage_receipt")
    assert control.register_stage(plan.round.round_id, expected, proposed).stages == (proposed,)
    assert_unpaid(store)


@pytest.mark.parametrize("corruption", ["missing", "changed"])
def test_appending_dimensions_requires_the_preceding_gate_stage_receipt(rig, corruption):
    store, _, _, selection, control = rig
    selection = replace(selection, mode=ReviewMode.MULTI)
    plan = control.preflight(selection)
    gate = stage(plan, ReviewChildKind.GATE, name="gate", child="gate")
    control.register_stage(plan.round.round_id, current(store, selection).revision, gate)
    expected = current(store, selection).revision
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stage'")
        else:
            changed = stage(plan, ReviewChildKind.GATE, name="gate", child="forged-gate")
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-stage'",
                       (encode_workflow((plan.round.round_id, changed)),))
    before = dump(store)
    dimensions = stage(plan, ReviewChildKind.DIMENSION, name="dimensions", child="correctness")
    with pytest.raises(WorkflowAuthorityError):
        control.register_stage(plan.round.round_id, expected, dimensions)
    assert dump(store) == before
    assert_unpaid(store)


def test_a_retained_stage_receipt_prevents_erasing_the_stage_and_registering_a_replacement(rig):
    store, _, _, selection, control = rig
    selection = replace(selection, mode=ReviewMode.MULTI)
    initial = control.preflight(selection)
    gate = stage(initial, ReviewChildKind.GATE, name="gate", child="accepted-gate")
    control.register_stage(initial.round.round_id, current(store, selection).revision, gate)
    expected = current(store, selection).revision
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                   (encode_workflow(initial), initial.round.round_id))
    before = dump(store)
    replacement = stage(initial, ReviewChildKind.GATE, name="replacement", child="different-gate")
    with pytest.raises(WorkflowAuthorityError):
        control.register_stage(initial.round.round_id, expected, replacement)
    assert dump(store) == before
    assert_unpaid(store)
