"""Read daemon scheduler heartbeats at the filesystem boundary."""

from __future__ import annotations

import json
from datetime import datetime

from ..domain.supervision import LIVE_DAEMON_STATES


def read_heartbeat_time(path: str) -> datetime | None:
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload["state"] not in LIVE_DAEMON_STATES:
            return None
        raw = payload["updated_at"]
        value = datetime.fromisoformat(raw)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value.tzinfo is not None else None

