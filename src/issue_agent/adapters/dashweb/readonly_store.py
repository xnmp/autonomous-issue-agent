"""The dashboard's read-only view of the control store.

It lives on the dashboard side so the core never carries a consumer-specific
store variant; it reuses the core store's read queries and authority check."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from ..control_store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    ControlStoreError,
    SQLiteControlStore,
    _check_authority,
)


class ReadOnlyControlStore(SQLiteControlStore):
    """The store's reads over one `mode=ro`, `query_only` connection, for a
    consumer that must never write the authority (the dashboard): no
    journal-mode or synchronous pragma, no checkpoint, no creation, no
    cutover. Opening refuses a missing, legacy or damaged file. Every write
    path raises. Use as a context manager; the connection closes on exit."""

    def __init__(self, path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        super().__init__(path, busy_timeout_ms=busy_timeout_ms)
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "ReadOnlyControlStore":
        if not os.path.isfile(self.path):
            raise ControlStoreError(f"control store does not exist: {self.path}")
        try:
            db = sqlite3.connect(f"{Path(self.path).as_uri()}?mode=ro", uri=True,
                                 timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        except sqlite3.Error as exc:
            raise ControlStoreError(f"control store could not be opened read-only: {exc}") from exc
        try:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            db.execute("PRAGMA query_only=ON")
            _check_authority(db)
        except BaseException:
            db.close()
            raise
        self._db = db
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def initialize(self) -> None:
        raise ControlStoreError("a read-only control store cannot be initialized")

    def _connect(self) -> sqlite3.Connection:
        raise ControlStoreError("a read-only control store cannot write")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._db is None:
            raise ControlStoreError("the read-only control store is not open")
        yield self._db

    def query(self, sql: str, parameters: Iterable[object] = ()) -> list[sqlite3.Row]:
        """Rows of one SELECT (the connection is `query_only`)."""
        with self._read() as db:
            return db.execute(sql, tuple(parameters)).fetchall()
