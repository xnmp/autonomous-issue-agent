from typing import Any, cast

import pytest

from issue_agent.domain.reservation_recovery import (
    ReservationRecoveryAction,
    ReservationRecoveryState,
    decide_reservation_recovery,
)


def _action(state: ReservationRecoveryState) -> ReservationRecoveryAction:
    return decide_reservation_recovery(state).action


def test_live_lease_is_preserved_without_reconciliation() -> None:
    state = ReservationRecoveryState.observed(
        live_lease=True,
        lease_reclaimed=False,
    )

    assert _action(state) is ReservationRecoveryAction.PRESERVE


def test_unowned_reservation_reconciles_before_any_release() -> None:
    state = ReservationRecoveryState.observed(
        live_lease=False,
        lease_reclaimed=False,
    )

    assert _action(state) is ReservationRecoveryAction.RECONCILE_SPEND
    state = state.after_spend_reconciliation(settled=False)
    assert _action(state) is ReservationRecoveryAction.RELEASE_UNLAUNCHED


def test_unlaunched_release_reports_unknown_spend_and_abandonment() -> None:
    state = ReservationRecoveryState.observed(
        live_lease=False,
        lease_reclaimed=False,
    ).after_spend_reconciliation(settled=False)
    state = state.after_unlaunched_release(released=True)

    decision = decide_reservation_recovery(state)

    assert decision.action is ReservationRecoveryAction.COMPLETE
    assert decision.report_spend_unknown is True
    assert decision.record_abandonment is True


def test_retained_release_is_tried_only_after_unlaunched_conflict() -> None:
    state = ReservationRecoveryState.observed(
        live_lease=False,
        lease_reclaimed=False,
    ).after_spend_reconciliation(settled=False)
    state = state.after_unlaunched_release(released=False)

    assert _action(state) is ReservationRecoveryAction.RELEASE_RETAINED

    decision = decide_reservation_recovery(
        state.after_retained_release(released=True)
    )
    assert decision.action is ReservationRecoveryAction.COMPLETE
    assert decision.report_spend_unknown is True
    assert decision.record_abandonment is False


def test_reclaimed_settled_run_records_only_abandonment() -> None:
    state = ReservationRecoveryState.observed(
        live_lease=False,
        lease_reclaimed=True,
    ).after_spend_reconciliation(settled=True)

    decision = decide_reservation_recovery(state)

    assert decision.action is ReservationRecoveryAction.COMPLETE
    assert decision.report_spend_unknown is False
    assert decision.record_abandonment is True


def test_impossible_evidence_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="both live and reclaimed"):
        ReservationRecoveryState.observed(
            live_lease=True,
            lease_reclaimed=True,
        )

    state = ReservationRecoveryState.observed(
        live_lease=False,
        lease_reclaimed=False,
    )
    with pytest.raises(ValueError, match="out of recovery order"):
        state.after_retained_release(released=True)

    with pytest.raises(TypeError, match="evidence must use enums"):
        ReservationRecoveryState(cast(Any, "live"))
