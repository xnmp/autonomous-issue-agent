"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import os
import subprocess
from glob import glob

from ...adapters.journal import parse_ts, read_events
from ...domain import dashboard as dash


def deployment_names(repos_dir: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            os.path.splitext(os.path.basename(path))[0]
            for path in glob(os.path.join(repos_dir, "*.env"))
        )
    )


def _environ_agent_env(blob: bytes) -> str:
    for item in blob.split(b"\0"):
        if item.startswith(b"AGENT_ENV="):
            return item[len(b"AGENT_ENV=") :].decode(errors="replace")
    return ""


def deployment_of_env(agent_env: str) -> str:
    base = os.path.basename(agent_env or "")
    if base.startswith(".active.") and base.endswith(".env"):
        return base[len(".active.") : -len(".env")]
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
        roles.update(argument for argument in argv if argument in dash.ROLE_NAMES)
    return found, read_foreign


def collect_snapshot(
    *,
    name: str,
    repo: str,
    target_dir: str,
    journal_paths: list[str],
    log_dir: str,
    ledger_path: str,
    now_fn,
    stale_after_s: float,
) -> dash.Snapshot:
    feeds = tuple(
        (
            role,
            path,
            os.path.join(log_dir, f"{role}-{name}.log"),
            read_events(path),
        )
        for role, path in zip(dash.ROLE_NAMES, journal_paths)
    )
    ledger_events = read_events(ledger_path)
    deployment = dash.build_deployment(
        name,
        repo,
        feeds,
        ledger_events,
        now_fn=now_fn,
        stale_after_s=stale_after_s,
    )
    timestamps = tuple(
        parse_ts(event.get("ts"))
        for row in deployment.issues
        for event in row.events
    ) + tuple(parse_ts(event.get("ts")) for event in deployment.unattributed)
    return dash.Snapshot(name, repo, target_dir, deployment, timestamps)


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
