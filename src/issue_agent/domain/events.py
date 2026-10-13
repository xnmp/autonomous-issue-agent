"""The JSON value shape journal lines carry (ADR 0004).

Journal records are plain dicts written by `adapters/journal.py`: telemetry
only, never read back to authorize an effect, reserve spend, or deduplicate.
SQLite remains the only authority.
"""

from __future__ import annotations

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
