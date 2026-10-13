"""A role's local state directory (journal, heartbeat, handovers)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Store:
    """Where a role keeps its local files; SQLite holds every authority."""

    state_dir: str

