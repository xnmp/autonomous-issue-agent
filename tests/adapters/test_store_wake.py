"""Store-change wake: a commit to the control store ends a daemon's wait early."""

from __future__ import annotations

import signal
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from issue_agent.adapters import control_store as control_store_module
from issue_agent.adapters.control_store import (
    STORE_CHANGED,
    WAKE_MIN_GAP_SECONDS,
    SQLiteControlStore,
    StoreWake,
)
from issue_agent.domain.contracts import RunId
from tests.fakes.manual_clock import ManualClock


def _store(tmp_path: Path) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    return store


def _commit(store: SQLiteControlStore, run: str) -> None:
    store.record_unpaid_leg(RunId(run), repository="o/r", role="review", kind="REVIEW", owner_id="o/r:review",
                            issue=1, number=1, head="", runtime="claude")


class _Sleeper:
    """Advances the manual clock; runs a scripted commit at given clock times."""

    def __init__(self, clock: ManualClock, at: dict[float, Callable[[], object]] | None = None) -> None:
        self.clock, self.at = clock, dict(at or {})

    def __call__(self, seconds: float) -> None:
        assert seconds > 0
        self.clock.advance(seconds)
        for when in sorted(t for t in self.at if t <= self.clock()):
            self.at.pop(when)()


def _wake(store, clock, sleeper, logs=None) -> StoreWake:
    return StoreWake(store.path, log=(logs.append if logs is not None else lambda _m: None), sleep=sleeper,
                     monotonic=clock)


def test_a_commit_from_another_connection_wakes_the_wait_early(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {1: lambda: _commit(store, "ended")}))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS


def test_without_a_change_the_full_timeout_elapses(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock))

    assert wake.wait(60) is None
    assert clock() == 60


def test_a_burst_of_commits_is_one_wake_after_the_minimum_gap(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    burst = {t: (lambda t=t: _commit(store, f"leg-{t}")) for t in (1, 2, 3, 4)}
    wake = _wake(store, clock, _Sleeper(clock, burst))

    assert wake.wait(60) == STORE_CHANGED
    assert clock() == WAKE_MIN_GAP_SECONDS
    assert wake.wait(60) is None  # the burst was spent on one wake
    assert clock() == WAKE_MIN_GAP_SECONDS + 60


def test_a_commit_while_the_tick_ran_wakes_the_next_wait(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock))
    _commit(store, "during-tick")  # between waits, as a tick would

    assert wake.wait(60) == STORE_CHANGED


def test_a_change_near_the_deadline_returns_at_the_deadline(tmp_path):
    store, clock = _store(tmp_path), ManualClock()
    wake = _wake(store, clock, _Sleeper(clock, {2: lambda: _commit(store, "late")}))

    assert wake.wait(3) is None  # shorter than the gap: the timeout ends it
    assert clock() == 3


def test_an_unreadable_store_falls_back_to_the_timed_wait_and_logs_once(tmp_path):
    clock, logs = ManualClock(), []
    wake = StoreWake(str(tmp_path / "missing" / "control.db"), log=logs.append, sleep=_Sleeper(clock),
                     monotonic=clock)

    assert wake.wait(60) is None
    assert clock() == 60
    assert wake.wait(60) is None
    assert clock() == 120
    assert len(logs) == 1 and "store wake unavailable" in logs[0]


class _FlakyConnection:
    """A read-only connection whose reads fail while `failing` holds."""

    failing = False

    def __init__(self, real: sqlite3.Connection) -> None:
        self.real = real

    def execute(self, sql: str):
        if _FlakyConnection.failing:
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql)

    def close(self) -> None:
        self.real.close()


def test_a_read_failing_mid_wait_finishes_on_the_timer_then_recovers(tmp_path, monkeypatch):
    store, clock, logs = _store(tmp_path), ManualClock(), []
    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        connection = real_connect(target, *args, **kwargs)
        return _FlakyConnection(connection) if "mode=ro" in str(target) else connection

    monkeypatch.setattr(control_store_module.sqlite3, "connect", connect)
    monkeypatch.setattr(_FlakyConnection, "failing", False)

    def fail() -> None:
        _FlakyConnection.failing = True

    sleeper = _Sleeper(clock, {2: fail})
    wake = _wake(store, clock, sleeper, logs)

    assert wake.wait(60) is None
    assert clock() == 60
    assert len(logs) == 1 and "disk I/O error" in logs[0]

    _FlakyConnection.failing = False
    assert wake.wait(60) is None  # reconnects; its first read is a new baseline
    sleeper.at[121] = lambda: _commit(store, "after-recovery")
    assert wake.wait(60) == STORE_CHANGED
    assert len(logs) == 1


def test_a_shutdown_signal_interrupts_the_wait_promptly(tmp_path):
    # The daemon's SIGTERM handler raises SystemExit in the main thread; the
    # wait's real sleep must let it through instead of finishing the interval.
    store = _store(tmp_path)
    wake = StoreWake(store.path, log=lambda _m: None)

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
