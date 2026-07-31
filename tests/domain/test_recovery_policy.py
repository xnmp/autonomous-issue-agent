import pytest

from issue_agent.domain.recovery import (
    RecoveryArm,
    RecoveryGrace,
    RecoveryPlan,
    RecoveryTrigger,
    recovery_plan,
)


def test_every_recovery_trigger_has_the_expected_plan():
    common = frozenset(
        {
            RecoveryArm.RECLAIM_LEASES,
            RecoveryArm.VALIDATE_WORKTREES,
            RecoveryArm.DRAIN_COMMANDS,
            RecoveryArm.SWEEP_RESERVATIONS,
        }
    )

    assert set(RecoveryTrigger) == {
        RecoveryTrigger.BOOT,
        RecoveryTrigger.WAKE,
        RecoveryTrigger.PERIODIC,
    }
    assert recovery_plan(RecoveryTrigger.BOOT) == RecoveryPlan(
        frozenset({RecoveryArm.REAP_ALL_PROCESSES}),
        common,
        RecoveryGrace.IMMEDIATE,
    )
    assert recovery_plan(RecoveryTrigger.WAKE) == RecoveryPlan(
        frozenset(),
        common | {RecoveryArm.REAP_ABANDONED_PROCESSES},
        RecoveryGrace.STALE,
    )
    assert recovery_plan(RecoveryTrigger.PERIODIC) == RecoveryPlan(
        frozenset(),
        frozenset(
            {
                RecoveryArm.RECLAIM_LEASES,
                RecoveryArm.SWEEP_RESERVATIONS,
            }
        ),
        RecoveryGrace.STALE,
    )


@pytest.mark.parametrize(
    ("preflight", "coordinator", "grace", "message"),
    (
        (
            frozenset({RecoveryArm.REAP_ABANDONED_PROCESSES}),
            frozenset(),
            None,
            "preflight may only reap all",
        ),
        (
            frozenset({RecoveryArm.REAP_ALL_PROCESSES}),
            frozenset({RecoveryArm.REAP_ABANDONED_PROCESSES}),
            None,
            "two process reapers",
        ),
        (
            frozenset(),
            frozenset({RecoveryArm.SWEEP_RESERVATIONS}),
            RecoveryGrace.STALE,
            "requires lease reclamation",
        ),
        (
            frozenset(),
            frozenset({RecoveryArm.RECLAIM_LEASES}),
            RecoveryGrace.STALE,
            "sweep and grace",
        ),
    ),
)
def test_invalid_recovery_plans_are_rejected(
    preflight, coordinator, grace, message
):
    with pytest.raises(ValueError, match=message):
        RecoveryPlan(preflight, coordinator, grace)
