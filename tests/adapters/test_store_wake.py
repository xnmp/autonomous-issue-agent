"""Store-change wake: an item of the repository freed in either role ends a
daemon's wait early."""

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
    WAKE_SETTLE_SECONDS,
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


def _claim(store: SQLiteControlStore, item: int, repository: str = REPO, role: str = "fix") -> None:
    assert store.lease(repository, role, item, f"{repository}:{role}")


def _free(store: SQLiteControlStore, item: int, repository: str = REPO, role: str = "fix") -> None:
    assert store.unlease(repository, role, item, f"{repository}:{role}")


def _record_leg(store: SQLiteControlStore, run: str, repository: str = REPO) -> None:
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


def _wake(store, clock, sleeper, logs=None, progress=None) -> StoreWake:
    return StoreWake(store.path, REPO, log=(logs.append if logs is not None else lambda _m: None),
                     progress=progress, sleep=sleeper, monotonic=clock)


def test_an_item_freed_on_another_connection_wakes_the_wait_early(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _free(store, 7)}))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS


def test_without_a_change_the_full_timeout_elapses(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    wake = _wake(store, clock, _Sleeper(clock))

    assert wake.wait(60) is None
    assert clock() == 60


@pytest.mark.parametrize("change", [
    "a claim is taken",
    "another repository frees an item",
    "a leg is recorded while its claim is still held",
])
def test_commits_that_free_nothing_here_do_not_wake(tmp_path, change):
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    _claim(store, 9, repository="other/repo")
    script = {
        "a claim is taken": {1: lambda: _claim(store, 8)},
        "another repository frees an item": {1: lambda: _free(store, 9, repository="other/repo")},
        "a leg is recorded while its claim is still held": {1: lambda: _record_leg(store, "ended")},
    }[change]
    wake = _wake(store, clock, _Sleeper(clock, script))

    assert wake.wait(60) is None
    assert clock() == 60


def test_a_claim_taken_after_the_tick_began_wakes_when_it_is_handed_back(tmp_path):
    # A light leg (STEWARD, TRIAGE) leases and releases inside one wait: the
    # next act in the chain must not wait for the poll.
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _claim(store, 8), 8: lambda: _free(store, 8)}))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == 9


def test_a_burst_wakes_once_after_the_store_goes_quiet(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    for item in (1, 2, 3):
        _claim(store, item)
    burst = {10: lambda: _free(store, 1), 11: lambda: _free(store, 2), 12: lambda: _free(store, 3)}
    wake = _wake(store, clock, _Sleeper(clock, burst))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == 13  # one quiet check after the burst
    assert wake.wait(60) is None  # the burst was spent on one wake


def test_a_burst_inside_the_minimum_gap_wakes_at_the_gap(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    for item in (1, 2, 3):
        _claim(store, item)
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _free(store, 1), 2: lambda: _free(store, 2),
                                                3: lambda: _free(store, 3)}))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS


def test_a_store_that_never_goes_quiet_is_judged_after_the_settle_time(tmp_path):
    # Every deployment of the user shares the store: another repository's
    # commits every second must not starve this one's wake.
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    churn: dict[float, Callable[[], object]] = {t: (lambda t=t: _record_leg(store, f"other-{t}", "other/repo"))
                                                for t in range(1, 60)}
    churn[10.5] = lambda: _free(store, 7)
    wake = _wake(store, clock, _Sleeper(clock, churn))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() <= 11 + WAKE_SETTLE_SECONDS


def test_an_item_freed_while_the_tick_ran_wakes_the_next_wait(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    wake = _wake(store, clock, _Sleeper(clock))
    assert wake.wait(10) is None
    _free(store, 7)  # between waits, as a dispatch finishing during a tick would

    assert wake.wait(60) == STORE_CHANGED


def test_a_change_near_the_deadline_returns_at_the_deadline(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    wake = _wake(store, clock, _Sleeper(clock, {2: lambda: _free(store, 7)}))

    assert wake.wait(3) is None  # shorter than the gap: the timeout ends it
    assert clock() == 3


def test_the_wait_feeds_the_watchdog_every_check(tmp_path):
    store, clock, fed = _store(tmp_path), ManualClock(), []
    wake = _wake(store, clock, _Sleeper(clock), progress=lambda: fed.append(clock()))

    assert wake.wait(10) is None
    assert fed == [float(t) for t in range(1, 11)]


def test_an_unreadable_store_falls_back_to_the_timed_wait_and_logs_once(tmp_path):
    clock, logs, fed = ManualClock(), [], []
    wake = StoreWake(str(tmp_path / "missing" / "control.db"), REPO, log=logs.append, progress=lambda: fed.append(1),
                     sleep=_Sleeper(clock), monotonic=clock)

    assert wake.wait(60) is None
    assert clock() == 60
    assert wake.wait(60) is None
    assert clock() == 120
    assert len(logs) == 1 and "store wake unavailable" in logs[0]
    assert len(fed) == 120  # the watchdog is still fed while falling back


def _rename_leases(store: SQLiteControlStore, old: str, new: str) -> None:
    with closing(sqlite3.connect(store.path, isolation_level=None)) as db:
        db.execute(f"ALTER TABLE {old} RENAME TO {new}")


def test_a_store_failing_mid_wait_finishes_on_the_timer_then_recovers(tmp_path):
    store, clock, logs = _store(tmp_path), ManualClock(), []
    _claim(store, 7)
    sleeper = _Sleeper(clock, {2: lambda: _rename_leases(store, "leases", "leases_away")})
    wake = _wake(store, clock, sleeper, logs)

    assert wake.wait(60) is None
    assert clock() == 60
    assert len(logs) == 1 and "store wake unavailable" in logs[0]

    _rename_leases(store, "leases_away", "leases")
    sleeper.at[61] = lambda: _free(store, 7)
    assert wake.wait(60) == STORE_CHANGED
    assert len(logs) == 1


def test_the_wait_leaves_no_connection_holding_the_wal(tmp_path):
    # A wait closes its connection, so the last close still folds the WAL into
    # the main file: a copy of control.db alone holds every commit.
    store, clock = _store(tmp_path), ManualClock()
    _claim(store, 7)
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: (_record_leg(store, "settled"), _free(store, 7))}))
    assert wake.wait(60) == STORE_CHANGED

    assert not os.path.exists(f"{store.path}-wal")
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro&immutable=1", uri=True)) as copy:
        assert copy.execute("SELECT leg_id FROM legs").fetchall() == [("settled",)]
        assert copy.execute("SELECT count(*) FROM leases").fetchone() == (0,)


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
    assert time.monotonic() - started < 5
