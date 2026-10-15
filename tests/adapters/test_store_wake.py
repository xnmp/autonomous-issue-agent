"""Store wake: an item of the repository that the other role frees is reported
to the daemon's wait."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore, StoreWake
from issue_agent.domain.contracts import RunId

REPO = "o/r"
OTHER = "review"  # the role whose releases wake a fix daemon


def _store(tmp_path: Path) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"))
    store.initialize()
    return store


def _claim(store: SQLiteControlStore, item: int, repository: str = REPO, role: str = OTHER) -> None:
    assert store.lease(repository, role, item, f"{repository}:{role}")


def _free(store: SQLiteControlStore, item: int, repository: str = REPO, role: str = OTHER) -> None:
    assert store.unlease(repository, role, item, f"{repository}:{role}")


def _record_leg(store: SQLiteControlStore, run: str, repository: str = REPO) -> None:
    store.record_unpaid_leg(RunId(run), repository=repository, role="review", kind="REVIEW",
                            owner_id=f"{repository}:review", issue=1, number=1, head="", runtime="claude")


def _wake(store: SQLiteControlStore, logs: list[str] | None = None) -> StoreWake:
    return StoreWake(store.path, REPO, "fix", log=(logs.append if logs is not None else lambda _m: None))


def test_an_item_the_other_role_frees_on_another_connection_is_reported(tmp_path):
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)

    with wake.watch() as freed:
        assert not freed()
        _free(store, 7)
        assert freed()


def test_without_a_change_nothing_is_reported_however_often_it_is_probed(tmp_path):
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)

    with wake.watch() as freed:
        assert not any(freed() for _ in range(1000))


@pytest.mark.parametrize("change", [
    "a claim is taken",
    "another repository frees an item",
    "a leg is recorded while its claim is still held",
    "this daemon's own role frees an item",
])
def test_commits_that_free_nothing_of_the_other_role_here_report_nothing(tmp_path, change):
    # The daemon's own release happens inside its dispatch, before the slot
    # leaves its pool: it wakes on the pool, never on the store.
    store = _store(tmp_path)
    _claim(store, 7)
    _claim(store, 8, role="fix")
    _claim(store, 9, repository="other/repo")
    act = {
        "a claim is taken": lambda: _claim(store, 10),
        "another repository frees an item": lambda: _free(store, 9, repository="other/repo"),
        "a leg is recorded while its claim is still held": lambda: _record_leg(store, "ended"),
        "this daemon's own role frees an item": lambda: _free(store, 8, role="fix"),
    }[change]
    wake = _wake(store)

    with wake.watch() as freed:
        assert not freed()
        act()
        assert not freed()


def test_a_claim_taken_after_the_tick_began_is_reported_when_handed_back(tmp_path):
    # A light leg (STEWARD, TRIAGE) of the other role leases and releases
    # inside one wait: the next act in the chain must not wait for the poll.
    store = _store(tmp_path)
    wake = _wake(store)

    with wake.watch() as freed:
        assert not freed()
        _claim(store, 8)
        assert not freed()
        _free(store, 8)
        assert freed()


def test_an_item_freed_on_a_busy_store_is_reported_at_once(tmp_path):
    # Every deployment of the user shares the store. The release is the last
    # write of its leg, so nothing waits for the store to go quiet: another
    # repository's commits on every probe neither starve nor delay it.
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)

    with wake.watch() as freed:
        for t in range(10):
            _record_leg(store, f"other-{t}", "other/repo")
            assert not freed()
        _free(store, 7)
        _record_leg(store, "other-after", "other/repo")
        assert freed()


def test_a_burst_of_releases_is_spent_on_one_wait(tmp_path):
    store = _store(tmp_path)
    for item in (1, 2, 3):
        _claim(store, item)
    wake = _wake(store)

    with wake.watch() as freed:
        _free(store, 1)
        _free(store, 2)
        assert freed()
        _free(store, 3)
    with wake.watch() as freed:  # the tick that ran in between saw all three free
        assert not freed()


def test_an_item_freed_while_the_tick_ran_is_reported_on_the_next_wait(tmp_path):
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)
    with wake.watch() as freed:
        assert not freed()
    _free(store, 7)  # between waits, as a leg finishing during a tick would

    with wake.watch() as freed:
        assert freed()


def test_an_unreadable_store_reports_nothing_and_logs_once(tmp_path):
    logs: list[str] = []
    wake = StoreWake(str(tmp_path / "missing" / "control.db"), REPO, "fix", log=logs.append)

    for _ in range(2):
        with wake.watch() as freed:
            assert not freed()
    assert len(logs) == 1 and "store wake unavailable" in logs[0]


def _rename_leases(store: SQLiteControlStore, old: str, new: str) -> None:
    with closing(sqlite3.connect(store.path, isolation_level=None)) as db:
        db.execute(f"ALTER TABLE {old} RENAME TO {new}")


def test_a_store_failing_mid_wait_reports_nothing_then_recovers(tmp_path):
    store, logs = _store(tmp_path), []
    _claim(store, 7)
    wake = _wake(store, logs)

    with wake.watch() as freed:
        assert not freed()
        _rename_leases(store, "leases", "leases_away")
        assert not freed()
        assert not freed()
    assert len(logs) == 1 and "store wake unavailable" in logs[0]

    _rename_leases(store, "leases_away", "leases")
    with wake.watch() as freed:
        assert not freed()
        _free(store, 7)
        assert freed()
    assert len(logs) == 1


def test_a_wait_leaves_no_connection_holding_the_wal(tmp_path):
    # A wait closes its connection, so the last close still folds the WAL into
    # the main file: a copy of control.db alone holds every commit.
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)
    with wake.watch() as freed:
        _record_leg(store, "settled")
        _free(store, 7)
        assert freed()

    assert not os.path.exists(f"{store.path}-wal")
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro&immutable=1", uri=True)) as copy:
        assert copy.execute("SELECT leg_id FROM legs").fetchall() == [("settled",)]
        assert copy.execute("SELECT count(*) FROM leases").fetchone() == (0,)


def test_a_wait_interrupted_by_shutdown_still_closes_its_connection(tmp_path):
    store = _store(tmp_path)
    _claim(store, 7)
    wake = _wake(store)
    with pytest.raises(SystemExit):
        with wake.watch():
            _free(store, 7)
            raise SystemExit(0)

    assert not os.path.exists(f"{store.path}-wal")
    with wake.watch() as freed:  # rebaselined on the way out: the next tick sees the release
        assert not freed()
