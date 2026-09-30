"""Abandoned planning requires the exact terminated parent claim occurrence."""

import sqlite3
from dataclasses import replace

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.control_store import ControlStoreError
from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.review_rounds import (
    ReviewChildNotAdmitted, ReviewParentAbandoned, ReviewPlanningSealed,
    ReviewSealedParentAbandoned,
)
from issue_agent.domain.review_execution import ReviewCancellationReason, ReviewRoundCancelled
from issue_agent.domain.contracts import CommandKind, LifecycleCommand
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_admission import planned, request
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


def test_sealed_parent_loss_keeps_completed_seal_and_terminalizes_unadmitted_child(planned):
    store, selection, control, child = planned
    sealed = control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)

    sweep = control.reconcile_review_rounds()

    assert sweep.sealed == sweep.finalized == (child.round.round_id,)
    assert sweep.blocked == ()
    facts = control.read_round_facts(child.round.round_id)
    assert isinstance(facts.planning, ReviewSealedParentAbandoned)
    assert facts.planning.completed_seal == sealed
    assert facts.children == (ReviewChildNotAdmitted(child, facts.planning.abandonment_receipt),)
    result = control.finalize_abandoned_round(child.round.round_id)
    assert result.outcome.disposition == ReviewRoundCancelled(
        ReviewCancellationReason.UNPAID_ABANDONMENT, facts.closure_receipt,
    )
    assert current(store, selection).execution.state.failures == 3
    before = dump(store)
    assert not store.reserve(request(child)).admitted
    assert control.reconcile_review_rounds().finalized == ()
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-seal'")
        assert load_review_workflow_row(db, selection.expected.fence.key) is not None
    with pytest.raises(WorkflowAuthorityError):
        control.finalize_abandoned_round(child.round.round_id)


def test_sealed_parent_without_terminal_claim_receipt_stays_open(planned):
    store, selection, control, child = planned
    sealed = control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    with store.transaction() as db:
        db.execute("DELETE FROM dispatch_claims WHERE occurrence_id=?", (selection.claim_occurrence_id,))

    sweep = control.reconcile_review_rounds()

    assert sweep.sealed == sweep.finalized == ()
    assert sweep.blocked and "terminal claim receipt" in sweep.blocked[0][1]
    with store.transaction() as db:
        row = db.execute("SELECT planning_json,closure_json,outcome_json FROM review_execution_rounds").fetchone()
        assert decode_workflow(row["planning_json"], ReviewPlanningSealed) == sealed
        assert row["closure_json"] is row["outcome_json"] is None


