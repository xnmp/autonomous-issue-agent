"""Completed control-store operations leave authority files physically stable."""

from contextlib import closing
import gc
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.domain.contracts import RunId


def _authority_bytes(store: SQLiteControlStore) -> bytes:
    return Path(store.path).read_bytes()


def _backup_and_collect(store: SQLiteControlStore, copy: Path) -> None:
    before = _authority_bytes(store)
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(copy)) as target:
            source.backup(target)
    gc.collect()
    assert _authority_bytes(store) == before


def _unpaid(store: SQLiteControlStore, run: str) -> None:
    store.record_unpaid_leg(
        RunId(run), repository="o/r", role="fix", kind="RESOLVE", owner_id="o/r:fix",
        issue=1, number=1, head="", runtime="claude",
    )


def test_completed_control_operations_keep_original_bytes_stable(tmp_path: Path) -> None:
    # Delayed connection finalization can checkpoint committed WAL pages into
    # the original database during a read-only rehearsal backup.
    automatic_gc = gc.isenabled()
    gc.disable()
    try:
        store = SQLiteControlStore(str(tmp_path / "authority" / "control.db"))
        store.initialize()
        _backup_and_collect(store, tmp_path / "initialized-copy.db")

        _unpaid(store, "committed")
        _backup_and_collect(store, tmp_path / "committed-copy.db")
        with closing(sqlite3.connect(tmp_path / "committed-copy.db")) as copied:
            assert copied.execute("SELECT leg_id FROM legs").fetchall() == [("committed",)]

        with pytest.raises(sqlite3.IntegrityError):
            _unpaid(store, "committed")  # a duplicate leg rolls its transaction back
        _backup_and_collect(store, tmp_path / "rolled-back-copy.db")
        with closing(sqlite3.connect(tmp_path / "rolled-back-copy.db")) as copied:
            assert copied.execute("SELECT leg_id FROM legs").fetchall() == [("committed",)]
    finally:
        if automatic_gc:
            gc.enable()
