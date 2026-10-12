from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.backoff import LegEnd, dispatched_kind, machine_backoff_until

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


def test_other_kinds_neither_wait_nor_reset():
    history = ends(True, True) + ends(False, kind=AgentKind.REBASE)
    assert machine_backoff_until(history, AgentKind.FIX_CI) == latest(ends(True, True)) + timedelta(seconds=120)
    assert machine_backoff_until(history, AgentKind.REBASE) is None


def test_no_history_means_no_wait():
    assert machine_backoff_until((), AgentKind.RESOLVE) is None


@pytest.mark.parametrize("run_kind", ["AUTHOR", "IMPLEMENT", "ADJUDICATE", "RESOLVE"])
def test_resolve_family_legs_back_off_the_resolve_dispatch(run_kind):
    assert dispatched_kind(run_kind) is AgentKind.RESOLVE


def test_a_normal_router_leg_before_each_dying_resolve_does_not_reset_the_streak():
    """Routing runs a cheap ROUTE leg before every unpinned RESOLVE. It returns
    normally; only the RESOLVE leg that ended the dispatch counts, so twenty
    consecutive RESOLVE deaths still double the wait up to the cap."""
    history: list[LegEnd] = []
    waits = []
    at = T
    for _ in range(20):
        route = dispatched_kind("ROUTE")
        resolve = dispatched_kind("RESOLVE")
        assert route is not None and resolve is not None
        history.append(LegEnd(route, at + timedelta(seconds=10), False))
        history.append(LegEnd(resolve, at + timedelta(seconds=40), True))
        until = machine_backoff_until(history, AgentKind.RESOLVE)
        assert until is not None
        waits.append(until - (at + timedelta(seconds=40)))
        at = until
    assert waits[:7] == [timedelta(seconds=60 * 2 ** i) for i in range(6)] + [timedelta(hours=1)]
    assert set(waits[7:]) == {timedelta(hours=1)}


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