def test_recovery_sweep_fences_only_exact_terminal_parent_occurrences(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert control.reconcile_review_rounds().sealed == ()
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    sweep = control.reconcile_review_rounds()
    assert sweep.sealed == (plan.round.round_id,)
    assert sweep.finalized == (plan.round.round_id,)
    assert sweep.blocked == () and not sweep.has_more
    with store.transaction() as db:
        planning = db.execute(
            "SELECT planning_json FROM review_execution_rounds WHERE round_id=?",
            (plan.round.round_id,),
        ).fetchone()[0]
    assert isinstance(decode_workflow(planning, ReviewParentAbandoned), ReviewParentAbandoned)
    assert control.reconcile_review_rounds().sealed == ()


def test_recovery_sweep_reports_missing_terminal_receipt_without_forging_abandonment(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute("DELETE FROM dispatch_claims WHERE occurrence_id=?", (selection.claim_occurrence_id,))
    before = dump(store)
    sweep = control.reconcile_review_rounds()
    assert sweep.sealed == ()
    assert sweep.blocked[0][0] == plan.round.round_id
    assert "terminal claim receipt" in sweep.blocked[0][1]
    assert dump(store) != before
    with store.transaction() as db:
        row = db.execute("SELECT planning_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        assert row["outcome_json"] is None
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-recovery-block'").fetchone()[0] == 1


def test_recovery_block_journal_deduplicates_alternating_faults(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    key = f"{selection.claim_occurrence_id}:released"
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND key=?", (key,))
    first = control.reconcile_review_rounds().blocked
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
            ("dispatch-claim-lifecycle", "o/r", key, store.now().isoformat(), "{}"),
        )
    second = control.reconcile_review_rounds().blocked
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND key=?", (key,))
    assert control.reconcile_review_rounds().blocked == first
    assert first != second and first[0][0] == plan.round.round_id
    with store.transaction() as db:
        payload = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-recovery-block'",
        ).fetchone()[0]
    assert len(decode_workflow(payload, tuple)) == 2


def test_recovery_sweep_pages_open_rounds_without_reprocessing_earlier_ones(rig, monkeypatch):
    import issue_agent.adapters.review_execution_control as module

    monkeypatch.setattr(module, "MAX_REVIEW_ROUND_SWEEP", 2)
    store, _, _, selection, control = rig
    round_ids = []
    for index in range(3):
        next_selection = replace(selection, request_id=f"preflight:page:{index}",
                                 expected=current(store, selection).revision)
        round_ids.append(control.preflight(next_selection).round.round_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    first = control.reconcile_review_rounds()
    second = control.reconcile_review_rounds()
    assert first.sealed == tuple(round_ids[:2]) and first.has_more
    assert first.finalized == tuple(round_ids[:2])
    assert second.sealed == (round_ids[2],) and not second.has_more
    assert second.finalized == (round_ids[2],)
    assert control.reconcile_review_rounds().sealed == ()


def test_recovery_sweep_bounds_a_backlog_of_fenced_but_unfinalized_rounds(rig, monkeypatch):
    import issue_agent.adapters.review_execution_control as module

    monkeypatch.setattr(module, "MAX_REVIEW_ROUND_SWEEP", 2)
    store, _, _, selection, control = rig
    round_ids = []
    for index in range(5):
        next_selection = replace(selection, request_id=f"preflight:backlog:{index}",
                                 expected=current(store, selection).revision)
        round_ids.append(control.preflight(next_selection).round.round_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    for round_id in round_ids[:4]:
        control.seal_abandoned_planning(round_id)
    first = control.reconcile_review_rounds()
    second = control.reconcile_review_rounds()
    third = control.reconcile_review_rounds()
    assert first.sealed == second.sealed == ()
    assert first.finalized == tuple(round_ids[:2])
    assert second.finalized == tuple(round_ids[2:4])
    assert first.has_more and second.has_more
    assert third.sealed == (round_ids[4],) and not third.has_more
    assert third.finalized == (round_ids[4],)


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
    with pytest.raises(WorkflowAuthorityError, match="abandonment fence"):
        current(store, selection)
    assert dump(store) == before


def test_recovery_cannot_release_a_replacement_claim_from_an_old_liveness_sample(rig, monkeypatch):
    from issue_agent.domain.actions import AgentKind

    store, _, _, selection, control = rig
    plan = control.preflight(selection)

    def replace_dead_claim(*_):
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
        return False

    with monkeypatch.context() as patch:
        patch.setattr(ProcessSupervisor, "matches_identity", staticmethod(replace_dead_claim))
        assert control.reconcile_review_rounds().sealed == ()
    replacement = store.active_dispatch_claims("o/r", "review")[0].claim_key
    assert replacement != selection.claim_occurrence_id
    sweep = control.reconcile_review_rounds()
    assert sweep.finalized == (plan.round.round_id,)
    assert store.active_dispatch_claims("o/r", "review")[0].claim_key == replacement


def test_conditional_parent_release_refuses_a_changed_process_identity(rig):
    store, _, _, selection, _ = rig
    before = dump(store)
    assert not store.release_dispatch_claim("o/r", "review", -9, OWNER,
                                           expected_occurrence_id=selection.claim_occurrence_id,
                                           expected_process_identity=(1, 1))
    assert dump(store) == before


def test_caught_abandonment_receipt_failure_cannot_commit_a_partial_fence(rig, monkeypatch):
    from contextlib import contextmanager

    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_abandonment BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-abandonment' BEGIN SELECT RAISE(ABORT,'abandonment refused'); END")
    before = dump(store)
    with store.transaction() as db:
        @contextmanager
        def borrowed_transaction():
            yield db

        monkeypatch.setattr(store, "transaction", borrowed_transaction)
        with pytest.raises(sqlite3.IntegrityError, match="abandonment refused"):
            control.seal_abandoned_planning(plan.round.round_id)
    monkeypatch.undo()
    assert dump(store) == before


def test_full_pending_suffix_allows_money_settlement_and_existing_round_recovery(rig, monkeypatch):
    from issue_agent.domain import review_preflight
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from issue_agent.adapters.control_store import AccountingDisposition
    from tests.adapters.test_review_execution_launch import register
    from tests.adapters.test_review_execution_return import returned, settlement

    monkeypatch.setattr(review_preflight, "MAX_EXECUTION_SUFFIX", 2)
    store, _, _, selection, control = rig
    paid = []
    for index in (1, 2):
        plan = control.preflight(replace(selection, request_id=f"recover-full:{index}",
                                        expected=current(store, selection).revision))
        child_stage = stage(plan)
        control.register_stage(plan.round.round_id, current(store, selection).revision, child_stage)
        proposed = request(child_stage.children[0], index=index, cap=200)
        assert store.reserve(proposed).admitted
        register(store, proposed)
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
        paid.append(proposed)
    with pytest.raises(ValueError, match="pending capacity"):
        control.preflight(replace(selection, request_id="recover-full:refused",
                                 expected=current(store, selection).revision))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    failure = AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR)
    for proposed in reversed(paid):
        assert store.settle_review_return(settlement(proposed), returned(store, proposed, result=failure)).disposition is AccountingDisposition.AVAILABLE
    sweep = control.reconcile_review_rounds()
    assert set(sweep.finalized) == {proposed.review_child.round.round_id for proposed in paid}
    assert sweep.blocked == ()
    assert store.snapshot(paid[0].scope).committed.micros == 24
    assert store.snapshot(paid[0].scope).reserved.micros == 0
    assert current(store, selection).execution.state.failures == 5
    before = dump(store)
    assert control.reconcile_review_rounds().finalized == ()
    assert dump(store) == before


def test_recovery_limit_holds_new_allocation_but_retry_and_valid_settlement_clear_it(rig):
    from issue_agent.domain.actions import AgentKind
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.adapters.test_review_execution_launch import register
    from tests.adapters.test_review_execution_return import returned, settlement

    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    child_stage = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, child_stage)
    paid = request(child_stage.children[0])
    assert store.reserve(paid).admitted
    register(store, paid)
    assert store.record_reservation_retained(paid.run_id, "actual launch returned no measured evidence")
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    for _ in range(8):
        control.reconcile_review_rounds()
    key = selection.expected.fence.key
    assert control.pending_recovery_hold(key) is not None
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    fresh = replace(selection, request_id="recovery-held:new",
                    claim_occurrence_id=store.active_dispatch_claims("o/r", "review")[0].claim_key,
                    expected=current(store, selection).revision)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="recovery limit"):
        control.preflight(fresh)
    assert dump(store) == before
    assert control.preflight(selection) == plan
    assert dump(store) == before
    failure = AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR)
    assert not store.settle_review_return(settlement(paid), returned(store, paid, result=failure)).idempotent
    assert control.reconcile_review_rounds().finalized == (plan.round.round_id,)
    assert control.pending_recovery_hold(key) is None
    assert current(store, selection).execution.state.failures == 4
    next_plan = control.preflight(replace(fresh, expected=current(store, selection).revision))
    assert next_plan.round.sequence == 2
    assert store.snapshot(paid.scope).committed.micros == 12


