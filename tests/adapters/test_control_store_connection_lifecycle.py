"""Completed control-store operations leave authority files physically stable."""

from contextlib import closing
import gc
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore


def _authority_bytes(store: SQLiteControlStore) -> tuple[bytes, bytes]:
    return (Path(store.path).read_bytes(), Path(store.authority_witness_path).read_bytes())


def _backup_and_collect(store: SQLiteControlStore, copy: Path) -> None:
    before = _authority_bytes(store)
    with closing(sqlite3.connect(f"{Path(store.path).as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(copy)) as target:
            source.backup(target)
    gc.collect()
    assert _authority_bytes(store) == before


def test_completed_control_operations_keep_original_bytes_stable(tmp_path: Path) -> None:
    # Delayed connection finalization can checkpoint committed WAL pages into
    # the original database during a read-only rehearsal backup.
    automatic_gc = gc.isenabled()
    gc.disable()
    try:
        store = SQLiteControlStore(str(tmp_path / "authority" / "control.db"))
        store.initialize()
        _backup_and_collect(store, tmp_path / "initialized-copy.db")

        with store.transaction() as db:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,?,?,?,?)",
                ("connection-lifecycle", "test", "committed", "2026-09-30T00:00:00+00:00", "{}"),
            )
        _backup_and_collect(store, tmp_path / "committed-copy.db")
        with closing(sqlite3.connect(tmp_path / "committed-copy.db")) as copied:
            assert copied.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='connection-lifecycle'"
            ).fetchone() == ("{}",)

        with pytest.raises(RuntimeError, match="rollback stimulus"):
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                    "VALUES (?,?,?,?,?)",
                    ("connection-lifecycle", "test", "rolled-back", "2026-09-30T00:00:00+00:00", "{}"),
                )
                raise RuntimeError("rollback stimulus")
        _backup_and_collect(store, tmp_path / "rolled-back-copy.db")
        with closing(sqlite3.connect(tmp_path / "rolled-back-copy.db")) as copied:
            assert copied.execute(
                "SELECT key FROM dedupe_keys WHERE namespace='connection-lifecycle'"
            ).fetchall() == [("committed",)]
    finally:
        if automatic_gc:
            gc.enable()
