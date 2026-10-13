"""Store-change wake: a leg of the repository ending ends a daemon's wait early."""

from __future__ import annotations

import os
import signal
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import (
    STORE_CHANGED,
    WAKE_MIN_GAP_SECONDS,
    SQLiteControlStore,
    StoreWake,
)
from issue_agent.domain.contracts import RunId
from tests.fakes.manual_clock import ManualClock

REPO = "o/r"


def _store(tmp_path: Path) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    return store


def _end_leg(store: SQLiteControlStore, run: str, repository: str = REPO) -> None:
    store.record_unpaid_leg(RunId(run), repository=repository, role="review", kind="REVIEW",
                            owner_id=f"{repository}:review", issue=1, number=1, head="", runtime="claude")


class _Sleeper:
    """Advances the manual clock; runs the scripted store writes due by then."""

    def __init__(self, clock: ManualClock, at: dict[float, Callable[[], object]] | None = None) -> None:
        self.clock, self.at = clock, dict(at or {})

    def __call__(self, seconds: float) -> None:
        assert seconds > 0
        self.clock.advance(seconds)
        for when in sorted(t for t in self.at if t <= self.clock()):
            self.at.pop(when)()


def _wake(store, clock, sleeper, logs=None) -> StoreWake:
    return StoreWake(store.path, REPO, log=(logs.append if logs is not None else lambda _m: None), sleep=sleeper,
                     monotonic=clock)


def test_a_leg_ending_on_another_connection_wakes_the_wait_early(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _end_leg(store, "ended")}))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS


def test_an_item_freed_by_its_lease_ending_wakes_the_wait(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    assert store.lease(REPO, "fix", 7, f"{REPO}:fix")
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: store.unlease(REPO, "fix", 7, f"{REPO}:fix")}))

    assert wake.wait(60) == STORE_CHANGED


def test_without_a_change_the_full_timeout_elapses(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock))

    assert wake.wait(60) is None
    assert clock() == 60


@pytest.mark.parametrize("change", ["leg starts", "lease taken and handed back", "another repository's leg ends"])
def test_commits_that_free_nothing_here_do_not_wake(tmp_path, change):
    store, clock = _store(tmp_path), ManualClock()
    owner = f"{REPO}:fix"
    script = {
        "leg starts": {1: lambda: store.lease(REPO, "fix", 7, owner)},
        "lease taken and handed back": {1: lambda: store.lease(REPO, "fix", 7, owner),
                                        8: lambda: store.unlease(REPO, "fix", 7, owner)},
        "another repository's leg ends": {1: lambda: _end_leg(store, "elsewhere", "other/repo")},
    }[change]
    wake = _wake(store, clock, _Sleeper(clock, script))

    assert wake.wait(60) is None
    assert clock() == 60


def test_a_burst_wakes_once_after_the_store_goes_quiet(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    burst = {t: (lambda t=t: _end_leg(store, f"leg-{t}")) for t in (10, 11, 12)}
    wake = _wake(store, clock, _Sleeper(clock, burst))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == 13  # one quiet check after the burst, not at its first commit
    assert wake.wait(60) is None  # the burst was spent on one wake


def test_a_burst_inside_the_minimum_gap_wakes_at_the_gap(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    burst = {t: (lambda t=t: _end_leg(store, f"leg-{t}")) for t in (1, 2, 3)}
    wake = _wake(store, clock, _Sleeper(clock, burst))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS


def test_a_leg_ending_while_the_tick_ran_wakes_the_next_wait(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock))
    assert wake.wait(10) is None
    _end_leg(store, "during-tick")  # between waits, as a tick's dispatch would

    assert wake.wait(60) == STORE_CHANGED


def test_a_change_near_the_deadline_returns_at_the_deadline(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {2: lambda: _end_leg(store, "late")}))

    assert wake.wait(3) is None  # shorter than the gap: the timeout ends it
    assert clock() == 3


def test_an_unreadable_store_falls_back_to_the_timed_wait_and_logs_once(tmp_path):
    clock, logs = ManualClock(), []
    wake = StoreWake(str(tmp_path / "missing" / "control.db"), REPO, log=logs.append, sleep=_Sleeper(clock),
                     monotonic=clock)

    assert wake.wait(60) is None
    assert clock() == 60
    assert wake.wait(60) is None
    assert clock() == 120
    assert len(logs) == 1 and "store wake unavailable" in logs[0]


def _rename_leases(store: SQLiteControlStore, old: str, new: str) -> None:
    with closing(sqlite3.connect(store.path, isolation_level=None)) as db:
        db.execute(f"ALTER TABLE {old} RENAME TO {new}")


def test_a_store_failing_mid_wait_finishes_on_the_timer_then_recovers(tmp_path):
    store, clock, logs = _store(tmp_path), ManualClock(), []
    sleeper = _Sleeper(clock, {2: lambda: _rename_leases(store, "leases", "leases_away")})
    wake = _wake(store, clock, sleeper, logs)

    assert wake.wait(60) is None
    assert clock() == 60
    assert len(logs) == 1 and "store wake unavailable" in logs[0]

    _rename_leases(store, "leases_away", "leases")
    sleeper.at[61] = lambda: _end_leg(store, "after-recovery")
    assert wake.wait(60) == STORE_CHANGED
    assert len(logs) == 1


def test_the_wait_leaves_no_connection_holding_the_wal(tmp_path):
    # A wait closes its connection, so the last close still folds the WAL into
    # the main file: a copy of control.db alone holds every committed leg.
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _end_leg(store, "settled")}))
    assert wake.wait(60) == STORE_CHANGED

    assert not os.path.exists(f"{store.path}-wal")
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro&immutable=1", uri=True)) as copy:
        assert copy.execute("SELECT leg_id FROM legs").fetchall() == [("settled",)]


def test_a_shutdown_signal_interrupts_the_wait_promptly(tmp_path):
    # The daemon's SIGTERM handler raises SystemExit in the main thread; the
    # wait's real sleep must let it through instead of finishing the interval.
    store = _store(tmp_path)
    wake = StoreWake(store.path, REPO, log=lambda _m: None)

    def stop(_signum, _frame):
        raise SystemExit(0)

    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    started = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            wake.wait(60)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 2
