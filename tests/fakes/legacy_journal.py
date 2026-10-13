"""Legacy JSONL writer used only by compatibility-reader tests."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone



def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Journal:
    """Test fixture for producing historical JSONL consumed by read-only tools."""

    path: str
    canonical: bool = False

    def emit(self, event: str, fields: dict | None = None, **extra: object) -> None:
        try:
            line = json.dumps(
                {"ts": _stamp(), "event": event, **(fields or {}), **extra},
                default=str,
                sort_keys=True,
            )
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except Exception as exc:  # telemetry fixtures preserve fail-soft behavior
            sys.stderr.write(f"journal: dropped {event!r} event ({exc})\n")
