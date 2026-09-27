"""Terminal child facts require the exact paid or refusal provenance."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ControlStoreError
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import (
    ReusableReviewSession, ReviewCancellationReason, ReviewChildIdentity, ReviewChildKind, ReviewChildLaunchRegistration,
    ReviewPreparationFailure, ReviewPreparationKind,
)
from issue_agent.domain.review_rounds import (
    ReviewChildCancelled, ReviewChildFailed, ReviewChildInfrastructureFailure, ReviewChildPending,
    ReviewChildRefused, ReviewChildReleased, ReviewChildReturned,
    ReviewChildSettledWithoutLaunch, ReviewStagePlan,
)
from issue_agent.domain.review_return import ReturnedReviewChild
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.contracts import PricingBasis
from tests.adapters.test_review_execution_admission import planned, receipt, request
from tests.adapters.test_review_execution_control import OWNER, current, rig
from tests.adapters.test_review_execution_launch import admitted, register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_preflight_stop import codex_admitted, codex_planned, stop


def observed(store, child):
    return store.read_review_child_evidence("o/r", OWNER, child.round.round_id)


def test_unadmitted_and_admitted_children_remain_pending(planned):
    store, _, _, child = planned
    assert observed(store, child) == (ReviewChildPending(child),)
    assert store.reserve(request(child)).admitted
    assert observed(store, child) == (ReviewChildPending(child),)


def test_exact_budget_refusal_becomes_terminal_only_for_its_planned_child(planned):
    store, _, _, child = planned
    refusal = store.record_review_admission_refusal(request(child, cap=59))
    assert refusal is not None
    assert observed(store, child) == (ReviewChildRefused(child, refusal.receipt_id),)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-refusal'",
                   (encode_workflow(replace(refusal, claim_occurrence_id="wrong")),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


@pytest.mark.parametrize("reason", ["cancel before spawn", "operator\ncancelled", "R" * 10000])
def test_release_requires_immutable_receipt_and_complete_no_launch_proof(admitted, reason):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    assert store.release_unlaunched(proposed.run_id, reason).disposition is AccountingDisposition.AVAILABLE
    evidence = observed(store, child)
    assert evidence == (ReviewChildReleased(child, str(proposed.run_id),
                                            f"review-execution-release:{proposed.run_id}"),)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-release'")
    with pytest.raises(ControlStoreError):
        observed(store, child)


def test_release_receipt_without_released_money_refuses(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    store.release_unlaunched(proposed.run_id, "cancel before spawn")
    with store.transaction() as db:
        db.execute("UPDATE budget_reservations SET state='active' WHERE run_id=?", (str(proposed.run_id),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


def test_release_receipt_write_failure_rolls_back_money_and_run(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_release BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-release' "
                   "BEGIN SELECT RAISE(ABORT,'release receipt refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="release receipt refused"):
        store.release_unlaunched(proposed.run_id, "before spawn")
    assert observed(store, child) == (ReviewChildPending(child),)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM runs WHERE run_id=?", (str(proposed.run_id),)).fetchone()[0] == "reserved"
        assert db.execute("SELECT state FROM budget_reservations WHERE run_id=?", (str(proposed.run_id),)).fetchone()[0] == "active"


def test_preflight_stop_waits_for_atomic_zero_settlement(codex_admitted):
    store, _, proposed = codex_admitted
    child = proposed.review_child
    assert child is not None
    stopped = stop(store, proposed)
    assert observed(store, child) == (ReviewChildPending(child),)
    returned = ReturnedReviewChild(child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                   stopped.result, store.now())
    assert store.settle_review_preflight_stop(returned) is not None
    assert observed(store, child) == (ReviewChildSettledWithoutLaunch(
        child, str(proposed.run_id), f"codex:preflight-stop:{proposed.run_id}",
        stopped.receipt_id, ReviewPreparationFailure(ReviewPreparationKind.RUNTIME, stopped.receipt_id),
    ),)
    with store.transaction() as db:
        db.execute("DELETE FROM charges WHERE run_id=?", (str(proposed.run_id),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


@pytest.mark.parametrize("contradiction", ["launch", "released_money"])
def test_pending_preflight_stop_rejects_contradictory_execution_or_money(codex_admitted, contradiction):
    store, _, proposed = codex_admitted
    child = proposed.review_child
    assert child is not None
    stop(store, proposed)
    admitted = receipt(store)
    with store.transaction() as db:
        if contradiction == "launch":
            launch = ReviewChildLaunchRegistration(
                admitted, f"review-execution-launch:{proposed.run_id}",
                proposed.dispatch_id, 1, 1, 1, store.now(),
            )
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES ('review-execution-launch','o/r',?,?,?)",
                       (str(proposed.run_id), store.now().isoformat(), encode_workflow(launch)))
        else:
            db.execute("UPDATE budget_reservations SET state='released' WHERE run_id=?", (str(proposed.run_id),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


def test_guarded_launch_and_unsettled_return_are_not_terminal_child_facts(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    assert observed(store, child) == (ReviewChildPending(child),)


def test_guarded_launch_raw_return_and_settled_charge_mint_normal_success(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), raw).disposition is AccountingDisposition.AVAILABLE
    evidence = observed(store, child)
    assert len(evidence) == 1 and isinstance(evidence[0], ReviewChildReturned)
    assert evidence[0].launched.launch_receipt == f"review-execution-launch:{proposed.run_id}"
    assert evidence[0].return_receipt == f"review-execution-return:{proposed.run_id}"
    assert isinstance(evidence[0].result.session, ReusableReviewSession)
    assert evidence[0].result.session.handle == "session:1"
    assert evidence[0].result.session.source == evidence[0].launched


def test_normal_failed_return_is_typed_only_after_settlement(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(
        True, status="error", summary="agent failed", exit_reason=ExitReason.AGENT_ERROR,
    ))
    assert store.record_reservation_retained(proposed.run_id, "usage unknown", returned_review=raw)
    assert observed(store, child) == (ReviewChildPending(child),)
    assert store.settle_review_return(settlement(proposed, basis=PricingBasis.RECONCILED), raw).disposition is AccountingDisposition.AVAILABLE
    evidence = observed(store, child)
    assert len(evidence) == 1 and isinstance(evidence[0], ReviewChildReturned)
    assert isinstance(evidence[0].result, ReviewChildFailed)
    assert evidence[0].result.reason is ExitReason.AGENT_ERROR


def test_reconciled_charge_before_raw_return_stays_pending_until_return_capture(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed)
    assert store.settle(settlement(proposed, basis=PricingBasis.RECONCILED)).disposition is AccountingDisposition.AVAILABLE
    assert observed(store, child) == (ReviewChildPending(child),)
    assert not store.record_reservation_retained(proposed.run_id, "late raw return", returned_review=raw)
    evidence = observed(store, child)
    assert len(evidence) == 1 and isinstance(evidence[0], ReviewChildReturned)


@pytest.mark.parametrize("reason", [
    ExitReason.TRANSPORT_DEATH, ExitReason.BUDGET_EXHAUSTED,
])
def test_settled_non_striking_error_without_specific_disposition_stays_pending(admitted, reason):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(
        True, status="raised", exit_reason=reason,
    ))
    store.settle_review_return(settlement(proposed), raw)
    assert observed(store, child) == (ReviewChildPending(child),)


@pytest.mark.parametrize("reason", [ExitReason.KILLED_EXTERNALLY, ExitReason.RUNTIME_UNAVAILABLE])
def test_settled_infrastructure_return_mints_non_striking_child(admitted, reason):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(True, status="raised", exit_reason=reason))
    store.settle_review_return(settlement(proposed), raw)
    evidence = observed(store, child)
    assert len(evidence) == 1 and isinstance(evidence[0], ReviewChildReturned)
    assert evidence[0].result == ReviewChildInfrastructureFailure(reason)


def test_settled_operator_cancel_waits_for_exact_shutdown_intent(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed, result=AgentResult(
        True, status="cancelled", exit_reason=ExitReason.OPERATOR_CANCELLED,
    ))
    store.settle_review_return(settlement(proposed), raw)
    assert observed(store, child) == (ReviewChildPending(child),)


def test_shutdown_intent_and_settled_cancel_mint_noncharging_child(admitted):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    intents = store.record_owned_cancellation_intents(OWNER, "shutdown_deadline_spend_unknown")
    assert any(intent.run_id == proposed.run_id for intent in intents)
    raw = returned(store, proposed, result=AgentResult(
        True, status="cancelled", exit_reason=ExitReason.OPERATOR_CANCELLED,
    ))
    store.settle_review_return(settlement(proposed), raw)
    evidence = observed(store, child)
    assert len(evidence) == 1 and isinstance(evidence[0], ReviewChildReturned)
    assert isinstance(evidence[0].result, ReviewChildCancelled)
    assert evidence[0].result.reason is ReviewCancellationReason.SHUTDOWN
    assert evidence[0].result.cancellation_receipt == f"run-cancellation-intent:{proposed.run_id}"


@pytest.mark.parametrize("corruption", ["missing_charge", "wrong_provider", "wrong_currency"])
def test_settled_return_refuses_corrupt_charge(admitted, corruption):
    store, _, proposed = admitted
    child = proposed.review_child
    assert child is not None
    register(store, proposed)
    raw = returned(store, proposed)
    store.settle_review_return(settlement(proposed), raw)
    with store.transaction() as db:
        if corruption == "missing_charge":
            db.execute("DELETE FROM charges WHERE run_id=?", (str(proposed.run_id),))
        elif corruption == "wrong_provider":
            db.execute("UPDATE runs SET provider_receipt_id='wrong' WHERE run_id=?", (str(proposed.run_id),))
        else:
            db.execute("UPDATE charges SET currency='EUR' WHERE run_id=?", (str(proposed.run_id),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


def test_mixed_multi_child_round_retains_each_exact_terminal_or_pending_fact(rig):
    store, _, _, selection, control = rig
    selection = replace(selection, mode=ReviewMode.MULTI)
    plan = control.preflight(selection)
    gate = ReviewChildIdentity(plan.round, "gate", "gate", ReviewChildKind.GATE)
    control.register_stage(plan.round.round_id, current(store, selection).revision,
                           ReviewStagePlan("gate", ReviewChildKind.GATE, (gate,)))
    first = ReviewChildIdentity(plan.round, "dimensions", "one", ReviewChildKind.DIMENSION)
    second = ReviewChildIdentity(plan.round, "dimensions", "two", ReviewChildKind.DIMENSION)
    control.register_stage(plan.round.round_id, current(store, selection).revision,
                           ReviewStagePlan("dimensions", ReviewChildKind.DIMENSION, (first, second)))
    refusal = store.record_review_admission_refusal(request(first, index=1, cap=59))
    assert refusal is not None
    proposed = request(second, index=2)
    assert store.reserve(proposed).admitted
    assert store.release_unlaunched(proposed.run_id, "before spawn").disposition is AccountingDisposition.AVAILABLE
    assert observed(store, gate) == (
        ReviewChildPending(gate), ReviewChildRefused(first, refusal.receipt_id),
        ReviewChildReleased(second, str(proposed.run_id), f"review-execution-release:{proposed.run_id}"),
    )


def test_future_refusal_revision_cannot_mint_terminal_evidence(planned):
    store, _, _, child = planned
    refusal = store.record_review_admission_refusal(request(child, cap=59))
    assert refusal is not None
    future = replace(refusal, refused_revision=replace(refusal.refused_revision,
                                                     version=refusal.refused_revision.version + 100))
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-refusal'",
                   (encode_workflow(future),))
    with pytest.raises(ControlStoreError):
        observed(store, child)


def test_paid_run_without_child_admission_index_refuses(planned):
    store, _, _, child = planned
    store.reserve(request(child))
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-child-admission'")
    with pytest.raises(ControlStoreError):
        observed(store, child)
