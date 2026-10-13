from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.backoff import LegEnd, dispatched_kind, machine_backoff_until, machine_backoff_wait

T = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def ends(*machine: bool, kind: AgentKind = AgentKind.FIX_CI) -> tuple[LegEnd, ...]:
    """Oldest first, one minute apart; the last argument is the latest end."""
    return tuple(LegEnd(kind, T + timedelta(minutes=i), m) for i, m in enumerate(machine))


def latest(history: tuple[LegEnd, ...]) -> datetime:
    return max(end.ended_at for end in history)


@pytest.mark.parametrize(("streak", "wait"), [
    (1, timedelta(seconds=60)),
    (2, timedelta(seconds=120)),
    (3, timedelta(seconds=240)),
    (6, timedelta(seconds=1920)),
    (7, timedelta(hours=1)),
    (40, timedelta(hours=1)),
])
def test_wait_doubles_per_consecutive_machine_end_up_to_an_hour(streak, wait):
    history = ends(*([True] * streak))
    assert machine_backoff_until(history, AgentKind.FIX_CI) == latest(history) + wait


@pytest.mark.parametrize(("streak", "wait"), [
    (-5, timedelta(seconds=60)), (0, timedelta(seconds=60)), (1, timedelta(seconds=60)),
    (4, timedelta(seconds=480)), (2**62, timedelta(hours=1)),
])
def test_wait_for_a_stored_streak_count_is_clamped_to_base_and_cap(streak, wait):
    assert machine_backoff_wait(streak) == wait


def test_huge_streak_is_capped_without_overflow():
    history = tuple(LegEnd(AgentKind.RESOLVE, T - timedelta(seconds=i), True) for i in range(10_000))
    assert machine_backoff_until(history, AgentKind.RESOLVE) == T + timedelta(hours=1)


def test_non_machine_end_resets_the_streak():
    assert machine_backoff_until(ends(True, True, True, False), AgentKind.FIX_CI) is None
    history = ends(True, True, False, True)
    assert machine_backoff_until(history, AgentKind.FIX_CI) == latest(history) + timedelta(seconds=60)


def test_order_of_input_does_not_matter():
    history = ends(False, True, True)
    assert machine_backoff_until(tuple(reversed(history)), AgentKind.FIX_CI) == latest(history) + timedelta(seconds=120)


def test_ends_sharing_a_timestamp_count_the_later_recorded_one_as_newest():
    # A refused submission can be recorded in the same millisecond as the paid
    # leg it follows; the recorded order, not chance, decides which is newest.
    paid, refused = LegEnd(AgentKind.REVIEW, T, False), LegEnd(AgentKind.REVIEW, T, True)
    assert machine_backoff_until((paid, refused), AgentKind.REVIEW) == T + timedelta(seconds=60)
    assert machine_backoff_until((refused, paid), AgentKind.REVIEW) is None


def test_other_kinds_neither_wait_nor_reset():
    history = ends(True, True) + ends(False, kind=AgentKind.REBASE)
    assert machine_backoff_until(history, AgentKind.FIX_CI) == latest(ends(True, True)) + timedelta(seconds=120)
    assert machine_backoff_until(history, AgentKind.REBASE) is None


def test_no_history_means_no_wait():
    assert machine_backoff_until((), AgentKind.RESOLVE) is None


@pytest.mark.parametrize("run_kind", ["", "unknown", "resolve", "x" * 10_000])
def test_unknown_run_kinds_are_not_agent_kinds(run_kind):
    assert dispatched_kind(run_kind) is None


@pytest.mark.parametrize(("kind", "ended_at", "machine"), [
    ("FIX_CI", T, True),
    (AgentKind.FIX_CI, datetime(2026, 10, 1), True),
    (AgentKind.FIX_CI, "2026-10-01T00:00:00Z", True),
    (AgentKind.FIX_CI, T, 1),
    (AgentKind.FIX_CI, T, None),
])
def test_malformed_leg_end_is_rejected(kind, ended_at, machine):
    with pytest.raises(ValueError):
        LegEnd(kind, ended_at, machine)
