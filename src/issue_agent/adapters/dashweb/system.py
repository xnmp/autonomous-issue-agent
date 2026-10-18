"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from ..env_file import source_env_file
from ...domain.dashboard import ROLE_NAMES
from .usage import tail_lines

# The newest journal bytes searched for leg models: a model older than this
# reads as unknown, never guessed.
JOURNAL_TAIL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Beat:
    """One role's `heartbeat.json` (written by `tick.Daemon` every tick):
    when, its scheduler state, the dispatches it holds as (kind, number), and
    whether a hot reload is draining them (absent or malformed: not)."""

    at: datetime
    state: str
    in_flight: tuple[tuple[str, int], ...] = ()
    reload_pending: bool = False


def read_beat(path: str) -> Beat | None:
    """A heartbeat, or None when absent, unreadable or naive-stamped."""
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        at = datetime.fromisoformat(payload["updated_at"])
        state = payload["state"]
        rows = payload.get("in_flight") or []
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if at.tzinfo is None or not isinstance(state, str) or not isinstance(rows, list):
        return None
    flight = tuple((str(row["kind"]), row["number"]) for row in rows
                   if isinstance(row, dict) and isinstance(row.get("kind"), str) and type(row.get("number")) is int)
    return Beat(at, state, flight, payload.get("reload_pending") is True)


def role_beats(state_dirs: Mapping[str, str]) -> dict[str, Beat | None]:
    return {
        role: read_beat(os.path.join(state_dirs[role], "heartbeat.json")) if state_dirs.get(role) else None
        for role in ROLE_NAMES
    }


def leg_models(state_dirs: Mapping[str, str], tail_bytes: int = JOURNAL_TAIL_BYTES) -> dict[str, str]:
    """`legs.leg_id` -> model from each role journal's `agent_run` events
    (the legs table records the runtime, not the model). The table's leg id
    is the journal's `run_id`; the journal's own `leg_id` is another id."""
    models: dict[str, str] = {}
    for role in ROLE_NAMES:
        directory = state_dirs.get(role)
        if directory:
            models.update(_journal_models(os.path.join(directory, "journal.jsonl"), tail_bytes))
    return models


# path -> ((size, mtime_ns, tail_bytes), models): an issue tab polls every 5s
# and the journal tail is megabytes; re-read only when the file changed.
_MODEL_CACHE: dict[str, tuple[tuple[int, int, int], dict[str, str]]] = {}


def _journal_models(path: str, tail_bytes: int) -> dict[str, str]:
    """One journal's run_id -> model. A torn or foreign line is skipped."""
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = (stat.st_size, stat.st_mtime_ns, tail_bytes)
    cached = _MODEL_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        lines = tail_lines(path, tail_bytes)
    except OSError:
        return {}
    models: dict[str, str] = {}
    for line in lines:
        if '"agent_run"' not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("event") != "agent_run":
            continue
        leg, model = event.get("run_id"), event.get("model")
        if isinstance(leg, str) and leg and isinstance(model, str) and model:
            models[leg] = model
    _MODEL_CACHE[path] = (key, models)
    return models


def _environ_agent_env(blob: bytes) -> str:
    for item in blob.split(b"\0"):
        if item.startswith(b"AGENT_ENV="):
            return item[len(b"AGENT_ENV=") :].decode(errors="replace")
    return ""


def deployment_of_env(agent_env: str) -> str:
    """Deployment name from a daemon's AGENT_ENV path, "" when not a daemon.

    Two launch layouts exist: `point-at` composes `config/.active.<name>.env`,
    and the systemd/launchd units (HARNESS.md service install) point at
    `~/.config/issue-agent/<name>.env`. The second arm requires the parent
    directory to be literally `issue-agent` so repo overlays
    (`config/repos/<name>.env`) and unrelated `.env` files never read as live
    daemons. Missing the second arm made every systemd deployment show
    `active: false` on the dashboard (keifu, observed 2026-08-02)."""
    base = os.path.basename(agent_env or "")
    if base.startswith(".active.") and base.endswith(".env"):
        return base[len(".active.") : -len(".env")]
    if (
        base.endswith(".env")
        and not base.startswith(".")
        and os.path.basename(os.path.dirname(agent_env or "")) == "issue-agent"
    ):
        return base[: -len(".env")]
    return ""


def scan_proc_daemons(proc_root: str = "/proc") -> tuple[dict[str, set[str]], bool]:
    """Return live roles by deployment and whether foreign processes were readable."""
    found: dict[str, set[str]] = {}
    read_foreign = False
    own_pid = str(os.getpid())
    try:
        pids = [entry for entry in os.listdir(proc_root) if entry.isdigit()]
    except OSError:
        return {}, False
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "environ"), "rb") as handle:
                environ = handle.read()
        except OSError:
            continue
        if pid != own_pid:
            read_foreign = True
        name = deployment_of_env(_environ_agent_env(environ))
        if not name:
            continue
        roles = found.setdefault(name, set())
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as handle:
                argv = handle.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        roles.update(argument for argument in argv if argument in ROLE_NAMES)
    return found, read_foreign
