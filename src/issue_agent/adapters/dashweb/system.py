"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from glob import glob

from ..env_file import source_env_file
from ...domain.dashboard import ROLE_NAMES
from .usage import tail_lines

# The newest journal bytes searched for leg models: a model older than this
# reads as unknown, never guessed.
JOURNAL_TAIL_BYTES = 4 * 1024 * 1024


def deployment_names(repos_dir: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            os.path.splitext(os.path.basename(path))[0]
            for path in glob(os.path.join(repos_dir, "*.env"))
        )
    )


def deployment_env(harness_root: str, name: str, inherited: Mapping[str, str]) -> dict[str, str]:
    """The environment a deployment's daemons were configured from.

    The launch snapshot `config/.active.<name>.env` is what the daemons loaded,
    so it is named as AGENT_ENV and `config.from_env` reads it the daemons'
    way (the file is authoritative). A deployment never launched has no
    snapshot: shared.env then its overlay are sourced, as a launch would
    compose them."""
    config = os.path.join(harness_root, "config")
    active = os.path.join(config, f".active.{name}.env")
    if os.access(active, os.R_OK):
        return {**inherited, "AGENT_ENV": active}
    env = {key: value for key, value in inherited.items() if key != "AGENT_ENV"}
    for path in (os.path.join(config, "shared.env"), os.path.join(config, "repos", f"{name}.env")):
        if os.access(path, os.R_OK):
            env.update(source_env_file(path, env, assigned_only=True))
    return env


@dataclass(frozen=True)
class Beat:
    """One role's `heartbeat.json` (written by `tick.Daemon` every tick):
    when, its scheduler state, and the dispatches it holds as (kind, number)."""

    at: datetime
    state: str
    in_flight: tuple[tuple[str, int], ...] = ()


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
    return Beat(at, state, flight)


def role_beats(state_dirs: Mapping[str, str]) -> dict[str, Beat | None]:
    return {
        role: read_beat(os.path.join(state_dirs[role], "heartbeat.json")) if state_dirs.get(role) else None
        for role in ROLE_NAMES
    }


def leg_models(state_dirs: Mapping[str, str], tail_bytes: int = JOURNAL_TAIL_BYTES) -> dict[str, str]:
    """leg_id -> model from each role journal's `agent_run` events (the
    legs table records the runtime, not the model). A torn or foreign line
    is skipped."""
    models: dict[str, str] = {}
    for role in ROLE_NAMES:
        directory = state_dirs.get(role)
        if not directory:
            continue
        try:
            lines = tail_lines(os.path.join(directory, "journal.jsonl"), tail_bytes)
        except OSError:
            continue
        for line in lines:
            if '"agent_run"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "agent_run":
                continue
            leg, model = event.get("leg_id"), event.get("model")
            if isinstance(leg, str) and leg and isinstance(model, str) and model:
                models[leg] = model
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


def remote_repo(root: str) -> str:
    """Return the origin as owner/name; an unavailable origin is cosmetic."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode:
        return ""
    url = result.stdout.strip().removesuffix(".git")
    if url.startswith("git@") and ":" in url:
        return url.split(":", 1)[1]
    marker = "github.com/"
    return url.split(marker, 1)[1] if marker in url else ""
