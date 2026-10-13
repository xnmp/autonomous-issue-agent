"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from datetime import datetime
from glob import glob

from ..env_file import source_env_file
from ..heartbeat import read_heartbeat_time
from ...domain.dashboard import ROLE_NAMES


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


def role_heartbeats(state_dirs: Mapping[str, str]) -> dict[str, datetime | None]:
    """Each role's last scheduler heartbeat (None when absent or terminal)."""
    return {
        role: read_heartbeat_time(os.path.join(state_dirs[role], "heartbeat.json")) if state_dirs.get(role) else None
        for role in ROLE_NAMES
    }


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
