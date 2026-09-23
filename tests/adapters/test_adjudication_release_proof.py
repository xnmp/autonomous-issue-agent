"""Only durable never-launched proof restores adjudication allowance."""

from datetime import timedelta
import sqlite3

import pytest

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    ControlStoreError,
    CorruptControlStore,
    SettlementRequest,
    admitted_adjudications,
)
from issue_agent.domain.contracts import Money, PricingBasis, RunId
from tests.adapters.test_workflow_admission import (
    OWNER,
    adjudication_input,
    control,
    request,
    snapshot,
)
from tests.adapters.test_workflow_control import NOW


def _count(control) -> int:
    with control.transaction() as db:
        return admitted_adjudications(db, snapshot(control).binding)


def _settlement(name: str) -> SettlementRequest:
    return SettlementRequest(
        RunId(name),
        Money("USD", 1),
        PricingBasis.PROVIDER_REPORTED,
        f"provider:{name}",
        None,
        None,
        None,
    )


@pytest.mark.parametrize("execution_evidence", ["running", "ambiguous"])
def test_generic_release_with_execution_evidence_still_consumes_round(
    control,
    execution_evidence,
):
    supplied = adjudication_input(control)
    accepted = request(
        f"generic-{execution_evidence}",
        stage="ADJUDICATE",
        ceiling=3,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    if execution_evidence == "running":
        control.register_process_lease(
            accepted.run_id,
            expected_owner_id=OWNER,
            pid=101,
            pgid=101,
            process_start_ticks=10,
        )
    else:
        assert control.record_reservation_retained(
            accepted.run_id, "provider may have executed"
        )

    assert (
        control.release(accepted.run_id, "generic operator release").disposition
        is AccountingDisposition.AVAILABLE
    )
    assert _count(control) == 1


def test_accounting_retained_release_still_consumes_round(control):
    supplied = adjudication_input(control)
    accepted = request(
        "retained",
        stage="ADJUDICATE",
        ceiling=3,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    assert control.record_reservation_retained(
        accepted.run_id, "usage unavailable after provider return"
    )

    released = control.release_retained(
        accepted.run_id,
        "reconciliation grace elapsed",
        NOW + timedelta(minutes=1),
    )

    assert released.disposition is AccountingDisposition.AVAILABLE
    assert _count(control) == 1


def test_strict_release_winning_before_process_lease_is_terminal_and_exempt(
    control,
):
    supplied = adjudication_input(control)
    accepted = request(
        "release-first",
        stage="ADJUDICATE",
        ceiling=1,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    assert (
        control.release_unlaunched(
            accepted.run_id, "spawn failed before process registration"
        ).disposition
        is AccountingDisposition.AVAILABLE
    )
    assert _count(control) == 0

    with pytest.raises(ControlStoreError, match="terminal"):
        control.register_process_lease(
            accepted.run_id,
            expected_owner_id=OWNER,
            pid=201,
            pgid=201,
            process_start_ticks=20,
        )
    assert (
        control.settle(_settlement("release-first")).disposition
        is AccountingDisposition.CONFLICT
    )
    assert _count(control) == 0


def test_process_lease_winning_before_strict_release_consumes_round(control):
    supplied = adjudication_input(control)
    accepted = request(
        "lease-first",
        stage="ADJUDICATE",
        ceiling=1,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    control.register_process_lease(
        accepted.run_id,
        expected_owner_id=OWNER,
        pid=301,
        pgid=301,
        process_start_ticks=30,
    )

    refused = control.release_unlaunched(
        accepted.run_id, "too late to prove no execution"
    )
    assert refused.disposition is AccountingDisposition.CONFLICT
    assert _count(control) == 1
    assert (
        control.settle(_settlement("lease-first")).disposition
        is AccountingDisposition.AVAILABLE
    )
    assert _count(control) == 1


@pytest.mark.parametrize("terminal", ["unknown", "crashed"])
def test_unknown_or_crashed_adjudication_consumes_round(control, terminal):
    supplied = adjudication_input(control)
    accepted = request(
        f"count-{terminal}",
        stage="ADJUDICATE",
        ceiling=2,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    if terminal == "crashed":
        assert control.complete_run(
            accepted.run_id,
            state="abandoned",
            outcome="provider_crash_unknown_spend",
        )

    assert _count(control) == 1


def test_selected_malformed_never_launched_proof_refuses_paid_admission(control):
    supplied = adjudication_input(control)
    accepted = request(
        "malformed-proof",
        stage="ADJUDICATE",
        ceiling=1,
        split_input=supplied,
    )
    assert control.reserve(accepted).admitted
    assert (
        control.release_unlaunched(
            accepted.run_id, "fixture starts with valid strict proof"
        ).disposition
        is AccountingDisposition.AVAILABLE
    )

    # Historical-corruption fixture: the run still claims the exempt terminal
    # shape, but its supporting launch policy now says execution began.
    with sqlite3.connect(control.path) as db:
        db.execute(
            "UPDATE reservation_recovery SET policy='running' WHERE run_id=?",
            (str(accepted.run_id),),
        )

    with pytest.raises(CorruptControlStore, match="semantic integrity"):
        control.integrity_check()

    refused = control.reserve(request(
        "must-not-admit",
        stage="ADJUDICATE",
        ceiling=1,
        split_input=supplied,
    ))
    assert refused.disposition is AccountingDisposition.CORRUPT
