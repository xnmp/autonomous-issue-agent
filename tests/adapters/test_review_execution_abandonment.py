"""Abandoned planning requires the exact terminated parent claim occurrence."""

import sqlite3

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.control_store import ControlStoreError
from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_rounds import ReviewParentAbandoned
from issue_agent.domain.contracts import CommandKind, LifecycleCommand
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_stage import stage


def test_live_or_merely_missing_parent_cannot_abandon_an_open_round(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="claim is still present"):
        control.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("DELETE FROM dispatch_claims WHERE occurrence_id=?", (selection.claim_occurrence_id,))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="terminal claim receipt"):
        control.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before


def test_exact_released_parent_seals_empty_round_and_fences_late_stage(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    abandoned = control.seal_abandoned_planning(plan.round.round_id)
    assert isinstance(abandoned, ReviewParentAbandoned)
    assert abandoned.abandonment_receipt == f"dispatch-claim-lifecycle:{selection.claim_occurrence_id}:released"
    with store.transaction() as db:
        row = db.execute("SELECT planning_json,closure_json,outcome_json FROM review_execution_rounds").fetchone()
        assert decode_workflow(row["planning_json"], ReviewParentAbandoned) == abandoned
        assert row["closure_json"] is None and row["outcome_json"] is None
    assert current(store, selection).execution.last_sequence == 1
    before = dump(store)
    assert control.seal_abandoned_planning(plan.round.round_id) == abandoned
    assert dump(store) == before
    with pytest.raises(WorkflowAuthorityError, match="claim"):
        control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    assert dump(store) == before


def test_replaced_claim_does_not_reopen_old_occurrence(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    from issue_agent.domain.actions import AgentKind
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    abandoned = control.seal_abandoned_planning(plan.round.round_id)
    assert abandoned.abandonment_receipt.endswith(":released")
    assert current(store, selection).execution.last_sequence == 1


def test_reclaimed_parent_occurrence_can_seal_its_open_round(rig, monkeypatch):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    monkeypatch.setattr(ProcessSupervisor, "matches_identity", staticmethod(lambda *_: False))
    from issue_agent.domain.actions import AgentKind
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    abandoned = control.seal_abandoned_planning(plan.round.round_id)
    assert abandoned.abandonment_receipt == f"dispatch-claim-lifecycle:{selection.claim_occurrence_id}:reclaimed"
    assert current(store, selection).execution.last_sequence == 1


def test_successor_owner_can_fence_predecessor_abandoned_round(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    successor_id = "o/r:review:successor"
    successor = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", successor_id), gateway)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="outside this deployment lineage"):
        successor.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?,?,'o/r','review',?)",
                   (OWNER, successor_id, store.now().isoformat()))
    abandoned = successor.seal_abandoned_planning(plan.round.round_id)
    assert abandoned.abandonment_receipt.endswith(":released")
    assert current(store, selection).execution.last_sequence == 1


def test_abandoned_planning_blocks_late_paid_admission_even_with_replacement_claim(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    registered = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    child = registered.children[0]
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    from issue_agent.domain.actions import AgentKind
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    control.seal_abandoned_planning(plan.round.round_id)
    before = dump(store)
    outcome = store.reserve(request(child))
    assert not outcome.admitted
    assert dump(store) == before


def test_active_execution_refuses_unregistered_submission_before_remote_write(rig):
    store, snapshot, _, selection, control = rig
    plan = control.preflight(selection)
    op = BoundReviewSubmissionOp(9, "APPROVE", "approved", "b" * 40, "review-bot", "0" * 64, None)
    command_id = bound_review_command_id(op, snapshot.binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                               "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                               workflow=snapshot.binding)
    before = dump(store)
    with pytest.raises(ControlStoreError, match="atomic round registration"):
        store.enqueue_command(command)
    assert dump(store) == before
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    before = dump(store)
    with pytest.raises(ControlStoreError, match="atomic round registration"):
        store.enqueue_command(command)
    assert dump(store) == before


@pytest.mark.parametrize("corruption", ["missing_created", "changed_terminal", "both_terminals"])
def test_claim_receipt_corruption_cannot_forge_abandonment(rig, corruption):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        if corruption == "missing_created":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND key=?",
                       (f"{selection.claim_occurrence_id}:created",))
        elif corruption == "changed_terminal":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='dispatch-claim-lifecycle' AND key=?",
                       (f"{selection.claim_occurrence_id}:released",))
        else:
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "SELECT namespace,scope,? ,claimed_at,payload_json FROM dedupe_keys "
                       "WHERE namespace='dispatch-claim-lifecycle' AND key=?",
                       (f"{selection.claim_occurrence_id}:reclaimed", f"{selection.claim_occurrence_id}:released"))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        control.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before


def test_abandonment_receipt_write_failure_rolls_back_planning_fence(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_abandonment BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-abandonment' "
                   "BEGIN SELECT RAISE(ABORT,'abandonment refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="abandonment refused"):
        control.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before


def test_loader_and_replay_refuse_lost_abandonment_fence(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-abandonment'")
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="immutable fence"):
        current(store, selection)
    with pytest.raises(WorkflowAuthorityError, match="immutable fence"):
        control.seal_abandoned_planning(plan.round.round_id)
    assert dump(store) == before


def test_loader_refuses_changed_terminal_claim_after_abandonment(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='dispatch-claim-lifecycle' "
                   "AND key=?", (f"{selection.claim_occurrence_id}:released",))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="terminal receipt"):
        current(store, selection)
    assert dump(store) == before


def test_loader_refuses_orphan_abandonment_receipt_on_open_round(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-round-abandonment", "o/r", plan.round.round_id, store.now().isoformat(), "[]"))
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="contradictory abandonment fence"):
        current(store, selection)
    assert dump(store) == before