@pytest.mark.parametrize("boundary", ["reservation", "launch"])
def test_recovery_limit_refuses_late_child_purchase_and_launch_in_an_existing_round(rig, boundary):
    from tests.adapters.test_review_execution_launch import register

    store, _, _, selection, control = rig
    first = control.preflight(selection)
    first_stage = stage(first)
    control.register_stage(first.round.round_id, current(store, selection).revision, first_stage)
    unknown = request(first_stage.children[0], cap=200)
    assert store.reserve(unknown).admitted
    register(store, unknown)
    assert store.record_reservation_retained(unknown.run_id, "launched usage remains unavailable")
    control.seal_completed_planning(first.round.round_id, current(store, selection).revision)
    second = control.preflight(replace(selection, request_id="before-recovery-limit:second",
                                       expected=current(store, selection).revision))
    second_stage = stage(second)
    control.register_stage(second.round.round_id, current(store, selection).revision, second_stage)
    late = request(second_stage.children[0], index=2, cap=200)
    if boundary == "launch":
        assert store.reserve(late).admitted
    for _ in range(8):
        control.reconcile_review_rounds()
    assert control.pending_recovery_hold(selection.expected.fence.key) is not None
    money = store.snapshot(unknown.scope)
    if boundary == "reservation":
        refusal = store.reserve(late)
        assert not refusal.admitted and "recovery limit" in refusal.detail
    else:
        with pytest.raises(WorkflowAuthorityError, match="recovery limit"):
            register(store, late)
    assert store.snapshot(unknown.scope) == money
    assert store.leases() == ()
