"""Read daemon scheduler heartbeats at the filesystem boundary."""

from __future__ import annotations

import json
from datetime import datetime


def read_heartbeat_time(path: str) -> datetime | None:
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload["state"] not in {"running", "draining"}:
            return None
        raw = payload["updated_at"]
        value = datetime.fromisoformat(raw)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value.tzinfo is not None else None


def heartbeat_liveness(path: str, *, now: datetime, stale_after_s: float) -> str | None:
    """Fail-closed liveness for fencing decisions.

    None only when there is no heartbeat, the daemon recorded a non-running
    state, or the running stamp is stale. An existing heartbeat that cannot be
    read or parsed is a reason, never evidence that the daemon stopped.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        return f"unreadable heartbeat {path}: {exc}"
    try:
        state = payload["state"]
        stamp = datetime.fromisoformat(payload["updated_at"])
    except (KeyError, TypeError, ValueError) as exc:
        return f"corrupt heartbeat {path}: {exc}"
    if not isinstance(state, str) or stamp.tzinfo is None:
        return f"corrupt heartbeat {path}"
    if state not in {"running", "draining"}:
        return None
    age = (now - stamp).total_seconds()
    if age > stale_after_s:
        return None
    return f"fresh {state} heartbeat {path}"
