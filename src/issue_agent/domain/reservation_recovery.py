"""Pure decision model for recovering durable budget reservations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class LeaseEvidence(str, Enum):
    LIVE = "live"
    ABSENT = "absent"
    RECLAIMED = "reclaimed"


class SpendEvidence(str, Enum):
    UNCHECKED = "unchecked"
    SETTLED = "settled"
    INDETERMINATE = "indeterminate"


class ReleaseEvidence(str, Enum):
    UNCHECKED = "unchecked"
    RELEASED = "released"
    INELIGIBLE = "ineligible"


class ReservationRecoveryAction(str, Enum):
    PRESERVE = "preserve"
    RECONCILE_SPEND = "reconcile_spend"
    RELEASE_UNLAUNCHED = "release_unlaunched"
    RELEASE_RETAINED = "release_retained"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ReservationRecoveryState:
    """Evidence accumulated while recovering one stale reservation."""

    lease: LeaseEvidence
    spend: SpendEvidence = SpendEvidence.UNCHECKED
    unlaunched_release: ReleaseEvidence = ReleaseEvidence.UNCHECKED
    retained_release: ReleaseEvidence = ReleaseEvidence.UNCHECKED

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lease, LeaseEvidence)
            or not isinstance(self.spend, SpendEvidence)
            or not isinstance(self.unlaunched_release, ReleaseEvidence)
            or not isinstance(self.retained_release, ReleaseEvidence)
        ):
            raise TypeError("reservation recovery evidence must use enums")
        releases_unchecked = (
            self.unlaunched_release is ReleaseEvidence.UNCHECKED
            and self.retained_release is ReleaseEvidence.UNCHECKED
        )
        if self.lease is LeaseEvidence.LIVE and (
            self.spend is not SpendEvidence.UNCHECKED
            or not releases_unchecked
        ):
            raise ValueError(
                "live reservations cannot accumulate release evidence"
            )
        if (
            self.spend is not SpendEvidence.INDETERMINATE
            and not releases_unchecked
        ):
            raise ValueError("release evidence requires indeterminate spend")
        if (
            self.retained_release is not ReleaseEvidence.UNCHECKED
            and self.unlaunched_release is not ReleaseEvidence.INELIGIBLE
        ):
            raise ValueError(
                "retained release requires an ineligible unlaunched release"
            )

    @classmethod
    def observed(
        cls,
        *,
        live_lease: bool,
        lease_reclaimed: bool,
    ) -> ReservationRecoveryState:
        if live_lease and lease_reclaimed:
            raise ValueError("a lease cannot be both live and reclaimed")
        lease = (
            LeaseEvidence.RECLAIMED
            if lease_reclaimed
            else LeaseEvidence.LIVE
            if live_lease
            else LeaseEvidence.ABSENT
        )
        return cls(lease)

    def after_spend_reconciliation(
        self,
        *,
        settled: bool,
    ) -> ReservationRecoveryState:
        if self.spend is not SpendEvidence.UNCHECKED:
            raise ValueError("spend reconciliation was already observed")
        return replace(
            self,
            spend=(
                SpendEvidence.SETTLED
                if settled
                else SpendEvidence.INDETERMINATE
            ),
        )

    def after_unlaunched_release(
        self,
        *,
        released: bool,
    ) -> ReservationRecoveryState:
        if (
            self.spend is not SpendEvidence.INDETERMINATE
            or self.unlaunched_release is not ReleaseEvidence.UNCHECKED
        ):
            raise ValueError("unlaunched release is out of recovery order")
        return replace(
            self,
            unlaunched_release=(
                ReleaseEvidence.RELEASED
                if released
                else ReleaseEvidence.INELIGIBLE
            ),
        )

    def after_retained_release(
        self,
        *,
        released: bool,
    ) -> ReservationRecoveryState:
        if (
            self.unlaunched_release is not ReleaseEvidence.INELIGIBLE
            or self.retained_release is not ReleaseEvidence.UNCHECKED
        ):
            raise ValueError("retained release is out of recovery order")
        return replace(
            self,
            retained_release=(
                ReleaseEvidence.RELEASED
                if released
                else ReleaseEvidence.INELIGIBLE
            ),
        )


@dataclass(frozen=True)
class ReservationRecoveryDecision:
    action: ReservationRecoveryAction
    report_spend_unknown: bool = False


def decide_reservation_recovery(
    state: ReservationRecoveryState,
) -> ReservationRecoveryDecision:
    """Choose the next safe effect from accumulated durable evidence."""

    if state.lease is LeaseEvidence.LIVE:
        return ReservationRecoveryDecision(ReservationRecoveryAction.PRESERVE)
    if state.spend is SpendEvidence.UNCHECKED:
        return ReservationRecoveryDecision(
            ReservationRecoveryAction.RECONCILE_SPEND
        )
    if state.spend is SpendEvidence.SETTLED:
        return ReservationRecoveryDecision(ReservationRecoveryAction.COMPLETE)
    if state.unlaunched_release is ReleaseEvidence.UNCHECKED:
        return ReservationRecoveryDecision(
            ReservationRecoveryAction.RELEASE_UNLAUNCHED
        )
    if state.unlaunched_release is ReleaseEvidence.RELEASED:
        return ReservationRecoveryDecision(
            ReservationRecoveryAction.COMPLETE,
            report_spend_unknown=True,
        )
    if state.retained_release is ReleaseEvidence.UNCHECKED:
        return ReservationRecoveryDecision(
            ReservationRecoveryAction.RELEASE_RETAINED
        )
    return ReservationRecoveryDecision(
        ReservationRecoveryAction.COMPLETE,
        report_spend_unknown=(
            state.retained_release is ReleaseEvidence.RELEASED
        ),
    )
